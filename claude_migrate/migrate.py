"""Top-level migration orchestrator. Wires together restore.py per Section 11."""

from __future__ import annotations

import sqlite3
from typing import Any

import structlog

from .config import Settings
from .restore import (
    RestoreSummary,
    restore_all_conversations,
    restore_profile_prefs,
    restore_projects,
    restore_styles,
)
from .session import open_session
from .state import RestoreState
from .store import fetch_one, open_db

log = structlog.get_logger(__name__)


async def run_restore(
    *,
    target_profile: str,
    dry_run: bool,
    do_prefs: bool = True,
    do_styles: bool = True,
    do_projects: bool = True,
    do_conversations: bool = True,
    concurrency: int = 1,
    settings: Settings | None = None,
) -> RestoreSummary:
    summary = RestoreSummary(dry_run=dry_run)
    async with open_session(target_profile, settings=settings) as session:
        conn = open_db()
        try:
            # Source must have at least an org row in the local archive.
            org_row = fetch_one(conn, "SELECT uuid FROM org LIMIT 1")
            if org_row is None:
                log.warning(
                    "no_local_archive",
                    detail="no rows in `org` table — "
                    "run `claude-migrate dump` against source first",
                )
                return summary

            state = RestoreState(conn, target_profile)
            # Order of operations per Section 11.
            if do_prefs:
                summary.profile_prefs = await restore_profile_prefs(
                    session.client, conn, dry_run=dry_run
                )
            if do_styles:
                await restore_styles(
                    session.client, conn, session.org_uuid, state,
                    dry_run=dry_run, summary=summary,
                )
            if do_projects:
                project_map = await restore_projects(
                    session.client, conn, session.org_uuid, state,
                    dry_run=dry_run, summary=summary,
                )
            else:
                # Skipping the projects phase — but conversations still need
                # the map of source→target project uuids to wire themselves up.
                project_map = state.project_map()
            if do_conversations:
                await restore_all_conversations(
                    session.client, conn, session.org_uuid, state, project_map,
                    dry_run=dry_run, summary=summary, concurrency=concurrency,
                )
        finally:
            conn.close()
    return summary


def migration_status(target_profile: str) -> dict[str, Any]:
    """Read-only summary of migration_log + source archive for one profile.
    Pure SQLite — no network calls."""
    conn = open_db()
    try:
        state = RestoreState(conn, target_profile)
        archive = {
            "conversations": _table_count(conn, "conversation"),
            "projects": _table_count(conn, "project"),
            "styles": _table_count(conn, "custom_style"),
        }
        target_ok = {
            "conversations": state.migrated_count("conversation"),
            "projects": state.migrated_count("project"),
            "styles": state.migrated_count("style"),
        }
        return {
            "archive": archive,
            "target_ok": target_ok,
            "failures": state.recent_failures(),
            "last_activity": state.last_activity(),
        }
    finally:
        conn.close()


async def dry_run_plan(*, target_profile: str) -> dict[str, int]:
    """Pre-flight count: how many of each object type are not yet migrated."""
    conn = open_db()
    try:
        state = RestoreState(conn, target_profile)
        return {
            "projects_pending": state.pending_count("project"),
            "projects_total": _table_count(conn, "project"),
            "styles_pending": state.pending_count("custom_style"),
            "styles_total": _table_count(conn, "custom_style"),
            "conversations_pending": state.pending_count("conversation"),
            "conversations_total": _table_count(conn, "conversation"),
        }
    finally:
        conn.close()


def _table_count(conn: sqlite3.Connection, table: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    return int(row[0])


async def verify_target_conversations(
    *,
    target_profile: str,
    reconcile: bool = False,
) -> dict[str, Any]:
    """For each conversation logged as `ok` against this profile, GET the
    target to confirm it still exists. Returns counts + missing UUIDs.

    If `reconcile=True`, drops migration_log rows for missing target convs so
    a subsequent restore --execute will re-create them.
    """
    from .errors import EndpointChanged, NetworkError

    confirmed = 0
    missing: list[tuple[str, str]] = []
    async with open_session(target_profile) as session:
        conn = open_db()
        try:
            state = RestoreState(conn, target_profile)
            for source_uuid, target_uuid in state.confirmed_conversations():
                try:
                    await session.client.get_json(
                        f"/api/organizations/{session.org_uuid}"
                        f"/chat_conversations/{target_uuid}",
                        timeout=15.0,
                    )
                    confirmed += 1
                except EndpointChanged:
                    missing.append((source_uuid, target_uuid))
                except NetworkError as e:
                    log.warning(
                        "verify_probe_failed",
                        source_uuid=source_uuid, err=str(e),
                    )
            if reconcile and missing:
                for src_uuid, _ in missing:
                    state.drop(src_uuid)
        finally:
            conn.close()
        return {
            "email": session.email,
            "confirmed": confirmed,
            "missing": missing,
            "reconciled": reconcile and bool(missing),
        }
