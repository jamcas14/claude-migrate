"""Idempotent restore: source archive (sqlite) → target claude.ai org.

Each phase (styles → projects → conversations) is a small worker plus a call
to `migrate_row`. The worker raises session-fatal exceptions to bubble out;
everything else turns into a `WorkerOutcome.failed(...)`. `Pacer` owns the
pacing and 429 cooldown for the strictly-serial conversation phase.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog

from .client import ClaudeClient
from .errors import (
    AuthExpired,
    ClaudeMigrateError,
    ClientVersionStale,
    CloudflareChallenge,
    EndpointChanged,
    NetworkError,
    RateLimited,
    RestoreConflict,
    SchemaDrift,
    TLSReject,
)
from .render import prepare_paste_payload
from .runner import Pacer, WorkerOutcome, migrate_row
from .state import RestoreState
from .store import fetch_all
from .transport import send_payload

log = structlog.get_logger(__name__)

PROJECTS_CONCURRENCY = 3


@dataclass
class RestoreSummary:
    profile_prefs: bool = False
    styles_total: int = 0
    styles_migrated: int = 0
    projects_total: int = 0
    projects_migrated: int = 0
    conversations_total: int = 0
    conversations_migrated: int = 0
    skipped: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    dry_run: bool = False

    def record_failure(self, source_uuid: str, label: str) -> None:
        self.failed.append((source_uuid, label))


# ---------------------------------------------------------------------------
# Profile preferences
# ---------------------------------------------------------------------------


async def restore_profile_prefs(
    client: ClaudeClient, conn: sqlite3.Connection, *, dry_run: bool
) -> bool:
    """Best-effort profile-prefs sync. Never aborts the run — prefs are cosmetic
    relative to chats/projects, and /api/account's body shape has shifted across
    builds; treat any non-session error as a non-fatal warning.
    """
    row = conn.execute("SELECT profile, raw FROM account LIMIT 1").fetchone()
    if row is None:
        return False
    if dry_run:
        return True
    raw = json.loads(row["raw"]) if row["raw"] else {}
    settable = {k: v for k, v in raw.items() if k in {"full_name", "settings", "name", "role"}}
    if not settable:
        return False
    try:
        await client.put_json("/api/account", body=settable)
    except (AuthExpired, CloudflareChallenge):
        # Session-fatal: re-raise so the orchestrator halts and surfaces re-auth.
        raise
    except ClaudeMigrateError as e:
        log.warning(
            "profile_prefs_skipped",
            err=str(e),
            hint="prefs are best-effort; chats/projects/styles will continue.",
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Custom styles (sequential)
# ---------------------------------------------------------------------------


async def restore_styles(
    client: ClaudeClient,
    conn: sqlite3.Connection,
    target_org: str,
    state: RestoreState,
    *,
    dry_run: bool,
    summary: RestoreSummary,
) -> None:
    rows = fetch_all(conn, "SELECT uuid, raw FROM custom_style")
    summary.styles_total = len(rows)
    if dry_run:
        return
    for row in rows:
        source_uuid = row["uuid"]

        async def work(row: sqlite3.Row = row) -> WorkerOutcome:
            style = json.loads(row["raw"])
            body = {k: style.get(k) for k in ("name", "summary", "prompt", "examples")}
            body = {k: v for k, v in body.items() if v is not None}
            try:
                created = await client.post_json(
                    f"/api/organizations/{target_org}/custom_styles", body=body
                )
            except RateLimited as e:
                return WorkerOutcome.failed(f"style: {e}", rate_limited=True)
            except (EndpointChanged, NetworkError) as e:
                return WorkerOutcome.failed(f"style: {e}")
            new_uuid = created.get("uuid") if isinstance(created, dict) else None
            if not isinstance(new_uuid, str):
                return WorkerOutcome.failed("style: create returned no uuid")
            return WorkerOutcome.ok(new_uuid)

        outcome = await migrate_row(
            state=state, object_type="style", source_uuid=source_uuid, work=work,
        )
        if outcome is None:
            summary.skipped += 1
        elif outcome.target_uuid:
            summary.styles_migrated += 1
        else:
            summary.record_failure(source_uuid, outcome.error or "unknown")


# ---------------------------------------------------------------------------
# Projects + their docs (parallel under semaphore)
# ---------------------------------------------------------------------------


async def restore_projects(
    client: ClaudeClient,
    conn: sqlite3.Connection,
    target_org: str,
    state: RestoreState,
    *,
    dry_run: bool,
    summary: RestoreSummary,
) -> dict[str, str]:
    rows = fetch_all(conn, "SELECT uuid, name, prompt_template, raw FROM project")
    summary.projects_total = len(rows)
    sem = asyncio.Semaphore(PROJECTS_CONCURRENCY)
    mapping: dict[str, str] = dict(state.project_map())  # carry over prior runs

    async def one(row: sqlite3.Row) -> None:
        async with sem:
            source_uuid = row["uuid"]
            if dry_run:
                # Dry run: don't touch state, just count what would be skipped.
                if state.already_migrated(source_uuid):
                    summary.skipped += 1
                return

            async def work() -> WorkerOutcome:
                try:
                    source_raw = json.loads(row["raw"]) if row["raw"] else {}
                    description = source_raw.get("description") or ""
                    created = await client.post_json(
                        f"/api/organizations/{target_org}/projects",
                        body={
                            "name": row["name"] or "(untitled)",
                            "description": description,
                            "is_private": True,
                        },
                    )
                    new_uuid = created.get("uuid") if isinstance(created, dict) else None
                    if not isinstance(new_uuid, str):
                        return WorkerOutcome.failed("project: create returned no uuid")
                    if row["prompt_template"]:
                        await client.put_json(
                            f"/api/organizations/{target_org}/projects/{new_uuid}",
                            body={"prompt_template": row["prompt_template"]},
                        )
                    docs = fetch_all(
                        conn,
                        "SELECT file_name, content FROM project_doc WHERE project_uuid=?",
                        (source_uuid,),
                    )
                    for doc in docs:
                        if not doc["file_name"] or not doc["content"]:
                            continue
                        await client.post_json(
                            f"/api/organizations/{target_org}/projects/{new_uuid}/docs",
                            body={"file_name": doc["file_name"], "content": doc["content"]},
                        )
                    return WorkerOutcome.ok(new_uuid)
                except RateLimited as e:
                    return WorkerOutcome.failed(f"project: {e}", rate_limited=True)
                except (EndpointChanged, NetworkError, SchemaDrift) as e:
                    return WorkerOutcome.failed(f"project: {e}")

            outcome = await migrate_row(
                state=state, object_type="project", source_uuid=source_uuid, work=work,
            )
            if outcome is None:
                # Already migrated in a prior run — project_map already captured it.
                summary.skipped += 1
            elif outcome.target_uuid:
                mapping[source_uuid] = outcome.target_uuid
                summary.projects_migrated += 1
            else:
                summary.record_failure(source_uuid, outcome.error or "unknown")

    if rows:
        await asyncio.gather(*(one(r) for r in rows))
    return mapping


# ---------------------------------------------------------------------------
# Orphan cleanup — wipe empty conversations from a target left behind by a
# failed prior restore (zero messages, empty name, recent created_at).
# ---------------------------------------------------------------------------


async def find_orphan_conversations(
    client: ClaudeClient,
    target_org: str,
    *,
    created_after: datetime,
    created_before: datetime,
    require_empty_name: bool = False,
) -> list[dict[str, Any]]:
    """Find restore-orphan conversations on target.

    All conversations within [created_after, created_before] are fetched in full
    (?tree=True) and only those with **zero messages** are returned. Real user
    chats always have at least the user prompt + assistant reply, so zero
    messages is a strong orphan signal regardless of name.

    `require_empty_name=True` adds a pre-filter for `name == ""` to skip the
    expensive per-conv fetch when the orphans are known to be unnamed.
    """
    cursor: str | None = None
    candidates: list[dict[str, Any]] = []
    while True:
        params: dict[str, Any] = {"limit": 100}
        if cursor:
            params["starting_after"] = cursor
        page = await client.get_json(
            f"/api/organizations/{target_org}/chat_conversations", params=params
        )
        if not isinstance(page, list) or not page:
            break
        page_oldest = ""
        for c in page:
            if not isinstance(c, dict):
                continue
            ca = c.get("created_at") or ""
            page_oldest = ca if not page_oldest or ca < page_oldest else page_oldest
            if require_empty_name and c.get("name"):
                continue
            try:
                ca_dt = datetime.fromisoformat(ca)
            except ValueError:
                continue
            if created_after <= ca_dt <= created_before:
                candidates.append(c)
        last = page[-1]
        if not isinstance(last, dict) or "uuid" not in last or len(page) < 100:
            break
        # Stop paginating once we've gone past the window's lower bound.
        try:
            oldest_dt = datetime.fromisoformat(page_oldest)
            if oldest_dt < created_after:
                break
        except ValueError:
            pass
        cursor = last["uuid"]

    # Per-candidate verify: fetch the tree and only keep zero-message ones.
    confirmed: list[dict[str, Any]] = []
    for cand in candidates:
        cu = cand.get("uuid")
        if not isinstance(cu, str):
            continue
        try:
            full = await client.get_json(
                f"/api/organizations/{target_org}/chat_conversations/{cu}",
                params={"tree": "True", "rendering_mode": "raw"},
                timeout=15.0,
            )
        except (EndpointChanged, NetworkError) as e:
            log.warning("orphan_verify_failed", uuid=cu, err=str(e))
            continue
        msgs = full.get("chat_messages") if isinstance(full, dict) else None
        if isinstance(msgs, list) and len(msgs) == 0:
            confirmed.append(cand)
    return confirmed


async def reorder_conversations(
    client: ClaudeClient,
    conn: sqlite3.Connection,
    target_org: str,
    state: RestoreState,
    *,
    dry_run: bool,
) -> tuple[int, int, list[tuple[str, str]]]:
    """Reorder migrated chats on target so they sort like the source's
    last-modified order. Walks source archive in `updated_at ASC` order and
    sends a no-op PUT (re-set the current name) to each target chat. Each PUT
    bumps the target's `updated_at` to now, so the loop ends with the most
    recently-modified source chat being the most recently-touched on target —
    exactly how Recents sorts.

    Returns (touched, missing, errors).
    """
    rows = fetch_all(
        conn,
        "SELECT uuid, updated_at, created_at, title FROM conversation "
        "WHERE updated_at IS NOT NULL "
        "ORDER BY updated_at ASC",
    )
    touched = 0
    missing = 0
    errors: list[tuple[str, str]] = []
    for row in rows:
        source_uuid = row["uuid"]
        target_uuid = state.already_migrated(source_uuid)
        if not target_uuid:
            missing += 1
            continue
        if dry_run:
            touched += 1
            continue
        try:
            current = await client.get_json(
                f"/api/organizations/{target_org}/chat_conversations/{target_uuid}",
                timeout=15.0,
            )
            name = current.get("name") if isinstance(current, dict) else None
            if not isinstance(name, str) or not name:
                # Fallback: reconstruct the [YYYY-MM-DD] Title we used at create-time
                prefix = _date_prefix(row["created_at"])
                base = row["title"] or "(untitled)"
                name = f"[{prefix}] {base}" if prefix else base
            await client.put_json(
                f"/api/organizations/{target_org}/chat_conversations/{target_uuid}",
                body={"name": name},
                timeout=15.0,
            )
            touched += 1
            await asyncio.sleep(0.5)
        except (EndpointChanged, NetworkError) as e:
            errors.append((source_uuid, f"reorder: {e}"))
    return touched, missing, errors


async def delete_conversation(
    client: ClaudeClient, target_org: str, conv_uuid: str
) -> bool:
    try:
        await client.request(
            "DELETE",
            f"/api/organizations/{target_org}/chat_conversations/{conv_uuid}",
            expect_json=False,
        )
        return True
    except (EndpointChanged, NetworkError) as e:
        log.warning("orphan_delete_failed", uuid=conv_uuid, err=str(e))
        return False


# ---------------------------------------------------------------------------
# Conversations (strictly serial; pacer owns sleep + 429 cooldown)
# ---------------------------------------------------------------------------


def _date_prefix(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d")
    except ValueError:
        return ""


async def restore_conversation(
    client: ClaudeClient,
    conn: sqlite3.Connection,
    target_org: str,
    source_conv: sqlite3.Row,
    project_map: dict[str, str],
) -> WorkerOutcome:
    """Migrate one conversation. Returns a WorkerOutcome.

    Lets session-fatal exceptions (`AuthExpired`, `CloudflareChallenge`,
    `TLSReject`) propagate after cleanup; every other failure turns into
    `WorkerOutcome.failed(...)`, with cleanup of any half-created target
    conversation handled inline.
    """
    payload = prepare_paste_payload(conn, source_conv["uuid"])
    title = source_conv["title"] or "(untitled)"
    prefix = _date_prefix(source_conv["created_at"])
    full_title = f"[{prefix}] {title}" if prefix else title
    proj_target = project_map.get(source_conv["project_uuid"]) if source_conv["project_uuid"] else None

    new_uuid: str | None = None
    try:
        # Single POST sets name + optional project_uuid in one shot.
        # (PATCH on this endpoint returns 405 on the current API; the original
        # spec assumed PATCH-after-create but probe shows POST takes both.)
        create_body: dict[str, Any] = {"name": full_title}
        if proj_target:
            create_body["project_uuid"] = proj_target
        created = await client.post_json(
            f"/api/organizations/{target_org}/chat_conversations",
            body=create_body,
        )
        if not isinstance(created, dict) or not isinstance(created.get("uuid"), str):
            raise SchemaDrift("conversation create returned no uuid")
        new_uuid = created["uuid"]
        await send_payload(client, target_org, new_uuid, payload)
        return WorkerOutcome.ok(new_uuid)
    except RateLimited as e:
        await _cleanup_partial(client, target_org, new_uuid)
        return WorkerOutcome.failed(f"{type(e).__name__}: {e}", rate_limited=True)
    except NetworkError as e:
        await _cleanup_partial(client, target_org, new_uuid)
        return WorkerOutcome.failed(f"{type(e).__name__}: {e}")
    except (AuthExpired, CloudflareChallenge, TLSReject):
        # Session-fatal: clean up the half-created chat, then re-raise so the
        # orchestrator stops and surfaces a re-auth instruction.
        await _cleanup_partial(client, target_org, new_uuid)
        raise
    except (ClientVersionStale, EndpointChanged, SchemaDrift, RestoreConflict) as e:
        # ClientVersionStale: per-row failure (the run can continue; only this
        # request's body shape was rejected). User can refresh headers and retry.
        await _cleanup_partial(client, target_org, new_uuid)
        return WorkerOutcome.failed(f"{type(e).__name__}: {e}")


async def _cleanup_partial(
    client: ClaudeClient, target_org: str, new_uuid: str | None
) -> None:
    """Best-effort delete of a half-created conversation."""
    if not new_uuid:
        return
    try:
        await client.request(
            "DELETE",
            f"/api/organizations/{target_org}/chat_conversations/{new_uuid}",
            expect_json=False,
            timeout=15.0,
        )
    except (EndpointChanged, NetworkError) as e:
        log.warning("partial_cleanup_failed", uuid=new_uuid, err=str(e))


async def restore_all_conversations(
    client: ClaudeClient,
    conn: sqlite3.Connection,
    target_org: str,
    state: RestoreState,
    project_map: dict[str, str],
    *,
    dry_run: bool,
    summary: RestoreSummary,
    concurrency: int = 1,
) -> None:
    """Migrate every conversation in the local archive against `state`'s profile.

    `concurrency=1` (default) keeps strictly-serial ordering so target Recents
    matches the source's last-modified order out of the box. `concurrency>1`
    runs N workers behind a Semaphore — drops wall-clock by ~Nx at the cost of
    non-deterministic Recents ordering (run `claude-migrate reorder` afterwards
    to fix). The shared `Pacer` serializes workers during a 429 cooldown so
    they don't all hammer back at the API at once.
    """
    from .config import RESTORE_CHAT_RATE_LIMIT_SLEEP_SEC

    # Sort by source `updated_at ASC` so that the most-recently-modified source
    # chat is migrated last, ending up at the top of target Recents (which
    # sorts by updated_at desc). Falls back to created_at when updated_at is
    # NULL — chats with no modification history get placed by their create-time.
    #
    # Within projects: order by the source project's own created_at ASC so
    # multi-project sources keep chronological project ordering instead of
    # being interleaved alphabetically by uuid.
    rows = fetch_all(
        conn,
        "SELECT c.uuid AS uuid, c.project_uuid AS project_uuid, "
        "  c.title AS title, c.created_at AS created_at, "
        "  c.updated_at AS updated_at "
        "FROM conversation c "
        "LEFT JOIN project p ON c.project_uuid = p.uuid "
        "ORDER BY (c.project_uuid IS NULL) ASC, "
        "  COALESCE(p.created_at, ''), "
        "  COALESCE(c.updated_at, c.created_at) ASC",
    )
    summary.conversations_total = len(rows)
    if dry_run:
        return

    pacer = Pacer(
        base_sleep_sec=client.settings.chat_sleep_sec,
        rate_limit_sleep_sec=RESTORE_CHAT_RATE_LIMIT_SLEEP_SEC,
    )
    total = len(rows)
    max_attempts = 3  # allow two retries past the first rate-limited failure

    async def one(idx: int, row: sqlite3.Row) -> None:
        source_uuid = row["uuid"]
        title = (row["title"] or "(untitled)")[:60]

        async def work() -> WorkerOutcome:
            return await restore_conversation(
                client, conn, target_org, row, project_map,
            )

        outcome: WorkerOutcome | None = None
        for attempt in range(1, max_attempts + 1):
            await pacer.before()  # respect any active 429 pause window
            log.info(
                "conv_migrate_start",
                progress=f"{idx + 1}/{total}",
                source=source_uuid[:8],
                title=title,
                attempt=attempt,
            )
            outcome = await migrate_row(
                state=state, object_type="conversation",
                source_uuid=source_uuid, work=work,
            )
            if outcome is None or outcome.target_uuid is not None:
                break
            if outcome.rate_limited and attempt < max_attempts:
                log.warning(
                    "conv_migrate_retry",
                    progress=f"{idx + 1}/{total}",
                    source=source_uuid[:8],
                    attempt=attempt,
                )
                await pacer.after(outcome)  # bumps pause_until
                continue
            break

        if outcome is None:
            summary.skipped += 1
            log.info(
                "conv_migrate_skip",
                progress=f"{idx + 1}/{total}",
                source=source_uuid[:8],
                reason="already migrated",
            )
        elif outcome.target_uuid:
            summary.conversations_migrated += 1
            log.info(
                "conv_migrate_ok",
                progress=f"{idx + 1}/{total}",
                source=source_uuid[:8],
                target=outcome.target_uuid[:8],
            )
            await pacer.after(outcome)  # base inter-call sleep
        else:
            summary.record_failure(source_uuid, outcome.error or "unknown")
            log.warning(
                "conv_migrate_fail",
                progress=f"{idx + 1}/{total}",
                source=source_uuid[:8],
                err=outcome.error,
            )
            await pacer.after(outcome)

    if concurrency <= 1:
        for idx, row in enumerate(rows):
            await one(idx, row)
        return

    sem = asyncio.Semaphore(concurrency)

    async def gated(idx: int, row: sqlite3.Row) -> None:
        async with sem:
            await one(idx, row)

    await asyncio.gather(*(gated(idx, r) for idx, r in enumerate(rows)))
