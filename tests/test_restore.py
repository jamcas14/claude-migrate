"""Restore-side tests: dry-run plan, date-prefix titles, idempotency wrapper."""

from __future__ import annotations

import sqlite3

from click.testing import CliRunner

from claude_migrate.cli import cli
from claude_migrate.restore import RestoreSummary, _date_prefix
from claude_migrate.store import (
    log_migration,
    upsert_conversation,
    upsert_project,
    upsert_style,
)


def test_date_prefix_iso_z() -> None:
    assert _date_prefix("2024-03-15T09:22:11Z") == "2024-03-15"


def test_date_prefix_iso_offset() -> None:
    assert _date_prefix("2024-03-15T09:22:11+00:00") == "2024-03-15"


def test_date_prefix_invalid() -> None:
    assert _date_prefix("not-a-date") == ""


def test_date_prefix_none() -> None:
    assert _date_prefix(None) == ""


def test_summary_default_failed_list() -> None:
    s = RestoreSummary()
    assert s.failed == []


def test_migrate_help_advertises_selectors() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["migrate", "--help"])
    assert result.exit_code == 0
    normalized = " ".join(result.output.split())
    for stem in ("prefs", "styles", "projects", "conversations"):
        assert f"--{stem} / --no-{stem}" in normalized, f"flag --{stem}/--no-{stem} missing"


def test_migrate_help_documents_concurrency_flag() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["migrate", "--help"])
    assert result.exit_code == 0
    assert "--concurrency" in result.output


def test_migrate_help_takes_two_positional_args() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["migrate", "--help"])
    assert result.exit_code == 0
    # Click formats positional args in usage as `Usage: cli migrate [OPTIONS] SOURCE TARGET`
    normalized = " ".join(result.output.split())
    assert "SOURCE TARGET" in normalized


def test_status_command_takes_positional_target() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["status", "--help"])
    assert result.exit_code == 0
    # Positional arg now: `Usage: cli status [OPTIONS] TARGET`
    assert "TARGET" in result.output


def test_verify_command_help_uses_reconcile_flag() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["verify", "--help"])
    assert result.exit_code == 0
    assert "--reconcile" in result.output
    # Old --drop-missing flag is gone.
    assert "--drop-missing" not in result.output


def test_top_level_help_lists_new_verbs() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    for verb in ("login", "logout", "accounts", "backup", "migrate",
                 "verify", "reorder", "cleanup", "schedule"):
        assert verb in result.output, f"top-level verb {verb!r} missing from --help"


def test_old_verbs_no_longer_registered() -> None:
    """auth, dump, restore (as separate command), timer (as group) are gone."""
    runner = CliRunner()
    for old in ("auth", "dump", "restore", "timer"):
        result = runner.invoke(cli, [old, "--help"])
        assert result.exit_code != 0, f"old command {old!r} unexpectedly still works"


def test_status_prints_all_caught_up_for_complete_migration(
    db_conn: sqlite3.Connection,
    tmp_data_dir: object,
) -> None:
    """When source archive equals target_ok, status shows 'All caught up.'"""
    from claude_migrate.store import (
        log_migration,
        upsert_conversation,
        upsert_project,
    )

    upsert_project(db_conn, "o1", {"uuid": "p1", "name": "P"})
    upsert_conversation(db_conn, "o1", {"uuid": "c1", "name": "Chat"})
    log_migration(
        db_conn, source_uuid="c1", object_type="conversation",
        target_profile="t", target_uuid="tgt-c1", status="ok",
    )
    log_migration(
        db_conn, source_uuid="p1", object_type="project",
        target_profile="t", target_uuid="tgt-p1", status="ok",
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["status", "t"])
    assert result.exit_code == 0, result.output
    assert "1/1 migrated" in result.output
    assert "All caught up" in result.output


def test_status_prints_failures_with_recovery_hint(
    db_conn: sqlite3.Connection,
    tmp_data_dir: object,
) -> None:
    from claude_migrate.store import log_migration, upsert_conversation

    upsert_conversation(db_conn, "o1", {"uuid": "c1", "name": "Chat"})
    log_migration(
        db_conn, source_uuid="c1", object_type="conversation",
        target_profile="t", target_uuid=None, status="error",
        error="RateLimited: 429",
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["status", "t"])
    assert result.exit_code == 0
    assert "recent failures" in result.output
    assert "claude-migrate migrate" in result.output  # new recovery hint
    assert "RateLimited" in result.output


