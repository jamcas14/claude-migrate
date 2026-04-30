"""Idempotency tests: UPSERT, migration_log, checkpoint logic."""

from __future__ import annotations

import sqlite3

from claude_migrate.checkpoint import mark_dumped, needs_refresh
from claude_migrate.store import (
    already_migrated,
    log_migration,
    upsert_conversation,
    upsert_org,
    upsert_project,
)


def test_upsert_org_idempotent(db_conn: sqlite3.Connection) -> None:
    upsert_org(db_conn, {"uuid": "o1", "name": "First"})
    upsert_org(db_conn, {"uuid": "o1", "name": "Renamed"})
    rows = db_conn.execute("SELECT name FROM org WHERE uuid='o1'").fetchall()
    assert len(rows) == 1
    assert rows[0]["name"] == "Renamed"


def test_upsert_conversation_preserves_pk(db_conn: sqlite3.Connection) -> None:
    upsert_conversation(db_conn, "o1", {"uuid": "c1", "name": "x"})
    upsert_conversation(db_conn, "o1", {"uuid": "c1", "name": "y", "model": "m"})
    rows = db_conn.execute("SELECT title, model FROM conversation").fetchall()
    assert len(rows) == 1
    assert rows[0]["title"] == "y"
    assert rows[0]["model"] == "m"


def test_migration_log_idempotent(db_conn: sqlite3.Connection) -> None:
    log_migration(
        db_conn, source_uuid="s1", object_type="conversation",
        target_profile="t", target_uuid="t1", status="ok",
    )
    assert already_migrated(db_conn, "s1", "t") == "t1"
    log_migration(
        db_conn, source_uuid="s1", object_type="conversation",
        target_profile="t", target_uuid="t1-new", status="ok",
    )
    assert already_migrated(db_conn, "s1", "t") == "t1-new"


def test_already_migrated_other_profile_independent(db_conn: sqlite3.Connection) -> None:
    log_migration(
        db_conn, source_uuid="s1", object_type="conversation",
        target_profile="alpha", target_uuid="a1", status="ok",
    )
    assert already_migrated(db_conn, "s1", "alpha") == "a1"
    assert already_migrated(db_conn, "s1", "beta") is None


def test_already_migrated_skips_errors(db_conn: sqlite3.Connection) -> None:
    log_migration(
        db_conn, source_uuid="s1", object_type="conversation",
        target_profile="t", target_uuid=None, status="error",
        error="boom",
    )
    assert already_migrated(db_conn, "s1", "t") is None


def test_checkpoint_first_seen_needs_refresh(db_conn: sqlite3.Connection) -> None:
    assert needs_refresh(db_conn, "conversation", "c1", updated_at=None) is True


def test_checkpoint_unchanged_skips(db_conn: sqlite3.Connection) -> None:
    payload = {"uuid": "c1", "msg": "v1"}
    mark_dumped(db_conn, "conversation", "c1", updated_at="2024-01-01", payload=payload)
    assert needs_refresh(
        db_conn, "conversation", "c1", updated_at="2024-01-01"
    ) is False


def test_checkpoint_updated_at_change_triggers(db_conn: sqlite3.Connection) -> None:
    mark_dumped(db_conn, "conversation", "c1", updated_at="2024-01-01", payload={"v": 1})
    assert needs_refresh(
        db_conn, "conversation", "c1", updated_at="2024-01-02"
    ) is True


def test_checkpoint_hash_change_triggers(db_conn: sqlite3.Connection) -> None:
    payload_v1 = {"uuid": "c1", "v": 1}
    payload_v2 = {"uuid": "c1", "v": 2}
    mark_dumped(db_conn, "conversation", "c1", updated_at=None, payload=payload_v1)
    from claude_migrate.store import content_hash

    assert needs_refresh(
        db_conn, "conversation", "c1", updated_at=None,
        payload_hash=content_hash(payload_v2),
    ) is True


def test_dump_all_writes_org_row_for_restore_preflight(db_conn: sqlite3.Connection) -> None:
    """Regression: dump_all must populate org so run_restore's preflight passes."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from claude_migrate.fetch import dump_all

    fake_client = MagicMock()
    fake_client.get_json = AsyncMock(side_effect=Exception("not used"))

    async def go() -> None:
        # Patch network-touching calls; dump_all should still write the org row.
        from claude_migrate import fetch

        async def _noop(*a: object, **kw: object) -> None:
            return None

        async def _empty_list(*a: object, **kw: object) -> list[dict[str, object]]:
            return []

        original_account = fetch.fetch_account
        original_styles = fetch.fetch_styles
        original_projects = fetch.fetch_projects
        original_conv_list = fetch.fetch_conversation_list
        try:
            fetch.fetch_account = _noop  # type: ignore[assignment]
            fetch.fetch_styles = _noop  # type: ignore[assignment]
            fetch.fetch_projects = _empty_list  # type: ignore[assignment]
            fetch.fetch_conversation_list = _empty_list  # type: ignore[assignment]
            await dump_all(fake_client, db_conn, "org-uuid-1", org_name="Test Org")
        finally:
            fetch.fetch_account = original_account  # type: ignore[assignment]
            fetch.fetch_styles = original_styles  # type: ignore[assignment]
            fetch.fetch_projects = original_projects  # type: ignore[assignment]
            fetch.fetch_conversation_list = original_conv_list  # type: ignore[assignment]

    asyncio.run(go())

    row = db_conn.execute("SELECT uuid, name FROM org").fetchone()
    assert row is not None, "dump_all must write the org row"
    assert row["uuid"] == "org-uuid-1"
    assert row["name"] == "Test Org"


def test_upsert_project_round_trip(db_conn: sqlite3.Connection) -> None:
    upsert_project(db_conn, "o1", {
        "uuid": "p1", "name": "P", "prompt_template": "do thing",
        "created_at": "2024-01-01", "updated_at": "2024-01-02",
    })
    row = db_conn.execute("SELECT name, prompt_template FROM project WHERE uuid='p1'").fetchone()
    assert row["name"] == "P"
    assert row["prompt_template"] == "do thing"
