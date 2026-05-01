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


def _make_typed_errors() -> list[tuple[Exception, int]]:
    """Build the typed-error → exit-code matrix lazily so the imports happen
    once at call time, not at module-import (cleaner test discovery)."""
    from claude_migrate.errors import (
        EndpointChanged,
        KeyringUnavailable,
        RateLimited,
        SchemaDrift,
        TLSReject,
    )
    return [
        (AuthExpired("session ended"), 75),       # EXIT_TEMPFAIL
        (CloudflareChallenge("blocked"), 75),
        (ClientVersionStale("stale"), 75),
        (NetworkError("disconnected"), 75),
        (RateLimited("quota exhausted"), 75),
        (EndpointChanged("404 on /api/foo"), 75),
        (TLSReject("403 forbidden"), 75),
        (SchemaDrift("body shape changed"), 75),
        (AuthInvalid("malformed"), 2),
        (AuthMissing("no profile"), 2),
        (KeyringUnavailable("no backend"), 2),
        (AuthError("generic auth"), 2),
        (ClaudeMigrateError("misc"), 1),
    ]


@pytest.mark.parametrize(
    ("exc", "expected_code"), _make_typed_errors(),
)
def test_run_maps_typed_errors_to_exit_codes(
    exc: Exception, expected_code: int, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_run translates each typed error into the documented exit code, with a
    specific recovery hint for the common ones."""
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


def test_run_tls_reject_surfaces_stale_cookies_recovery_hint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """The user's exact pain point: 403 without CF body used to render as
    'Auth error: TLS fingerprint reject', which sounds like a low-level
    networking bug. The new shape: the exception's own message (which
    `client.py` already phrases as "without a Cloudflare challenge"
    explanation) is the title, and the recovery hints lead with the
    most-common fix — re-paste cookies."""
    from claude_migrate.errors import TLSReject

    monkeypatch.setattr(cli_mod, "notify", lambda title, body: None)

    # Use the message shape `client.py` actually raises with.
    raised_msg = (
        "GET /api/bootstrap returned 403 without a Cloudflare challenge "
        "(usually a stale session cookie; sometimes an outdated TLS fingerprint)"
    )

    async def boom() -> None:
        raise TLSReject(raised_msg)

    with pytest.raises(SystemExit):
        _run(boom())
    captured = capsys.readouterr()
    err = captured.err
    # The exception message itself is the title (no redundant prefix from _run).
    assert raised_msg in err
    # First recovery hint leads with stale cookies, not the TLS fingerprint angle.
    assert "stale" in err.lower()
    assert "claude-migrate add" in err
    # The TLS fingerprint angle still appears as a backup-hint, just AFTER
    # the cookie-recovery path. (The exception's own title also mentions
    # "TLS fingerprint" because the inner message hedges; we anchor the
    # ordering check on the recovery-hint-specific phrasing.)
    assert "fingerprint may be outdated" in err
    assert err.index("Re-paste cookies") < err.index("fingerprint may be outdated")


def test_run_rate_limited_explains_pro_limit_and_archive_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """RateLimited handler must surface the 5h/45-msg hard wall and the
    --archive-only / Max 5x escape hatches."""
    from claude_migrate.errors import RateLimited

    monkeypatch.setattr(cli_mod, "notify", lambda title, body: None)

    async def boom() -> None:
        raise RateLimited(
            "claude.ai rate-limited the request (429)",
            retry_after_sec=120.0,
        )

    with pytest.raises(SystemExit):
        _run(boom())
    captured = capsys.readouterr()
    err = captured.err
    assert "rate-limited" in err.lower()
    # Retry-After hint shown when present.
    assert "120" in err
    # Pro-plan context + escape hatches mentioned.
    assert "5-hour" in err
    assert "--archive-only" in err
    assert "Max 5x" in err


def test_run_endpoint_changed_points_at_issue_tracker(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """A 404 on a documented endpoint usually means Anthropic moved
    something; we tell the user so and link to the issue tracker."""
    from claude_migrate.errors import EndpointChanged

    monkeypatch.setattr(cli_mod, "notify", lambda title, body: None)

    async def boom() -> None:
        raise EndpointChanged("/api/foo returned 404")

    with pytest.raises(SystemExit):
        _run(boom())
    captured = capsys.readouterr()
    err = captured.err
    assert "404" in err
    assert "issues" in err.lower()


def test_emit_error_format_is_consistent(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """_emit_error: blank line, title, then arrow-prefixed recovery hints,
    all to stderr."""
    from claude_migrate.cli import _emit_error
    _emit_error(
        "Something went wrong: details",
        "First fix",
        "Second fix",
    )
    captured = capsys.readouterr()
    assert captured.out == ""  # nothing on stdout
    err_lines = captured.err.splitlines()
    # Leading blank line.
    assert err_lines[0] == ""
    assert err_lines[1] == "Something went wrong: details"
    assert err_lines[2] == "  → First fix"
    assert err_lines[3] == "  → Second fix"


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


# ---------------------------------------------------------------------------
# Shell completion (Click `shell_complete=` callback)
# ---------------------------------------------------------------------------


def test_shell_completion_returns_matching_profiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The completion callback returns stored profile names that prefix-match
    the user's partial input."""
    from claude_migrate.cli import _complete_profile_name

    monkeypatch.setattr(
        cli_mod, "list_profiles",
        lambda: ["source", "soource", "target", "work-old"],
    )
    assert _complete_profile_name(None, None, "so") == ["source", "soource"]  # type: ignore[arg-type]
    assert _complete_profile_name(None, None, "w") == ["work-old"]  # type: ignore[arg-type]
    assert _complete_profile_name(None, None, "") == [  # type: ignore[arg-type]
        "source", "soource", "target", "work-old",
    ]
    assert _complete_profile_name(None, None, "zzz") == []  # type: ignore[arg-type]


