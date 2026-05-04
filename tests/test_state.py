"""Tests for RestoreState — the migration_log projection per (conn, profile)."""

from __future__ import annotations

import sqlite3

from claude_migrate.state import RestoreState
from claude_migrate.store import upsert_conversation, upsert_project, upsert_style


def test_already_migrated_round_trip(db_conn: sqlite3.Connection) -> None:
    state = RestoreState(db_conn, "t")
    assert state.already_migrated("c1") is None
    state.mark_ok(source_uuid="c1", object_type="conversation", target_uuid="tgt-c1")
    assert state.already_migrated("c1") == "tgt-c1"


def test_mark_error_then_mark_ok_overwrites(db_conn: sqlite3.Connection) -> None:
    """Composite-PK semantics: a successful retry replaces the prior error row."""
    state = RestoreState(db_conn, "t")
    state.mark_error(source_uuid="c1", object_type="conversation", error="boom")
    assert state.already_migrated("c1") is None  # error rows don't count as ok
    state.mark_ok(source_uuid="c1", object_type="conversation", target_uuid="tgt-c1")
    assert state.already_migrated("c1") == "tgt-c1"
    failures = state.recent_failures()
    assert failures == [], "ok overwrites the prior error row"


def test_project_map_only_returns_ok_with_target_uuid(db_conn: sqlite3.Connection) -> None:
    state = RestoreState(db_conn, "t")
    state.mark_ok(source_uuid="p1", object_type="project", target_uuid="tgt-p1")
    state.mark_ok(source_uuid="p2", object_type="project", target_uuid="tgt-p2")
    state.mark_error(source_uuid="p3", object_type="project", error="boom")
    state.mark_ok(source_uuid="s1", object_type="style", target_uuid="tgt-s1")
    assert state.project_map() == {"p1": "tgt-p1", "p2": "tgt-p2"}


def test_project_map_isolates_by_profile(db_conn: sqlite3.Connection) -> None:
    alpha = RestoreState(db_conn, "alpha")
    beta = RestoreState(db_conn, "beta")
    alpha.mark_ok(source_uuid="p1", object_type="project", target_uuid="alpha-p1")
    beta.mark_ok(source_uuid="p1", object_type="project", target_uuid="beta-p1")
    assert alpha.project_map() == {"p1": "alpha-p1"}
    assert beta.project_map() == {"p1": "beta-p1"}


def test_pending_count_excludes_already_migrated(db_conn: sqlite3.Connection) -> None:
    upsert_project(db_conn, "o1", {"uuid": "p1", "name": "First"})
    upsert_project(db_conn, "o1", {"uuid": "p2", "name": "Second"})
    upsert_project(db_conn, "o1", {"uuid": "p3", "name": "Third"})
    state = RestoreState(db_conn, "t")
    state.mark_ok(source_uuid="p1", object_type="project", target_uuid="x")
    state.mark_error(source_uuid="p2", object_type="project", error="boom")
    # p1 ok → not pending; p2 error → still pending; p3 untouched → pending.
    assert state.pending_count("project") == 2


def test_migrated_count_isolates_by_object_type(db_conn: sqlite3.Connection) -> None:
    state = RestoreState(db_conn, "t")
    state.mark_ok(source_uuid="c1", object_type="conversation", target_uuid="x")
    state.mark_ok(source_uuid="c2", object_type="conversation", target_uuid="y")
    state.mark_ok(source_uuid="p1", object_type="project", target_uuid="z")
    assert state.migrated_count("conversation") == 2
    assert state.migrated_count("project") == 1
    assert state.migrated_count("style") == 0


def test_recent_failures_orders_by_migrated_at_desc(db_conn: sqlite3.Connection) -> None:
    state = RestoreState(db_conn, "t")
    upsert_style(db_conn, "o1", {"uuid": "s1", "name": "S1"})
    upsert_style(db_conn, "o1", {"uuid": "s2", "name": "S2"})
    state.mark_error(source_uuid="s1", object_type="style", error="first")
    state.mark_error(source_uuid="s2", object_type="style", error="second")
    rows = state.recent_failures(limit=10)
    # Newer error appears first.
    assert [r["source_uuid"] for r in rows] == ["s2", "s1"]
    assert rows[0]["error"] == "second"


def test_last_activity_returns_most_recent(db_conn: sqlite3.Connection) -> None:
    state = RestoreState(db_conn, "t")
    assert state.last_activity() is None
    state.mark_ok(source_uuid="c1", object_type="conversation", target_uuid="x")
    state.mark_error(source_uuid="c2", object_type="conversation", error="boom")
    last = state.last_activity()
    assert last is not None
    assert last["status"] == "error"