def test_accounts_command_help() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["accounts", "--help"])
    assert result.exit_code == 0
    assert "List stored profiles" in result.output


def test_accounts_with_no_profiles_shows_login_hint() -> None:
    """`accounts` with an empty profile list points the user at `login`."""
    from claude_migrate import cli as cli_mod

    real = cli_mod.list_profiles
    cli_mod.list_profiles = lambda: []  # type: ignore[assignment]
    try:
        runner = CliRunner()
        result = runner.invoke(cli, ["accounts"])
    finally:
        cli_mod.list_profiles = real  # type: ignore[assignment]
    assert result.exit_code == 0
    assert "No profiles stored" in result.output
    assert "claude-migrate login" in result.output


def test_accounts_with_profiles_shows_management_hints() -> None:
    """The non-empty `accounts` output should advertise login/rename/logout/whoami
    so users discover the profile-management verbs without reading --help."""
    from claude_migrate import cli as cli_mod
    from claude_migrate.auth import Profile

    real_list = cli_mod.list_profiles
    real_load = cli_mod.load_profile
    cli_mod.list_profiles = lambda: ["work"]  # type: ignore[assignment]
    cli_mod.load_profile = lambda name: Profile(  # type: ignore[assignment]
        session_key="x", cf_clearance="y",
        email="user@example.com", last_probe_ok="2026-01-01",
    )
    try:
        runner = CliRunner()
        result = runner.invoke(cli, ["accounts"])
    finally:
        cli_mod.list_profiles = real_list  # type: ignore[assignment]
        cli_mod.load_profile = real_load  # type: ignore[assignment]
    assert result.exit_code == 0
    assert "user@example.com" in result.output
    for hint in ("claude-migrate login", "claude-migrate rename",
                 "claude-migrate logout", "claude-migrate whoami"):
        assert hint in result.output, f"{hint!r} missing from accounts output"


def test_rename_help_lists_old_and_new_args() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["rename", "--help"])
    assert result.exit_code == 0
    assert "OLD_NAME NEW_NAME" in result.output
    assert "no network call" in result.output


def test_rename_refuses_when_destination_already_exists() -> None:
    """If a profile under NEW_NAME already exists, refuse rather than clobber it."""
    from claude_migrate import cli as cli_mod
    from claude_migrate.auth import Profile
    from claude_migrate.errors import AuthMissing

    profile = Profile(session_key="x", cf_clearance="y", email="a@b.com")

    def fake_load(name: str) -> Profile:
        if name in ("source", "target"):
            return profile
        raise AuthMissing(f"No profile named {name!r}")

    real_load = cli_mod.load_profile
    cli_mod.load_profile = fake_load  # type: ignore[assignment]
    try:
        runner = CliRunner()
        result = runner.invoke(cli, ["rename", "source", "target"])
    finally:
        cli_mod.load_profile = real_load  # type: ignore[assignment]
    assert result.exit_code != 0
    assert "already exists" in result.output


def test_rename_no_op_when_old_and_new_match() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["rename", "samename", "samename"])
    assert result.exit_code == 0
    assert "nothing to do" in result.output


def test_dry_run_plan_counts_pending(db_conn: sqlite3.Connection) -> None:
    upsert_project(db_conn, "o1", {"uuid": "p1", "name": "First"})
    upsert_project(db_conn, "o1", {"uuid": "p2", "name": "Second"})
    upsert_style(db_conn, "o1", {"uuid": "s1", "name": "Style"})
    upsert_conversation(db_conn, "o1", {"uuid": "c1", "name": "Chat"})
    log_migration(
        db_conn, source_uuid="p1", object_type="project",
        target_profile="t", target_uuid="x", status="ok",
    )

    pending = db_conn.execute(
        "SELECT COUNT(*) FROM project p WHERE NOT EXISTS ("
        "  SELECT 1 FROM migration_log m WHERE m.source_uuid=p.uuid "
        "  AND m.target_profile='t' AND m.status='ok')"
    ).fetchone()[0]
    assert pending == 1
