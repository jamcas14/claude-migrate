"""Worker-level tests: error propagation contracts for the restore pipeline."""

from __future__ import annotations

import sqlite3
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from claude_migrate.errors import (
    AuthExpired,
    ClientVersionStale,
    CloudflareChallenge,
    EndpointChanged,
    NetworkError,
    RateLimited,
    TLSReject,
)
from claude_migrate.restore import (
    _cleanup_partial,
    delete_conversation,
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


async def test_restore_profile_prefs_filters_internal_settings_keys(
    db_conn: sqlite3.Connection,
) -> None:
    """The user's log shows claude.ai 400s on `internal_*` keys nested inside
    `settings`. Top-level filter wouldn't catch them. Per-PUT body must have
    those nested keys removed."""
    upsert_org(db_conn, {"uuid": "o1", "name": "Test"})
    upsert_account(db_conn, "o1", {
        "full_name": "User",
        "settings": {
            "theme": "dark",
            "internal_melange_store_id": "abc",
            "internal_tier_org_type": "pro",
            "preferred_model": "claude-sonnet-4-6",
        },
    })

    captured: dict[str, object] = {}

    async def fake_put(path: str, *, body: dict[str, object], **kw: object) -> dict[str, object]:
        captured["body"] = body
        return {"ok": True}

    client = MagicMock()
    client.put_json = AsyncMock(side_effect=fake_put)

    result = await restore_profile_prefs(client, db_conn, dry_run=False)
    assert result is True
    body = captured["body"]
    assert isinstance(body, dict)
    settings = body.get("settings")
    assert isinstance(settings, dict)
    # Allowed keys passed through.
    assert "theme" in settings
    assert "preferred_model" in settings
    # Internal keys filtered out.
    for k in settings:
        assert not k.startswith("internal_"), (
            f"internal key {k!r} must be filtered out of settings"
        )


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


# ---------------------------------------------------------------------------
# _cleanup_partial / delete_conversation — must NOT escape on retryable
# errors during a failed-cleanup-of-a-failed-op cascade.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        RateLimited("429 again"),
        ClientVersionStale("stale headers"),
        EndpointChanged("404"),
        NetworkError("network down"),
        AuthExpired("session ended"),
        CloudflareChallenge("blocked"),
        TLSReject("fingerprint reject"),
    ],
)
async def test_cleanup_partial_swallows_every_typed_error(exc: Exception) -> None:
    """_cleanup_partial is best-effort; nothing it does should be allowed to
    escape and replace the in-flight exception its caller is handling."""
    client = MagicMock()
    client.request = AsyncMock(side_effect=exc)
    # Must NOT raise.
    await _cleanup_partial(client, "tgt-org", "new-uuid")


async def test_cleanup_partial_no_op_when_no_uuid() -> None:
    """If the create call never returned a uuid, there's nothing to clean up."""
    client = MagicMock()
    client.request = AsyncMock(side_effect=AssertionError("should not be called"))
    await _cleanup_partial(client, "tgt-org", None)


@pytest.mark.parametrize(
    ("exc", "expect_rate_limited"),
    [
        (RateLimited("429"), True),
        (ClientVersionStale("stale"), False),
        (AuthExpired("session ended"), False),
        (CloudflareChallenge("blocked"), False),
        (TLSReject("fingerprint"), False),
    ],
)
async def test_delete_conversation_returns_outcome_on_typed_errors(
    exc: Exception, expect_rate_limited: bool,
) -> None:
    """cleanup CLI's per-orphan loop relies on the WorkerOutcome shape; the
    rate_limited flag drives Pacer cooldown so sustained 429s actually
    back off instead of burning through every orphan."""
    client = MagicMock()
    client.request = AsyncMock(side_effect=exc)
    outcome = await delete_conversation(client, "tgt-org", "uuid")
    assert outcome.target_uuid is None
    assert outcome.rate_limited is expect_rate_limited
    assert outcome.error is not None


async def test_delete_conversation_returns_ok_on_success() -> None:
    client = MagicMock()
    client.request = AsyncMock(return_value=None)
    outcome = await delete_conversation(client, "tgt-org", "uuid-X")
    assert outcome.target_uuid == "uuid-X"
    assert outcome.error is None
    assert outcome.rate_limited is False


# ---------------------------------------------------------------------------
# _upload_attachment status mapping (issue 2.3 from round-5 review).
# ---------------------------------------------------------------------------


class _FakeUploadResp:
    def __init__(self, status: int, body: bytes = b"") -> None:
        self.status_code = status
        self.content = body


class _FakeUploadSession:
    def __init__(self, resp: _FakeUploadResp) -> None:
        self._resp = resp

    async def post(self, url: str, **kw: Any) -> _FakeUploadResp:
        return self._resp

    async def close(self) -> None: ...


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (401, b"", AuthExpired),
        (429, b"", RateLimited),
        (404, b"", EndpointChanged),
        (400, b"<html>cf</html>", ClientVersionStale),
        (403, b"<title>Just a moment...</title>", CloudflareChallenge),
        (403, b"forbidden", TLSReject),
        (500, b"", NetworkError),
    ],
)
async def test_upload_status_mapped_to_typed_error(
    status: int, body: bytes, expected: type[Exception],
) -> None:
    """_upload_attachment used to flatten everything to NetworkError; now it
    routes through map_status_to_typed_error so the orchestrator sees the
    same typed errors the rest of the codebase expects."""
    from claude_migrate.client import ClaudeClient, Credentials
    from claude_migrate.config import load_settings
    from claude_migrate.transport import _upload_attachment

    client = ClaudeClient(
        Credentials(session_key="sk-ant-sid01-X", cf_clearance="cf-X"),
        load_settings(),
    )
    client._session = _FakeUploadSession(_FakeUploadResp(status, body))
    with pytest.raises(expected):
        await _upload_attachment(client, "org", "f.md", "content")