def test_drop_removes_row(db_conn: sqlite3.Connection) -> None:
    state = RestoreState(db_conn, "t")
    state.mark_ok(source_uuid="c1", object_type="conversation", target_uuid="x")
    state.drop("c1")
    assert state.already_migrated("c1") is None


def test_confirmed_conversations_excludes_other_types(db_conn: sqlite3.Connection) -> None:
    state = RestoreState(db_conn, "t")
    state.mark_ok(source_uuid="c1", object_type="conversation", target_uuid="tc1")
    state.mark_ok(source_uuid="p1", object_type="project", target_uuid="tp1")
    state.mark_error(source_uuid="c2", object_type="conversation", error="x")
    pairs = state.confirmed_conversations()
    assert pairs == [("c1", "tc1")]


def test_empty_target_uuid_excluded_from_confirmed(db_conn: sqlite3.Connection) -> None:
    """Defensive: a target_uuid=NULL row should not appear in confirmed_conversations."""
    state = RestoreState(db_conn, "t")
    state.mark_ok(source_uuid="c1", object_type="conversation", target_uuid=None)
    assert state.confirmed_conversations() == []


def test_state_for_status_command_shape(db_conn: sqlite3.Connection) -> None:
    """Round-trip: seed via state, query via state, shape matches CLI status."""
    upsert_conversation(db_conn, "o1", {"uuid": "c1", "name": "Chat"})
    state = RestoreState(db_conn, "t")
    state.mark_error(source_uuid="c1", object_type="conversation", error="RateLimited: 429")
    failures = state.recent_failures()
    assert len(failures) == 1
    assert failures[0]["object_type"] == "conversation"
    assert "RateLimited" in failures[0]["error"]


def test_bookmarked_count_filters_by_status_and_profile(
    db_conn: sqlite3.Connection,
) -> None:
    """`bookmarked_count` returns ONLY conversation rows with status='bookmarked'
    for THIS profile — error / ok / wrong-profile rows must not leak in."""
    a = RestoreState(db_conn, "alpha")
    b = RestoreState(db_conn, "beta")
    # Three bookmarked stubs for alpha.
    a.mark_bookmarked(source_uuid="c1", target_uuid="t1")
    a.mark_bookmarked(source_uuid="c2", target_uuid="t2")
    a.mark_bookmarked(source_uuid="c3", target_uuid="t3")
    # One loaded chat (status='ok') for alpha — must not count as bookmarked.
    a.mark_ok(source_uuid="c4", object_type="conversation", target_uuid="t4")
    # Beta has its own bookmarks — must not leak into alpha's count.
    b.mark_bookmarked(source_uuid="c1", target_uuid="bt1")

    assert a.bookmarked_count() == 3
    assert b.bookmarked_count() == 1


def test_already_bookmarked_returns_target_uuid_or_none(
    db_conn: sqlite3.Connection,
) -> None:
    """`already_bookmarked` is the gate the bookmark phase uses to skip
    re-creating a stub on re-run, AND the gate the default phase uses to
    refuse duplicating into a target that already has one."""
    state = RestoreState(db_conn, "tgt")
    assert state.already_bookmarked("c1") is None
    state.mark_bookmarked(source_uuid="c1", target_uuid="tgt-c1")
    assert state.already_bookmarked("c1") == "tgt-c1"
    # Loaded (status='ok') conversations are NOT bookmarked.
    state.mark_ok(source_uuid="c2", object_type="conversation", target_uuid="tgt-c2")
    assert state.already_bookmarked("c2") is None


def test_all_migrated_target_uuids_unions_ok_and_bookmarked(
    db_conn: sqlite3.Connection,
) -> None:
    """`cleanup` consults this set to refuse deletion of any chat the tool
    owns — both fully-loaded and bookmarked-but-empty chats must appear."""
    state = RestoreState(db_conn, "tgt")
    state.mark_bookmarked(source_uuid="c1", target_uuid="t-bookmark")
    state.mark_ok(source_uuid="c2", object_type="conversation", target_uuid="t-loaded")
    state.mark_error(source_uuid="c3", object_type="conversation", error="boom")
    # Project rows must not appear (cleanup operates on conversations only).
    state.mark_ok(source_uuid="p1", object_type="project", target_uuid="t-proj")

    assert state.all_migrated_target_uuids() == {"t-bookmark", "t-loaded"}
