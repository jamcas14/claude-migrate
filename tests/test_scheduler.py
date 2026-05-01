"""Regression tests for scheduler-template generation.

Older versions of these templates invoked `claude-migrate dump --incremental`,
which broke after the CLI was reorganized to use `backup PROFILE` instead.
These tests pin every backend's rendered output so a future template edit
can't silently re-break the daily timer.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from claude_migrate import scheduler


def _stub_subprocess() -> object:
    """Patch subprocess.run so install/uninstall don't actually touch the OS."""

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    return patch("claude_migrate.scheduler.subprocess.run", return_value=_Result())


def test_systemd_install_writes_backup_command_with_profile(
    tmp_path: Path, monkeypatch: object,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))  # type: ignore[attr-defined]
    monkeypatch.setenv("CLAUDE_MIGRATE_DATA_DIR", str(tmp_path / "data"))  # type: ignore[attr-defined]
    with _stub_subprocess():
        scheduler._systemd_install("work")
    service = (tmp_path / "config" / "systemd" / "user" / "claude-migrate.service").read_text()
    timer = (tmp_path / "config" / "systemd" / "user" / "claude-migrate.timer").read_text()
    assert "backup work" in service, "service unit must call `backup <profile>`"
    assert "dump" not in service, "service unit must not reference the removed `dump` command"
    assert "--quiet" in service
    assert "OnCalendar=daily" in timer
    assert "Persistent=true" in timer


def test_launchd_install_writes_backup_command_with_profile(
    tmp_path: Path, monkeypatch: object,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("CLAUDE_MIGRATE_DATA_DIR", str(tmp_path / "data"))  # type: ignore[attr-defined]
    with _stub_subprocess():
        scheduler._launchd_install("personal")
    plist_path = scheduler._launchd_plist_path()
    plist = plist_path.read_text()
    assert "<string>backup</string>" in plist, "plist must include backup command"
    assert "<string>personal</string>" in plist, "plist must reference the profile name"
    assert "<string>dump</string>" not in plist, "plist must not reference the removed `dump`"
    assert "<string>--quiet</string>" in plist
    assert "Hour" in plist  # has a daily schedule


def test_task_scheduler_install_writes_backup_command_with_profile() -> None:
    """Windows Task Scheduler: assert the schtasks /TR argument is correct."""
    captured: dict[str, list[str]] = {"args": []}

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd: list[str], *args: object, **kwargs: object) -> _Result:
        captured["args"] = list(cmd)
        return _Result()

    with patch("claude_migrate.scheduler.subprocess.run", side_effect=fake_run):
        scheduler._task_scheduler_install("work-old")
    cmd = captured["args"]
    # /TR carries the actual command to run; find it.
    tr_idx = cmd.index("/TR")
    tr_value = cmd[tr_idx + 1]
    assert "backup work-old" in tr_value, f"task command should run backup work-old, got {tr_value!r}"
    assert "dump" not in tr_value, "must not reference removed `dump` command"
    assert "--quiet" in tr_value


def test_cron_install_writes_backup_command_with_profile() -> None:
    """Cron fallback: capture the crontab input piped to `crontab -`."""
    captured: dict[str, str] = {"input": ""}

    class _Result:
        returncode = 0
        stdout = ""

    def fake_run(cmd: list[str], **kwargs: object) -> _Result:
        if cmd == ["crontab", "-"]:
            captured["input"] = str(kwargs.get("input", ""))
        return _Result()

    with patch("claude_migrate.scheduler.subprocess.run", side_effect=fake_run):
        scheduler._cron_install("scratch")
    cron_input = captured["input"]
    assert "backup scratch" in cron_input
    assert "dump" not in cron_input
    assert scheduler.CRON_TAG in cron_input


def test_install_timer_default_profile_is_source() -> None:
    """Public install_timer() default backs up `source` (matches CLI default)."""
    assert scheduler.DEFAULT_PROFILE == "source"


def test_detect_backend_returns_known_value() -> None:
    """detect_backend should return one of the supported strings, never crash."""
    backend = scheduler.detect_backend()
    assert backend in {"systemd", "launchd", "task_scheduler", "cron", "unsupported"}


def test_systemd_quoting_handles_spaces_in_path() -> None:
    """A claude-migrate path containing spaces (e.g. `/Users/My Name/.local/bin/`)
    must be quoted so systemd parses it as one argv token, not three."""
    quoted = scheduler._shell_quote_for_systemd(
        ["/Users/My Name/.local/bin/claude-migrate", "--quiet", "backup", "src"]
    )
    assert '"/Users/My Name/.local/bin/claude-migrate"' in quoted
    assert "--quiet backup src" in quoted


def test_systemd_quoting_escapes_backslash_and_quote() -> None:
    quoted = scheduler._shell_quote_for_systemd(["/path/with\"quote", "arg"])
    assert "\\\"" in quoted  # embedded " is backslash-escaped


def test_cron_quoting_single_quotes_special_chars() -> None:
    """cron lines are POSIX shell; single-quote anything with metacharacters."""
    quoted = scheduler._shell_quote_for_cron(
        ["/some/path", "--quiet", "backup", "src;rm-rf"]
    )
    # The dangerous src;rm-rf must be in single quotes.
    assert "'src;rm-rf'" in quoted


def test_claude_migrate_argv_returns_list() -> None:
    """No more `exe.split()` — paths with spaces don't get fragmented."""
    argv = scheduler._claude_migrate_argv()
    assert isinstance(argv, list)
    assert all(isinstance(a, str) for a in argv)
    assert len(argv) >= 1


def test_launchd_install_xml_escapes_special_chars(
    tmp_path: Path, monkeypatch: object,
) -> None:
    """Plist `<string>` content must XML-escape `&`/`<`/`>`/quotes so a path
    or profile name with these chars doesn't break the plist parser."""
    monkeypatch.setenv("HOME", str(tmp_path))  # type: ignore[attr-defined]
    monkeypatch.setenv("CLAUDE_MIGRATE_DATA_DIR", str(tmp_path / "data"))  # type: ignore[attr-defined]
    monkeypatch.setattr(  # type: ignore[attr-defined]
        scheduler, "_claude_migrate_argv",
        lambda: ["/path/with&amp"],
    )
    with _stub_subprocess():
        scheduler._launchd_install("normal-name")
    plist = scheduler._launchd_plist_path().read_text()
    # Raw `&` in the path must come out as `&amp;` in the plist; otherwise
    # the plist is malformed.
    assert "&amp;amp" in plist
    assert "<string>/path/with&amp;amp</string>" in plist