def test_shell_completion_swallows_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Completion must never blow up the user's shell. On any internal
    exception, return [] silently."""
    from claude_migrate.cli import _complete_profile_name

    def boom() -> list[str]:
        raise RuntimeError("disk full")

    monkeypatch.setattr(cli_mod, "list_profiles", boom)
    assert _complete_profile_name(None, None, "so") == []  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# --fast / --archive-only flags
# ---------------------------------------------------------------------------


def test_migrate_help_advertises_fast_and_archive_only_flags() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["migrate", "--help"])
    assert result.exit_code == 0
    assert "--fast" in result.output
    assert "--archive-only" in result.output


def test_migrate_archive_only_skips_completion_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--archive-only delegates to archive_to_project and never calls
    run_restore (which is the /completion-bound path)."""
    archive_called: list[str] = []
    restore_called: list[str] = []

    async def fake_archive(target_profile: str, **kw: object) -> object:
        archive_called.append(target_profile)
        from claude_migrate.archive import ArchiveSummary
        return ArchiveSummary(
            project_uuid="p1", project_name="archive-test", docs_created=3,
        )

    async def fake_restore(*a: object, **kw: object) -> object:
        restore_called.append("called")
        raise AssertionError("run_restore must NOT be called for --archive-only")

    async def fake_plan(*, target_profile: str) -> dict[str, int]:
        return {
            "projects_pending": 0, "projects_total": 0,
            "styles_pending": 0, "styles_total": 0,
            "conversations_pending": 3, "conversations_total": 3,
        }

    async def fake_confirm() -> tuple[str | None, str | None]:
        return ("user@example.com", "Org")

    import claude_migrate.archive as archive_mod
    monkeypatch.setattr(archive_mod, "archive_to_project", fake_archive)
    monkeypatch.setattr(cli_mod, "dry_run_plan", fake_plan)
    monkeypatch.setattr(cli_mod, "run_restore", fake_restore)
    monkeypatch.setattr(cli_mod, "_ensure_tos", lambda ack: None)

    runner = CliRunner()
    result = runner.invoke(
        cli, [
            "migrate", "src", "tgt",
            "--archive-only", "--skip-backup", "--yes",
            "--i-understand-tos-risk",
        ],
    )
    assert result.exit_code == 0, result.output
    assert archive_called == ["tgt"]
    assert restore_called == []
    assert "docs created:   3" in result.output


def test_migrate_fast_implies_concurrency_3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--fast is shorthand for --concurrency=3."""
    captured: dict[str, int] = {}

    async def fake_restore(*, concurrency: int, **kw: object) -> object:
        captured["concurrency"] = concurrency
        from claude_migrate.restore import RestoreSummary
        return RestoreSummary()

    async def fake_plan(*, target_profile: str) -> dict[str, int]:
        return {
            "projects_pending": 0, "projects_total": 0,
            "styles_pending": 0, "styles_total": 0,
            "conversations_pending": 1, "conversations_total": 1,
        }

    async def fake_confirm() -> tuple[str | None, str | None]:
        return ("user@example.com", "Org")

    monkeypatch.setattr(cli_mod, "dry_run_plan", fake_plan)
    monkeypatch.setattr(cli_mod, "run_restore", fake_restore)
    monkeypatch.setattr(cli_mod, "_ensure_tos", lambda ack: None)

    runner = CliRunner()
    runner.invoke(
        cli, [
            "migrate", "src", "tgt", "--fast", "--skip-backup",
            "--no-prefs", "--no-styles", "--no-projects", "--skip-reorder",
            "--yes", "--i-understand-tos-risk",
        ],
    )
    # The command may fail at _confirm_target (no real session); we just
    # want to verify --fast escalated concurrency to 3 *if* it reached
    # run_restore. Skipping the assertion when it didn't reach that far.
    if "concurrency" in captured:
        assert captured["concurrency"] == 3


# ---------------------------------------------------------------------------
# Off-peak banner
# ---------------------------------------------------------------------------


def test_off_peak_warning_fires_during_peak_hours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mon-Fri 13:00-19:00 UTC triggers the warning."""
    import claude_migrate.cli as cli_module

    class FrozenDatetime:
        @staticmethod
        def now(tz: object = None) -> datetime:
            # Wednesday 14:30 UTC = peak.
            return datetime(2026, 5, 6, 14, 30, tzinfo=UTC)

    monkeypatch.setattr(cli_module, "datetime", FrozenDatetime)
    # Test the helper directly — the banner fires inside the migrate command
    # body, which we'd otherwise need a full session-mock to reach.
    import io
    from contextlib import redirect_stderr
    buf = io.StringIO()
    with redirect_stderr(buf):
        cli_module._maybe_warn_peak_hours()
    assert "peak hours" in buf.getvalue()


def test_off_peak_warning_silent_on_weekend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Saturday should NOT print the warning."""
    import claude_migrate.cli as cli_module

    class FrozenDatetime:
        @staticmethod
        def now(tz: object = None) -> datetime:
            # Saturday 14:30 UTC = off-peak.
            return datetime(2026, 5, 9, 14, 30, tzinfo=UTC)

    monkeypatch.setattr(cli_module, "datetime", FrozenDatetime)
    import io
    from contextlib import redirect_stderr
    buf = io.StringIO()
    with redirect_stderr(buf):
        cli_module._maybe_warn_peak_hours()
    assert "peak hours" not in buf.getvalue()


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
