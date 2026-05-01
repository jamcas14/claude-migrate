"""CLI-level tests: exit-code mapping, cleanup parser, dry-run hermeticity."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from click.testing import CliRunner

from claude_migrate import cli as cli_mod
from claude_migrate.cli import _parse_window_arg, _run, cli
from claude_migrate.errors import (
    AuthError,
    AuthExpired,
    AuthInvalid,
    AuthMissing,
    ClaudeMigrateError,
    ClientVersionStale,
    CloudflareChallenge,
    NetworkError,
)

# ---------------------------------------------------------------------------
# _run exit-code mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "expected_code"),
    [
        (AuthExpired("session ended"), 75),       # EXIT_TEMPFAIL
        (CloudflareChallenge("blocked"), 75),
        (ClientVersionStale("stale"), 75),
        (NetworkError("disconnected"), 75),
        (AuthInvalid("malformed"), 2),
        (AuthMissing("no profile"), 2),
        (AuthError("generic auth"), 2),
        (ClaudeMigrateError("misc"), 1),
    ],
)
def test_run_maps_typed_errors_to_exit_codes(
    exc: Exception, expected_code: int, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_run translates each known typed error into the documented exit code."""
    # Silence the desktop notification side effect on AuthExpired.
    monkeypatch.setattr(cli_mod, "notify", lambda title, body: None)

    async def boom() -> None:
        raise exc

    with pytest.raises(SystemExit) as exit_info:
        _run(boom())
    assert exit_info.value.code == expected_code


def test_run_keyboard_interrupt_exits_130(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_mod, "notify", lambda title, body: None)

    async def interrupt() -> None:
        raise KeyboardInterrupt

    with pytest.raises(SystemExit) as exit_info:
        _run(interrupt())
    assert exit_info.value.code == 130


def test_run_returns_value_on_success() -> None:
    """No exception → coroutine return value passes through unchanged and the
    declared TypeVar return type is preserved."""
    async def hello() -> str:
        return "hello"
    assert _run(hello()) == "hello"


# ---------------------------------------------------------------------------
# cleanup --since/--until parsing
# ---------------------------------------------------------------------------


def test_parse_window_date_only() -> None:
    """A bare date is interpreted as midnight UTC."""
    assert _parse_window_arg("2026-04-30") == datetime(2026, 4, 30, tzinfo=UTC)


def test_parse_window_date_and_minute() -> None:
    assert _parse_window_arg("2026-04-30T14:37") == datetime(
        2026, 4, 30, 14, 37, tzinfo=UTC
    )


def test_parse_window_with_z_suffix() -> None:
    assert _parse_window_arg("2026-04-30T14:37Z") == datetime(
        2026, 4, 30, 14, 37, tzinfo=UTC
    )


def test_parse_window_preserves_explicit_positive_offset() -> None:
    """+02:00 → 12:37 UTC. Pre-fix: replace(tzinfo=UTC) clobbered the offset,
    silently shifting the user's intended window by 2 hours."""
    assert _parse_window_arg("2026-04-30T14:37+02:00") == datetime(
        2026, 4, 30, 12, 37, tzinfo=UTC
    )


def test_parse_window_preserves_explicit_negative_offset() -> None:
    """-05:00 → 19:37 UTC."""
    assert _parse_window_arg("2026-04-30T14:37-05:00") == datetime(
        2026, 4, 30, 19, 37, tzinfo=UTC
    )


def test_parse_window_with_seconds() -> None:
    assert _parse_window_arg("2026-04-30T14:37:42") == datetime(
        2026, 4, 30, 14, 37, 42, tzinfo=UTC
    )


def test_parse_window_garbage_raises_value_error() -> None:
    with pytest.raises(ValueError):
        _parse_window_arg("not-a-date")


def test_parse_window_naive_input_assumes_utc() -> None:
    """Bare-naive input → tz-aware UTC datetime."""
    dt = _parse_window_arg("2026-04-30T14:37:00")
    assert dt.tzinfo is UTC
    assert dt.utcoffset().total_seconds() == 0


# ---------------------------------------------------------------------------
# Friendly errors for synchronous commands (don't traceback on missing profile).
# ---------------------------------------------------------------------------


