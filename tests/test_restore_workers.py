"""Worker-level tests: error propagation contracts for the restore pipeline."""

from __future__ import annotations

import sqlite3
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from claude_migrate.errors import (
    AuthExpired,
    CloudflareChallenge,
    EndpointChanged,
    NetworkError,
    RateLimited,
    TLSReject,
)
from claude_migrate.restore import (
    reorder_conversations,
    restore_profile_prefs,
)
from claude_migrate.state import RestoreState
from claude_migrate.store import (
    log_migration,
    upsert_account,
    upsert_conversation,
    upsert_org,
)

# ---------------------------------------------------------------------------
# restore_profile_prefs — session-fatal propagation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc_cls", [AuthExpired, CloudflareChallenge, TLSReject],
)
async def test_restore_profile_prefs_propagates_session_fatal(
    exc_cls: type[Exception], db_conn: sqlite3.Connection,
) -> None:
    """All three session-fatal exceptions must propagate; otherwise the
    orchestrator continues into doomed-to-fail later phases."""
    upsert_org(db_conn, {"uuid": "o1", "name": "Test"})
    upsert_account(db_conn, "o1", {"full_name": "User"})

    client = MagicMock()
    client.put_json = AsyncMock(side_effect=exc_cls("simulated"))

    with pytest.raises(exc_cls):
        await restore_profile_prefs(client, db_conn, dry_run=False)


async def test_restore_profile_prefs_swallows_recoverable_errors(
    db_conn: sqlite3.Connection,
) -> None:
    """Non-session-fatal errors (NetworkError, ClientVersionStale) should NOT
    abort the run — prefs are best-effort relative to chats and projects."""
    upsert_org(db_conn, {"uuid": "o1", "name": "Test"})
    upsert_account(db_conn, "o1", {"full_name": "User"})

    client = MagicMock()
    client.put_json = AsyncMock(side_effect=NetworkError("transient"))

    result = await restore_profile_prefs(client, db_conn, dry_run=False)
    assert result is False  # logged + skipped, no raise


# ---------------------------------------------------------------------------
# reorder_conversations — RateLimited handling
# ---------------------------------------------------------------------------


class _NopPacer:
    """Drop-in Pacer replacement for tests — no sleeping, no cooldown."""

    async def before(self) -> None:
        return None

    async def after(self, outcome: object) -> None:
        return None


async def test_reorder_records_rate_limited_per_row(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 429 mid-reorder used to abort the whole loop. Now: per-row error,
    loop continues."""
    upsert_conversation(db_conn, "o1", {
        "uuid": "c1", "name": "Chat 1",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-02T00:00:00Z",
    })
    upsert_conversation(db_conn, "o1", {
        "uuid": "c2", "name": "Chat 2",
        "created_at": "2024-01-03T00:00:00Z",
        "updated_at": "2024-01-04T00:00:00Z",
    })
    state = RestoreState(db_conn, "tgt")
    log_migration(
        db_conn, source_uuid="c1", object_type="conversation",
        target_profile="tgt", target_uuid="t1", status="ok",
    )
    log_migration(
        db_conn, source_uuid="c2", object_type="conversation",
        target_profile="tgt", target_uuid="t2", status="ok",
    )

    call_count = 0

    async def fake_get_json(path: str, **kw: Any) -> dict[str, str]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RateLimited("simulated 429")
        return {"name": "Existing target name"}

    client = MagicMock()
    client.get_json = AsyncMock(side_effect=fake_get_json)
    client.put_json = AsyncMock(return_value={"uuid": "ok"})

    # Replace the Pacer constructor so reorder doesn't actually sleep.
    import claude_migrate.restore as restore_mod
    monkeypatch.setattr(restore_mod, "Pacer", lambda **kw: _NopPacer())

    touched, _missing, errors = await reorder_conversations(
        client, db_conn, "tgt-org", state, dry_run=False,
    )

    # First row 429ed → recorded as error. Second row went through.
    assert len(errors) == 1
    assert "429" in errors[0][1] or "RateLimited" in errors[0][1]
    assert touched == 1


async def test_reorder_records_other_typed_errors(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EndpointChanged / NetworkError should also be per-row failures, not aborts."""
    upsert_conversation(db_conn, "o1", {
        "uuid": "c1", "name": "Chat 1",
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-02T00:00:00Z",
    })
    state = RestoreState(db_conn, "tgt")
    log_migration(
        db_conn, source_uuid="c1", object_type="conversation",
        target_profile="tgt", target_uuid="t1", status="ok",
    )

    client = MagicMock()
    client.get_json = AsyncMock(side_effect=EndpointChanged("missing"))
    client.put_json = AsyncMock()

    import claude_migrate.restore as restore_mod
    monkeypatch.setattr(restore_mod, "Pacer", lambda **kw: _NopPacer())

    touched, _missing, errors = await reorder_conversations(
        client, db_conn, "tgt-org", state, dry_run=False,
    )

    assert touched == 0
    assert len(errors) == 1
    assert "EndpointChanged" in errors[0][1] or "missing" in errors[0][1]
