"""Async fan-out: orgs → projects → docs → conversations → messages → files.

Concurrency is gated by the client's Semaphore. The `chat_conversations`
list endpoint is fetched serially because parallel paginated reads against
that endpoint return inconsistent windows (the cursor advances per response
rather than as a stable snapshot); per-conversation detail pages run under
the semaphore.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterable
from typing import Any

import structlog

from .checkpoint import mark_dumped, needs_refresh
from .client import ClaudeClient
from .errors import EndpointChanged, NetworkError, SchemaDrift
from .store import (
    content_hash,
    transaction,
    upsert_account,
    upsert_attachment,
    upsert_conversation,
    upsert_message,
    upsert_org,
    upsert_project,
    upsert_project_doc,
    upsert_style,
    write_raw,
)

log = structlog.get_logger(__name__)

LIST_PAGE_SIZE = 100


async def fetch_account(client: ClaudeClient, conn: sqlite3.Connection, org_uuid: str) -> None:
    try:
        payload = await client.get_json("/api/account")
    except (EndpointChanged, NetworkError) as e:
        log.warning("account_fetch_failed", err=str(e))
        return
    write_raw("account", payload)
    with transaction(conn):
        upsert_account(conn, org_uuid, payload)
        mark_dumped(conn, "account", org_uuid, updated_at=None, payload=payload)


async def fetch_orgs(client: ClaudeClient, conn: sqlite3.Connection) -> list[dict[str, Any]]:
    try:
        payload = await client.get_json("/api/organizations")
    except EndpointChanged:
        return []
    if not isinstance(payload, list):
        raise SchemaDrift("/api/organizations not a list")
    write_raw("organizations", payload)
    with transaction(conn):
        for org in payload:
            if isinstance(org, dict) and "uuid" in org:
                upsert_org(conn, org)
    return [o for o in payload if isinstance(o, dict)]


async def fetch_styles(client: ClaudeClient, conn: sqlite3.Connection, org_uuid: str) -> None:
    try:
        styles = await client.get_json(f"/api/organizations/{org_uuid}/custom_styles")
    except EndpointChanged:
        return
    if not isinstance(styles, list):
        return
    write_raw(f"styles-{org_uuid[:8]}", styles)
    with transaction(conn):
        for s in styles:
            if isinstance(s, dict) and "uuid" in s:
                upsert_style(conn, org_uuid, s)


async def fetch_projects(
    client: ClaudeClient, conn: sqlite3.Connection, org_uuid: str
) -> list[dict[str, Any]]:
    try:
        projects = await client.get_json(f"/api/organizations/{org_uuid}/projects")
    except EndpointChanged:
        return []
    if not isinstance(projects, list):
        return []
    write_raw(f"projects-{org_uuid[:8]}", projects)
    with transaction(conn):
        for p in projects:
            if isinstance(p, dict) and "uuid" in p:
                upsert_project(conn, org_uuid, p)
    return [p for p in projects if isinstance(p, dict)]


async def fetch_project_docs(
    client: ClaudeClient, conn: sqlite3.Connection, project_uuid: str, org_uuid: str
) -> None:
    try:
        docs = await client.get_json(
            f"/api/organizations/{org_uuid}/projects/{project_uuid}/docs"
        )
    except EndpointChanged:
        return
    if not isinstance(docs, list):
        return
    with transaction(conn):
        for d in docs:
            if isinstance(d, dict) and "uuid" in d:
                upsert_project_doc(conn, project_uuid, d)


async def fetch_conversation_list(
    client: ClaudeClient, org_uuid: str
) -> list[dict[str, Any]]:
    """Serial pagination — parallel reads against this endpoint return
    inconsistent windows because the cursor isn't snapshot-stable."""
    out: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": LIST_PAGE_SIZE}
        if cursor:
            params["starting_after"] = cursor
        page = await client.get_json(
            f"/api/organizations/{org_uuid}/chat_conversations", params=params
        )
        if not isinstance(page, list):
            break
        if not page:
            break
        out.extend(p for p in page if isinstance(p, dict))
        last = page[-1]
        if not isinstance(last, dict) or "uuid" not in last:
            break
        cursor = last["uuid"]
        if len(page) < LIST_PAGE_SIZE:
            break
    return out