def test_remove_missing_profile_emits_friendly_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`remove` runs synchronously without _run; AuthMissing must NOT
    propagate as a traceback."""
    from claude_migrate.errors import AuthMissing

    def fake_remove(name: str) -> None:
        raise AuthMissing(f"No profile named {name!r} to remove.")

    monkeypatch.setattr(cli_mod, "remove_profile", fake_remove)
    runner = CliRunner()
    result = runner.invoke(cli, ["remove", "ghost", "--yes"])
    assert result.exit_code == 2
    assert "No profile named 'ghost' to remove" in result.output
    # No Python traceback — just the friendly error.
    assert "Traceback" not in result.output


def test_rename_missing_source_emits_friendly_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`rename old new` with nonexistent old should print a friendly error,
    not traceback from load_profile."""
    from claude_migrate.errors import AuthMissing

    def fake_load(name: str) -> object:
        raise AuthMissing(f"No profile named {name!r} found. Run `claude-migrate add {name}` first.")

    monkeypatch.setattr(cli_mod, "load_profile", fake_load)
    runner = CliRunner()
    result = runner.invoke(cli, ["rename", "ghost", "newname"])
    assert result.exit_code == 2
    assert "No profile named 'ghost'" in result.output
    assert "Traceback" not in result.output


def test_cleanup_garbage_arg_exits_two() -> None:
    """The CLI command translates parse failures into exit code 2 with a hint."""
    runner = CliRunner()
    result = runner.invoke(
        cli, ["cleanup", "no-such-profile", "--since", "not-a-date", "--dry-run"]
    )
    assert result.exit_code == 2
    assert "Could not parse time bound" in result.output


def test_cleanup_default_until_arithmetic() -> None:
    """Sanity: --until omitted ⇒ since + 1 hour (the cleanup command's default)."""
    since = _parse_window_arg("2026-04-30T14:37")
    assert since + timedelta(hours=1) == datetime(2026, 4, 30, 15, 37, tzinfo=UTC)


# ---------------------------------------------------------------------------
# migrate --dry-run hermeticity: no source-side network
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Profile-name validation at CLI boundary (defense against schtasks/cron/SQL
# injection via attacker-controlled profile names).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_name",
    [
        "; calc.exe",          # cmd separator
        "name with spaces",    # whitespace
        "name`whoami`",        # backtick
        'name"$(whoami)"',     # shell substitution
        "name|pipe",           # pipe
        "../etc/passwd",       # path traversal
        "name\nnewline",       # newline
        "",                    # empty
        "a" * 65,              # too long (max 64)
        "name'quote",          # single quote
        "name&amp",            # ampersand
        "name<lt>",            # XML-special
    ],
)
def test_profile_name_rejected(bad_name: str) -> None:
    """Every profile-arg in the CLI runs a callback that refuses these."""
    runner = CliRunner()
    # Use `whoami` since it doesn't try to network-probe before the validator.
    result = runner.invoke(cli, ["whoami", bad_name])
    assert result.exit_code != 0, f"input {bad_name!r} unexpectedly accepted"
    assert "Profile name" in result.output or "invalid" in result.output.lower()


@pytest.mark.parametrize(
    "good_name",
    ["source", "target", "work", "personal-old", "acme.prod", "user_2024", "a", "A1"],
)
def test_profile_name_accepted(good_name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reasonable names pass validation. (whoami still fails downstream because
    the profile doesn't exist in the keychain, but the callback let it through.)"""
    runner = CliRunner()
    result = runner.invoke(cli, ["whoami", good_name])
    # Callback rejection prints "Profile name X is invalid"; success here
    # means we got past the callback into the actual command body. The
    # downstream failure (AuthMissing) gives exit code 2 with a specific
    # message — either way, we shouldn't see the validator's rejection text.
    assert "Profile name" not in result.output


def test_migrate_dry_run_does_not_open_source_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--dry-run must not call open_session(source). The previous behavior ran
    a full backup of the source account before the dry-run check returned."""
    calls: list[str] = []

    def fake_open_session(profile_name: str, **kw: object) -> object:
        calls.append(profile_name)
        raise AssertionError(
            f"--dry-run unexpectedly opened a session for profile={profile_name}"
        )

    # `open_session` is imported into `cli` at module-load time; patch the bound
    # attribute, not the source.
    monkeypatch.setattr(cli_mod, "open_session", fake_open_session)
    # `dry_run_plan` is async-coroutine-returning; stub with our own awaitable
    # that returns an empty plan so the command can finish without a DB.

    async def fake_plan(*, target_profile: str) -> dict[str, int]:
        return {
            "projects_pending": 0, "projects_total": 0,
            "styles_pending": 0, "styles_total": 0,
            "conversations_pending": 0, "conversations_total": 0,
        }

    monkeypatch.setattr(cli_mod, "dry_run_plan", fake_plan)
    monkeypatch.setattr(cli_mod, "_ensure_tos", lambda ack: None)

    runner = CliRunner()
    result = runner.invoke(
        cli, ["migrate", "src", "tgt", "--dry-run", "--i-understand-tos-risk"]
    )
    assert result.exit_code == 0, result.output
    assert calls == [], f"open_session was called with: {calls}"
    assert "(dry-run — no changes made)" in result.output
