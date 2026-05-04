"""Tests for `--bookmark` and `claude-migrate load`.

Covers the bookmark phase (empty named stub creation, idempotency, cascade
abort, status='bookmarked' migration_log writes) and the load phase (pattern
filtering, refusal on non-empty target, transcript paste through the existing
transport, name renaming on success, migration_log status flip).

No network — every external surface is stubbed via AsyncMock. The Pacer is
replaced with a no-op so tests don't actually sleep.
"""

from __future__ import annotations

import sqlite3
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from claude_migrate.bookmark import (
    UNLOADED_PREFIX_RE,
    BookmarkSummary,
    LoadCandidate,
    _bookmark_chat_name,
    _filter_candidates,
    _loaded_chat_name,
    load_bookmarks,
    restore_bookmarks,
)
from claude_migrate.errors import (
    AuthExpired,
    RateLimited,
)
from claude_migrate.state import RestoreState
from claude_migrate.store import (
    log_migration,
    upsert_conversation,
    upsert_message,
    upsert_org,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_conversation(
    conn: sqlite3.Connection, uuid: str, title: str = "A chat", *,
    created_at: str = "2026-03-15T12:00:00Z",
) -> None:
    """Seed a conversation + one message so the loader's render_transcript
    has content to render. Same shape as fetch.py upserts in production."""
    upsert_conversation(conn, "o1", {
        "uuid": uuid, "name": title,
        "created_at": created_at, "updated_at": created_at,
    })
    upsert_message(conn, uuid, {
        "uuid": f"msg-{uuid}",
        "parent_message_uuid": None,
        "sender": "human", "index": 0,
        "created_at": created_at,
        "content": [{"type": "text", "text": f"hello in {title}"}],
    })


class _NopPacer:
    """Pacer replacement: no sleeping, but counts cascading 429s so the
    cascade-abort branch can be exercised."""

    def __init__(self, **kw: object) -> None:
        self._consecutive_rate_limits = 0

    @property
    def consecutive_rate_limits(self) -> int:
        return self._consecutive_rate_limits

    async def before(self) -> None:
        return None

    async def after(self, outcome: object) -> None:
        if outcome is None:
            return
        if getattr(outcome, "rate_limited", False):
            self._consecutive_rate_limits += 1
        elif getattr(outcome, "target_uuid", None):
            self._consecutive_rate_limits = 0


# ---------------------------------------------------------------------------
# Name builders
# ---------------------------------------------------------------------------


def test_bookmark_name_format() -> None:
    """`[ul|YYYY-MM-DD] Title` — compact stub indicator + date in one
    bracket-pair. ~16 chars of prefix vs `[unloaded] [YYYY-MM-DD]` (~24)."""
    assert _bookmark_chat_name("My chat", "2026-03-15") == "[ul|2026-03-15] My chat"


def test_bookmark_name_handles_empty_title_and_date() -> None:
    """Empty source title becomes `(untitled)`. Empty date drops the date
    portion entirely so the prefix doesn't show a confusing `[ul|]`."""
    assert _bookmark_chat_name("", "2026-03-15") == "[ul|2026-03-15] (untitled)"
    assert _bookmark_chat_name("Foo", "") == "[ul] Foo"


def test_unloaded_prefix_re_matches_both_formats() -> None:
    """Detector regex must accept the current `[ul|...]` shape AND the
    no-date fallback `[ul]` so maintenance scripts can identify any stub."""
    assert UNLOADED_PREFIX_RE.match("[ul|2026-03-15] Title")
    assert UNLOADED_PREFIX_RE.match("[ul] Title")
    assert not UNLOADED_PREFIX_RE.match("[2026-03-15] Title")
    assert not UNLOADED_PREFIX_RE.match("Random title with [ul] in middle")


def test_loaded_chat_name_matches_default_mode_format() -> None:
    """Names applied AFTER successful load match `[YYYY-MM-DD] Title` —
    the same shape default-mode `migrate` uses, so loaded-from-bookmark
    chats are visually indistinguishable from default-mode migrations."""
    assert _loaded_chat_name("My chat", "2026-03-15") == "[2026-03-15] My chat"
    assert _loaded_chat_name("", "2026-03-15") == "[2026-03-15] (untitled)"
    # No date → no bracket prefix.
    assert _loaded_chat_name("Foo", "") == "Foo"


# ---------------------------------------------------------------------------
# Pattern filtering
# ---------------------------------------------------------------------------


def _candidate(src: str = "src-1", tgt: str = "tgt-aaaaaaaa", title: str = "A chat") -> LoadCandidate:
    return LoadCandidate(source_uuid=src, target_uuid=tgt, title=title)


def test_filter_load_all_returns_everything() -> None:
    cs = [_candidate(title="Foo"), _candidate(title="Bar")]
    assert _filter_candidates(cs, pattern=None, load_all=True) == cs


def test_filter_no_pattern_returns_everything() -> None:
    """Without --all and without a pattern, everything is returned — the
    CLI then defers to the interactive picker for selection."""
    cs = [_candidate(title="Foo"), _candidate(title="Bar")]
    assert _filter_candidates(cs, pattern=None, load_all=False) == cs


def test_filter_substring_case_insensitive() -> None:
    cs = [
        _candidate(title="React hooks deep dive"),
        _candidate(title="Random other chat"),
        _candidate(title="REACT context API"),
    ]
    out = _filter_candidates(cs, pattern="react", load_all=False)
    assert len(out) == 2
    assert all("react" in c.title.lower() for c in out)


def test_filter_uuid_prefix_matches_target_or_source() -> None:
    """A pattern matching `^[0-9a-f]{6,}$` (real UUIDs are hex) is treated
    as a UUID prefix lookup against target_uuid OR source_uuid — the URL bar
    in claude.ai shows a UUID, so paste-from-URL should just work."""
    cs = [
        _candidate(src="abc12345", tgt="def56789", title="Foo"),
        _candidate(src="aaa11111", tgt="bbb22222", title="Bar"),
    ]
    # Match by target uuid prefix (the common case — user copies from URL bar).
    out = _filter_candidates(cs, pattern="def567", load_all=False)
    assert len(out) == 1
    assert out[0].title == "Foo"
    # Match by source uuid prefix (less useful but supported — `claude-migrate
    # status` shows source uuids).
    out = _filter_candidates(cs, pattern="aaa111", load_all=False)
    assert len(out) == 1
    assert out[0].title == "Bar"
    # Non-hex pattern of similar length falls through to the title substring
    # branch (verifies the regex isn't matching too greedily).
    out = _filter_candidates(cs, pattern="foo", load_all=False)
    assert len(out) == 1
    assert out[0].title == "Foo"


def test_filter_no_match_returns_empty() -> None:
    cs = [_candidate(title="Foo")]
    assert _filter_candidates(cs, pattern="nonexistent", load_all=False) == []


def test_filter_extracts_uuid_from_full_url() -> None:
    """The fastest user flow: paste the chat URL straight from the browser
    bar. The matcher pulls the UUID out of the URL and exact-matches against
    target_uuid (or source_uuid). No trimming required from the user."""
    cs = [
        _candidate(
            src="aaaaaaaa-1111-2222-3333-444444444444",
            tgt="fcc92573-75de-4ee6-9f03-6b4f4fd98214",
            title="Memeable",
        ),
        _candidate(
            src="bbbbbbbb-1111-2222-3333-444444444444",
            tgt="11111111-2222-3333-4444-555555555555",
            title="Other",
        ),
    ]
    out = _filter_candidates(
        cs,
        pattern="https://claude.ai/chat/fcc92573-75de-4ee6-9f03-6b4f4fd98214",
        load_all=False,
    )
    assert len(out) == 1
    assert out[0].title == "Memeable"


def test_filter_extracts_full_bare_uuid() -> None:
    """Paste of just a UUID (no URL wrapper) also exact-matches via the
    in-text extraction path. Subtly different from the bare-prefix case
    because a full-uuid match is exact, not prefix."""
    cs = [
        _candidate(tgt="fcc92573-75de-4ee6-9f03-6b4f4fd98214", title="Bullseye"),
        _candidate(tgt="fcc92573-aaaa-bbbb-cccc-dddddddddddd", title="Same prefix"),
    ]
    out = _filter_candidates(
        cs,
        pattern="fcc92573-75de-4ee6-9f03-6b4f4fd98214",
        load_all=False,
    )
    # Exact match: only the chat with the matching FULL uuid, not the same-
    # prefix sibling.
    assert len(out) == 1
    assert out[0].title == "Bullseye"


def test_filter_handles_empty_string_pattern() -> None:
    """An empty string is treated like no-pattern (returns everything) so a
    user accidentally pressing Enter at the prompt doesn't error out."""
    cs = [_candidate(title="Foo"), _candidate(title="Bar")]
    assert _filter_candidates(cs, pattern="", load_all=False) == cs
    assert _filter_candidates(cs, pattern="   ", load_all=False) == cs


# ---------------------------------------------------------------------------
# Bookmark phase
# ---------------------------------------------------------------------------


async def test_bookmark_creates_one_empty_chat_per_source(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: N source chats → N POSTs to /chat_conversations with the
    [unloaded] prefix. migration_log marks each row as status='bookmarked'."""
    upsert_org(db_conn, {"uuid": "o1", "name": "Test"})
    _seed_conversation(db_conn, "src-1", "First chat")
    _seed_conversation(db_conn, "src-2", "Second chat")

    state = RestoreState(db_conn, "tgt")
    summary = BookmarkSummary()

    client = MagicMock()
    client.post_json = AsyncMock(side_effect=[
        {"uuid": "tgt-chat-1"},
        {"uuid": "tgt-chat-2"},
    ])

    import claude_migrate.bookmark as bookmark_mod
    monkeypatch.setattr(bookmark_mod, "Pacer", _NopPacer)

    await restore_bookmarks(
        client, db_conn, "tgt-org", state,
        dry_run=False, summary=summary,
    )

    assert summary.conversations_total == 2
    assert summary.conversations_bookmarked == 2
    assert summary.failures == []
    assert client.post_json.call_count == 2

    # Each POST was to /chat_conversations with name carrying the unloaded
    # marker (matches the bookmark-stub detector regex).
    posts = client.post_json.call_args_list
    for call in posts:
        assert call.args[0].endswith("/chat_conversations")
        assert UNLOADED_PREFIX_RE.match(call.kwargs["body"]["name"])
        # No project_uuid, no transcript.
        assert "project_uuid" not in call.kwargs["body"]

    # migration_log shape: status='bookmarked' (not 'ok').
    row1 = db_conn.execute(
        "SELECT target_uuid, status FROM migration_log WHERE source_uuid='src-1'",
    ).fetchone()
    assert row1["target_uuid"] == "tgt-chat-1"
    assert row1["status"] == "bookmarked"

    # already_migrated (status='ok' filter) returns None for bookmarked rows.
    assert state.already_migrated("src-1") is None
    # already_bookmarked finds them.
    assert state.already_bookmarked("src-1") == "tgt-chat-1"


async def test_bookmark_rerun_skips_existing_stubs(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second --bookmark run after a successful one must skip every chat —
    no POSTs at all."""
    upsert_org(db_conn, {"uuid": "o1", "name": "Test"})
    _seed_conversation(db_conn, "src-1")
    state = RestoreState(db_conn, "tgt")
    state.mark_bookmarked(source_uuid="src-1", target_uuid="tgt-chat-1")

    summary = BookmarkSummary()
    client = MagicMock()
    client.post_json = AsyncMock()

    import claude_migrate.bookmark as bookmark_mod
    monkeypatch.setattr(bookmark_mod, "Pacer", _NopPacer)

    await restore_bookmarks(
        client, db_conn, "tgt-org", state,
        dry_run=False, summary=summary,
    )

    assert summary.skipped == 1
    assert summary.conversations_bookmarked == 0
    assert client.post_json.call_count == 0


async def test_bookmark_rerun_skips_already_loaded_chats(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mixing modes: a chat already loaded via default `migrate` (status='ok')
    must not be re-bookmarked. Without this, --bookmark would create a
    duplicate empty stub for every loaded chat."""
    upsert_org(db_conn, {"uuid": "o1", "name": "Test"})
    _seed_conversation(db_conn, "src-1")
    log_migration(
        db_conn, source_uuid="src-1", object_type="conversation",
        target_profile="tgt", target_uuid="tgt-loaded-1", status="ok",
    )

    state = RestoreState(db_conn, "tgt")
    summary = BookmarkSummary()
    client = MagicMock()
    client.post_json = AsyncMock()

    import claude_migrate.bookmark as bookmark_mod
    monkeypatch.setattr(bookmark_mod, "Pacer", _NopPacer)

    await restore_bookmarks(
        client, db_conn, "tgt-org", state,
        dry_run=False, summary=summary,
    )

    assert summary.skipped == 1
    assert client.post_json.call_count == 0


async def test_bookmark_cascade_aborts_on_repeated_429(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """5 consecutive RateLimited outcomes with no successes between → run
    bails out cleanly. Same contract as the conversation-restore phase."""
    upsert_org(db_conn, {"uuid": "o1", "name": "Test"})
    for i in range(10):
        _seed_conversation(db_conn, f"src-{i}", title=f"Chat {i}")
    state = RestoreState(db_conn, "tgt")
    summary = BookmarkSummary()

    client = MagicMock()
    client.post_json = AsyncMock(
        side_effect=RateLimited("429", retry_after_sec=10.0),
    )

    import claude_migrate.bookmark as bookmark_mod
    monkeypatch.setattr(bookmark_mod, "Pacer", _NopPacer)

    await restore_bookmarks(
        client, db_conn, "tgt-org", state,
        dry_run=False, summary=summary,
    )

    assert summary.cascade_aborted is True
    # Loop checks BEFORE each chat, so 5 attempts max.
    assert len(summary.failures) <= 5


async def test_bookmark_session_fatal_propagates(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AuthExpired must bubble out so the orchestrator can surface re-auth.
    No partial state to clean up — bookmark mode doesn't create projects."""
    upsert_org(db_conn, {"uuid": "o1", "name": "Test"})
    _seed_conversation(db_conn, "src-1")
    state = RestoreState(db_conn, "tgt")
    summary = BookmarkSummary()

    client = MagicMock()
    client.post_json = AsyncMock(side_effect=AuthExpired("session expired"))

    import claude_migrate.bookmark as bookmark_mod
    monkeypatch.setattr(bookmark_mod, "Pacer", _NopPacer)

    with pytest.raises(AuthExpired):
        await restore_bookmarks(
            client, db_conn, "tgt-org", state,
            dry_run=False, summary=summary,
        )


# ---------------------------------------------------------------------------
# Load phase
# ---------------------------------------------------------------------------


async def test_load_pastes_transcript_and_renames(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
    tmp_data_dir: Any,
) -> None:
    """Single-chat load: GET target → render → send_payload → PUT name →
    migration_log flips bookmarked → ok. The new name uses default-mode's
    `[YYYY-MM-DD] Title` shape, reconstructed from the source archive
    (NOT by stripping the prefix from the bookmark title — bookmark titles
    no longer carry the date)."""
    upsert_org(db_conn, {"uuid": "o1", "name": "Test"})
    _seed_conversation(db_conn, "src-1", "Real title")
    state = RestoreState(db_conn, "tgt")
    state.mark_bookmarked(source_uuid="src-1", target_uuid="tgt-chat-1")

    # Stub the session: open_session yields a SimpleNamespace-like context
    # manager bound to a faked client + org_uuid.
    client = MagicMock()
    client.get_json = AsyncMock(return_value={
        "uuid": "tgt-chat-1",
        "name": "[unloaded] Real title",
        "chat_messages": [],
    })
    client.put_json = AsyncMock(return_value={"ok": True})

    payload_calls: list[tuple[str, str]] = []

    async def fake_send_payload(
        client_arg: object, target_org: str,
        conv_uuid: str, payload: object,
    ) -> None:
        payload_calls.append((target_org, conv_uuid))

    import claude_migrate.bookmark as bookmark_mod
    monkeypatch.setattr(bookmark_mod, "Pacer", _NopPacer)
    monkeypatch.setattr(bookmark_mod, "send_payload", fake_send_payload)
    _patch_open_session(monkeypatch, bookmark_mod, client=client, org_uuid="tgt-org")

    cands = [LoadCandidate(
        source_uuid="src-1", target_uuid="tgt-chat-1", title="Real title",
    )]
    summary = await load_bookmarks("tgt", cands, force=False)

    assert summary.matched == 1
    assert summary.loaded == 1
    assert payload_calls == [("tgt-org", "tgt-chat-1")]

    # Rename: PUT with the default-mode `[YYYY-MM-DD] Title` shape.
    put_call = client.put_json.call_args_list[-1]
    assert put_call.kwargs["body"]["name"] == "[2026-03-15] Real title"

    # migration_log flipped bookmarked → ok.
    row = db_conn.execute(
        "SELECT status FROM migration_log WHERE source_uuid='src-1'",
    ).fetchone()
    assert row["status"] == "ok"


async def test_load_bookmarks_only_loads_provided_candidates(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
    tmp_data_dir: Any,
) -> None:
    """Regression: a CLI-side bug previously made `load_bookmarks` ignore the
    pre-filtered list and load every bookmark on the profile instead. The
    fix splits selection (CLI's job) from loading (this function's job) —
    `load_bookmarks` MUST load exactly the candidates passed in, no more."""
    upsert_org(db_conn, {"uuid": "o1", "name": "Test"})
    _seed_conversation(db_conn, "src-1", "First")
    _seed_conversation(db_conn, "src-2", "Second")
    _seed_conversation(db_conn, "src-3", "Third")
    state = RestoreState(db_conn, "tgt")
    state.mark_bookmarked(source_uuid="src-1", target_uuid="tgt-1")
    state.mark_bookmarked(source_uuid="src-2", target_uuid="tgt-2")
    state.mark_bookmarked(source_uuid="src-3", target_uuid="tgt-3")

    client = MagicMock()
    client.get_json = AsyncMock(return_value={
        "uuid": "tgt-2",
        "name": "[unloaded] Second",
        "chat_messages": [],
    })
    client.put_json = AsyncMock(return_value={"ok": True})

    sent: list[str] = []

    async def fake_send_payload(
        client_arg: object, target_org: str,
        conv_uuid: str, payload: object,
    ) -> None:
        sent.append(conv_uuid)

    import claude_migrate.bookmark as bookmark_mod
    monkeypatch.setattr(bookmark_mod, "Pacer", _NopPacer)
    monkeypatch.setattr(bookmark_mod, "send_payload", fake_send_payload)
    _patch_open_session(monkeypatch, bookmark_mod, client=client, org_uuid="tgt-org")

    only_second = [LoadCandidate(
        source_uuid="src-2", target_uuid="tgt-2", title="Second",
    )]
    summary = await load_bookmarks("tgt", only_second, force=False)

    # Only the one passed in was loaded — the other two bookmarks are
    # untouched even though they exist in migration_log.
    assert summary.matched == 1
    assert summary.loaded == 1
    assert sent == ["tgt-2"]


async def test_load_bookmarks_default_loads_all_when_no_list_given(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
    tmp_data_dir: Any,
) -> None:
    """`candidates=None` (default) loads every bookmark on the profile —
    this is what the CLI's `--all` flag delegates to. Verifies the
    explicit-list path doesn't silently change the no-list semantics."""
    upsert_org(db_conn, {"uuid": "o1", "name": "Test"})
    _seed_conversation(db_conn, "src-1", "Alpha")
    _seed_conversation(db_conn, "src-2", "Beta")
    state = RestoreState(db_conn, "tgt")
    state.mark_bookmarked(source_uuid="src-1", target_uuid="tgt-1")
    state.mark_bookmarked(source_uuid="src-2", target_uuid="tgt-2")

    client = MagicMock()
    # Both GETs return an empty chat for the iteration.
    client.get_json = AsyncMock(side_effect=[
        {"uuid": "tgt-1", "name": "[unloaded] Alpha", "chat_messages": []},
        {"uuid": "tgt-2", "name": "[unloaded] Beta", "chat_messages": []},
    ])
    client.put_json = AsyncMock(return_value={"ok": True})

    sent: list[str] = []

    async def fake_send_payload(
        client_arg: object, target_org: str,
        conv_uuid: str, payload: object,
    ) -> None:
        sent.append(conv_uuid)

    import claude_migrate.bookmark as bookmark_mod
    monkeypatch.setattr(bookmark_mod, "Pacer", _NopPacer)
    monkeypatch.setattr(bookmark_mod, "send_payload", fake_send_payload)
    _patch_open_session(monkeypatch, bookmark_mod, client=client, org_uuid="tgt-org")

    summary = await load_bookmarks("tgt", force=False)

    assert summary.matched == 2
    assert summary.loaded == 2
    # Order matches `_resolve_candidates` (sorted by title.lower()).
    assert sent == ["tgt-1", "tgt-2"]


async def test_load_refuses_non_empty_chat_without_force(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
    tmp_data_dir: Any,
) -> None:
    """If the user typed a question into the empty stub before running load,
    appending the transcript would leave a confusing chat history. Refuse,
    surface a clear error, leave migration_log untouched."""
    upsert_org(db_conn, {"uuid": "o1", "name": "Test"})
    _seed_conversation(db_conn, "src-1", "Some chat")
    state = RestoreState(db_conn, "tgt")
    state.mark_bookmarked(source_uuid="src-1", target_uuid="tgt-chat-1")

    client = MagicMock()
    client.get_json = AsyncMock(return_value={
        "uuid": "tgt-chat-1",
        "name": "[unloaded] Some chat",
        "chat_messages": [
            {"uuid": "u1", "sender": "human", "content": [{"type": "text", "text": "premature"}]},
        ],
    })
    client.put_json = AsyncMock()

    sent: list[object] = []

    async def fake_send_payload(*a: object, **kw: object) -> None:
        sent.append((a, kw))

    import claude_migrate.bookmark as bookmark_mod
    monkeypatch.setattr(bookmark_mod, "Pacer", _NopPacer)
    monkeypatch.setattr(bookmark_mod, "send_payload", fake_send_payload)
    _patch_open_session(monkeypatch, bookmark_mod, client=client, org_uuid="tgt-org")

    cands = [LoadCandidate(
        source_uuid="src-1", target_uuid="tgt-chat-1", title="Some chat",
    )]
    summary = await load_bookmarks("tgt", cands, force=False)

    assert summary.loaded == 0
    assert summary.skipped_non_empty == 1
    assert summary.failures
    assert "non_empty" in summary.failures[0][1]
    # No paste, no rename.
    assert sent == []
    assert client.put_json.call_count == 0
    # migration_log untouched.
    row = db_conn.execute(
        "SELECT status FROM migration_log WHERE source_uuid='src-1'",
    ).fetchone()
    assert row["status"] == "bookmarked"


async def test_load_force_loads_into_non_empty_chat(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
    tmp_data_dir: Any,
) -> None:
    """--force overrides the non-empty check. Transcript pastes, migration_log
    flips. The user has explicitly asked for the awkward outcome."""
    upsert_org(db_conn, {"uuid": "o1", "name": "Test"})
    _seed_conversation(db_conn, "src-1", "Some chat")
    state = RestoreState(db_conn, "tgt")
    state.mark_bookmarked(source_uuid="src-1", target_uuid="tgt-chat-1")

    client = MagicMock()
    client.get_json = AsyncMock(return_value={
        "uuid": "tgt-chat-1",
        "name": "[unloaded] Some chat",
        "chat_messages": [
            {"uuid": "u1", "sender": "human", "content": [{"type": "text", "text": "premature"}]},
        ],
    })
    client.put_json = AsyncMock(return_value={"ok": True})

    sent: list[tuple[object, ...]] = []

    async def fake_send_payload(*a: object, **kw: object) -> None:
        sent.append(a)

    import claude_migrate.bookmark as bookmark_mod
    monkeypatch.setattr(bookmark_mod, "Pacer", _NopPacer)
    monkeypatch.setattr(bookmark_mod, "send_payload", fake_send_payload)
    _patch_open_session(monkeypatch, bookmark_mod, client=client, org_uuid="tgt-org")

    cands = [LoadCandidate(
        source_uuid="src-1", target_uuid="tgt-chat-1", title="Some chat",
    )]
    summary = await load_bookmarks("tgt", cands, force=True)

    assert summary.loaded == 1
    assert len(sent) == 1


async def test_load_empty_candidates_is_a_noop(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
    tmp_data_dir: Any,
) -> None:
    """An empty candidates list returns an empty summary without any network
    calls. The CLI's `_filter_candidates` returns an empty list when a
    pattern matches nothing, and the load step must do nothing in that case."""
    upsert_org(db_conn, {"uuid": "o1", "name": "Test"})
    _seed_conversation(db_conn, "src-1", "Some chat")
    state = RestoreState(db_conn, "tgt")
    state.mark_bookmarked(source_uuid="src-1", target_uuid="tgt-chat-1")

    client = MagicMock()
    client.get_json = AsyncMock()
    client.put_json = AsyncMock()

    import claude_migrate.bookmark as bookmark_mod
    monkeypatch.setattr(bookmark_mod, "Pacer", _NopPacer)
    _patch_open_session(monkeypatch, bookmark_mod, client=client, org_uuid="tgt-org")

    summary = await load_bookmarks("tgt", [], force=False)

    assert summary.matched == 0
    assert summary.loaded == 0
    assert client.get_json.call_count == 0


async def test_load_handles_404_target_chat_gracefully(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
    tmp_data_dir: Any,
) -> None:
    """User deleted the bookmarked chat from claude.ai's UI between bookmark
    and load — GET returns 404 (EndpointChanged). Surface a clear error and
    point at `verify --reconcile` rather than crashing."""
    from claude_migrate.errors import EndpointChanged

    upsert_org(db_conn, {"uuid": "o1", "name": "Test"})
    _seed_conversation(db_conn, "src-1", "Vanished")
    state = RestoreState(db_conn, "tgt")
    state.mark_bookmarked(source_uuid="src-1", target_uuid="tgt-chat-1")

    client = MagicMock()
    client.get_json = AsyncMock(side_effect=EndpointChanged("404"))

    import claude_migrate.bookmark as bookmark_mod
    monkeypatch.setattr(bookmark_mod, "Pacer", _NopPacer)
    _patch_open_session(monkeypatch, bookmark_mod, client=client, org_uuid="tgt-org")

    cands = [LoadCandidate(
        source_uuid="src-1", target_uuid="tgt-chat-1", title="Vanished",
    )]
    summary = await load_bookmarks("tgt", cands, force=False)

    assert summary.loaded == 0
    assert summary.failures
    assert "verify" in summary.failures[0][1]


# ---------------------------------------------------------------------------
# Cleanup-skip protection extends to bookmarked uuids
# ---------------------------------------------------------------------------


async def test_cleanup_protected_uuids_includes_bookmarked(
    db_conn: sqlite3.Connection,
) -> None:
    """`state.all_migrated_target_uuids` returns target_uuids for BOTH
    status='ok' and status='bookmarked' rows. This is what cleanup uses to
    refuse deletion — bookmarked stubs are intentionally empty and must
    survive a stray --since run."""
    state = RestoreState(db_conn, "tgt")
    state.mark_bookmarked(source_uuid="src-1", target_uuid="tgt-bookmarked")
    log_migration(
        db_conn, source_uuid="src-2", object_type="conversation",
        target_profile="tgt", target_uuid="tgt-loaded", status="ok",
    )

    protected = state.all_migrated_target_uuids()
    assert protected == {"tgt-bookmarked", "tgt-loaded"}


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------


def _patch_open_session(
    monkeypatch: pytest.MonkeyPatch,
    module: object,
    *,
    client: object,
    org_uuid: str,
) -> None:
    """Replace `open_session` in `module` with a stub yielding a session
    object that has the right shape (`client`, `org_uuid`). Lets tests
    exercise `load_bookmarks` end-to-end without the real auth flow."""
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    @asynccontextmanager
    async def fake_open_session(profile: str, **kw: object) -> Any:
        yield SimpleNamespace(client=client, org_uuid=org_uuid, email="x@y", org_name="Org")

    monkeypatch.setattr(module, "open_session", fake_open_session)