async def fetch_conversation_full(
    client: ClaudeClient, conn: sqlite3.Connection, org_uuid: str, conv_uuid: str
) -> dict[str, Any] | None:
    # `rendering_mode=messages` returns each message's `content` as a list of typed
    # blocks (text / thinking with summaries / etc.) — far higher fidelity than
    # `rendering_mode=raw`, which flattens everything into one text field with
    # mobile-style "This block is not supported on your current device yet."
    # placeholders for tool calls. Both `raw` and `messages` are the only valid
    # values the API accepts; everything else 400s.
    try:
        payload = await client.get_json(
            f"/api/organizations/{org_uuid}/chat_conversations/{conv_uuid}",
            params={"tree": "True", "rendering_mode": "messages"},
        )
    except EndpointChanged:
        return None
    if not isinstance(payload, dict):
        raise SchemaDrift(f"conversation {conv_uuid} returned non-dict body")
    raw_path = write_raw(f"conv-{conv_uuid[:8]}", payload)
    with transaction(conn):
        upsert_conversation(conn, org_uuid, payload, raw_path=str(raw_path))
        msgs: Iterable[Any] = payload.get("chat_messages") or []
        for idx, m in enumerate(msgs):
            if not isinstance(m, dict) or "uuid" not in m:
                continue
            m.setdefault("index", idx)
            upsert_message(conn, conv_uuid, m)
            for att in m.get("attachments") or []:
                if isinstance(att, dict):
                    upsert_attachment(conn, m["uuid"], att)
            for f in m.get("files_v2") or []:
                if isinstance(f, dict):
                    upsert_attachment(conn, m["uuid"], f)
        mark_dumped(
            conn,
            "conversation",
            conv_uuid,
            updated_at=payload.get("updated_at"),
            payload=payload,
        )
    return payload


async def dump_all(
    client: ClaudeClient,
    conn: sqlite3.Connection,
    org_uuid: str,
    *,
    org_name: str | None = None,
    incremental: bool = True,
) -> dict[str, int]:
    """Pull everything for one org. Returns counts: {orgs, projects, conversations, refreshed}."""
    counts = {"projects": 0, "conversations": 0, "refreshed": 0, "skipped": 0, "styles": 0}

    # Pin the org first so restore's preflight (`SELECT uuid FROM org`) passes.
    with transaction(conn):
        upsert_org(conn, {"uuid": org_uuid, "name": org_name})

    await fetch_account(client, conn, org_uuid)
    await fetch_styles(client, conn, org_uuid)
    counts["styles"] = conn.execute(
        "SELECT COUNT(*) FROM custom_style WHERE org_uuid=?", (org_uuid,)
    ).fetchone()[0]

    projects = await fetch_projects(client, conn, org_uuid)
    counts["projects"] = len(projects)

    async def doc_task(p_uuid: str) -> None:
        await fetch_project_docs(client, conn, p_uuid, org_uuid)

    if projects:
        await asyncio.gather(*(doc_task(p["uuid"]) for p in projects), return_exceptions=False)

    listing = await fetch_conversation_list(client, org_uuid)
    counts["conversations"] = len(listing)
    log.info("conv_list_loaded", total=len(listing))
    progress_step = 25

    async def conv_task(meta: dict[str, Any]) -> None:
        cu = meta["uuid"]
        if incremental and not needs_refresh(
            conn, "conversation", cu, updated_at=meta.get("updated_at"),
            payload_hash=content_hash(meta),
        ):
            counts["skipped"] += 1
        else:
            try:
                await fetch_conversation_full(client, conn, org_uuid, cu)
                counts["refreshed"] += 1
            except (NetworkError, SchemaDrift) as e:
                log.warning("conv_fetch_failed", uuid=cu, err=str(e))
        processed = counts["refreshed"] + counts["skipped"]
        if processed > 0 and processed % progress_step == 0:
            log.info(
                "conv_progress",
                processed=processed,
                total=len(listing),
                refreshed=counts["refreshed"],
                skipped=counts["skipped"],
            )

    if listing:
        await asyncio.gather(
            *(conv_task(m) for m in listing if "uuid" in m), return_exceptions=False
        )

    return counts
