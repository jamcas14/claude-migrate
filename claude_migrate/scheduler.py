"""Install per-OS daily timer that runs `claude-migrate backup <profile>`."""

from __future__ import annotations

import contextlib
import getpass
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from .config import data_dir

UNIT_NAME = "claude-migrate"
LAUNCHD_LABEL = "com.user.claudemigrate"
DEFAULT_PROFILE = "source"

# Dict-keyed lookup so mypy doesn't statically narrow `sys.platform` and flag
# the non-host branches as unreachable under `warn_unreachable`.
_FIXED_BACKENDS: dict[str, str] = {
    "darwin": "launchd",
    "win32": "task_scheduler",
}


@dataclass
class TimerStatus:
    installed: bool
    backend: str
    detail: str


def detect_backend() -> str:
    if backend := _FIXED_BACKENDS.get(sys.platform):
        return backend
    if shutil.which("systemctl"):
        return "systemd"
    if shutil.which("crontab"):
        return "cron"
    return "unsupported"


def _claude_migrate_argv() -> list[str]:
    """Return the argv prefix that invokes the CLI as a list of tokens.

    Returning a list (not a string) avoids the round-trip-through-split
    bug that mangles paths containing spaces (`/Users/My Name/.local/...`).
    """
    p = shutil.which("claude-migrate")
    if p:
        return [p]
    return [sys.executable, "-m", "claude_migrate"]


def _shell_quote_for_systemd(argv: list[str]) -> str:
    """systemd ExecStart accepts shell-style quoting; double-quote any token
    with a space and escape embedded `"`/`\\`."""
    out: list[str] = []
    for tok in argv:
        if any(ch in tok for ch in ' \t"\\'):
            esc = tok.replace("\\", "\\\\").replace('"', '\\"')
            out.append(f'"{esc}"')
        else:
            out.append(tok)
    return " ".join(out)


def _shell_quote_for_cron(argv: list[str]) -> str:
    """cron lines are shell-evaluated; use single quotes (POSIX-safe)."""
    out: list[str] = []
    for tok in argv:
        if not tok or any(ch in tok for ch in " \t\"'\\$;&|<>*?(){}#~`!"):
            esc = tok.replace("'", "'\\''")
            out.append(f"'{esc}'")
        else:
            out.append(tok)
    return " ".join(out)


def _systemd_dir() -> Path:
    if env := os.environ.get("XDG_CONFIG_HOME"):
        return Path(env) / "systemd" / "user"
    return Path.home() / ".config" / "systemd" / "user"


def _systemd_install(profile: str) -> TimerStatus:
    udir = _systemd_dir()
    udir.mkdir(parents=True, exist_ok=True)
    argv = [*_claude_migrate_argv(), "--quiet", "backup", profile]
    exec_start = _shell_quote_for_systemd(argv)
    log_dir = data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    # Propagate CLAUDE_MIGRATE_DATA_DIR if the user set it at install time —
    # otherwise the daily timer writes to the default ~/.local/share path
    # while interactive runs use the configured override (split-brain).
    env_lines = ""
    data_dir_env = os.environ.get("CLAUDE_MIGRATE_DATA_DIR")
    if data_dir_env:
        env_lines = (
            f"Environment=CLAUDE_MIGRATE_DATA_DIR="
            f"{_shell_quote_for_systemd([data_dir_env])}\n"
        )
    service = (
        "[Unit]\n"
        f"Description=Daily incremental backup of profile {profile!r}\n"
        "Wants=network-online.target\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={exec_start}\n"
        f"WorkingDirectory={data_dir().parent}\n"
        f"{env_lines}"
        f"StandardOutput=append:{log_dir}/backup.log\n"
        f"StandardError=append:{log_dir}/backup.log\n"
    )
    timer = (
        "[Unit]\n"
        f"Description=Daily claude-migrate backup of profile {profile!r}\n\n"
        "[Timer]\n"
        "OnCalendar=daily\n"
        "Persistent=true\n"
        "RandomizedDelaySec=15m\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    (udir / f"{UNIT_NAME}.service").write_text(service, "utf-8")
    (udir / f"{UNIT_NAME}.timer").write_text(timer, "utf-8")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    subprocess.run(
        ["systemctl", "--user", "enable", "--now", f"{UNIT_NAME}.timer"], check=False
    )
    return TimerStatus(installed=True, backend="systemd", detail=str(udir))


def _systemd_uninstall() -> TimerStatus:
    udir = _systemd_dir()
    subprocess.run(
        ["systemctl", "--user", "disable", "--now", f"{UNIT_NAME}.timer"], check=False
    )
    for fname in (f"{UNIT_NAME}.service", f"{UNIT_NAME}.timer"):
        with contextlib.suppress(FileNotFoundError):
            (udir / fname).unlink()
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    return TimerStatus(installed=False, backend="systemd", detail="uninstalled")


def _systemd_status() -> TimerStatus:
    res = subprocess.run(
        ["systemctl", "--user", "list-timers", f"{UNIT_NAME}.timer"],
        capture_output=True, text=True, check=False,
    )
    installed = (
        f"{UNIT_NAME}.timer" in res.stdout and "0 timers" not in res.stdout
    )
    return TimerStatus(installed=installed, backend="systemd", detail=res.stdout.strip())


def _launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _launchd_install(profile: str) -> TimerStatus:
    p = _launchd_plist_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    log_dir = data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    args = [*_claude_migrate_argv(), "--quiet", "backup", profile]
    # Every text node we interpolate must be XML-escaped — argv, log paths,
    # and env values alike. Otherwise a path containing `&`/`<`/`>`/quotes
    # leaves the plist malformed and `launchctl load` fails silently.
    extra = {'"': "&quot;", "'": "&apos;"}
    def xe(s: str) -> str:
        return xml_escape(s, extra)
    program_args = "".join(f"<string>{xe(a)}</string>" for a in args)
    log_path = xe(f"{log_dir}/backup.log")
    env_block = ""
    data_dir_env = os.environ.get("CLAUDE_MIGRATE_DATA_DIR")
    if data_dir_env:
        env_block = (
            "  <key>EnvironmentVariables</key>\n"
            f"  <dict><key>CLAUDE_MIGRATE_DATA_DIR</key>"
            f"<string>{xe(data_dir_env)}</string></dict>\n"
        )
    plist = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n<dict>\n'
        f'  <key>Label</key><string>{xe(LAUNCHD_LABEL)}</string>\n'
        '  <key>ProgramArguments</key>\n'
        f'  <array>{program_args}</array>\n'
        '  <key>StartCalendarInterval</key>\n'
        '  <dict><key>Hour</key><integer>4</integer><key>Minute</key><integer>17</integer></dict>\n'
        f'  <key>StandardOutPath</key><string>{log_path}</string>\n'
        f'  <key>StandardErrorPath</key><string>{log_path}</string>\n'
        '  <key>RunAtLoad</key><false/>\n'
        f'{env_block}'
        '</dict>\n</plist>\n'
    )
    p.write_text(plist, "utf-8")
    subprocess.run(["launchctl", "unload", str(p)], check=False)
    subprocess.run(["launchctl", "load", str(p)], check=False)
    return TimerStatus(installed=True, backend="launchd", detail=str(p))


def _launchd_uninstall() -> TimerStatus:
    p = _launchd_plist_path()
    subprocess.run(["launchctl", "unload", str(p)], check=False)
    with contextlib.suppress(FileNotFoundError):
        p.unlink()
    return TimerStatus(installed=False, backend="launchd", detail="uninstalled")


def _launchd_status() -> TimerStatus:
    p = _launchd_plist_path()
    return TimerStatus(installed=p.exists(), backend="launchd", detail=str(p))


def _task_scheduler_install(profile: str) -> TimerStatus:
    # Profile name is validated at the CLI boundary against a strict regex,
    # but defense-in-depth: this string is interpreted by Windows cmd.exe as
    # the /TR argument. We use subprocess.list2cmdline to escape paths/spaces.
    argv = [*_claude_migrate_argv(), "--quiet", "backup", profile]
    tr_value = subprocess.list2cmdline(argv)
    # Propagate CLAUDE_MIGRATE_DATA_DIR by wrapping the call in cmd.exe so
    # the SET runs in the same shell as the invocation. Without this, a
    # user with a custom data dir would see the daily timer write to a
    # different location than their interactive runs.
    data_dir_env = os.environ.get("CLAUDE_MIGRATE_DATA_DIR")
    if data_dir_env:
        # Quote the SET value via list2cmdline-equivalent: schtasks evaluates
        # /TR with cmd.exe, so we follow cmd's escape rules.
        set_value = subprocess.list2cmdline([data_dir_env])
        tr_value = (
            f'cmd.exe /c "set CLAUDE_MIGRATE_DATA_DIR={set_value} && {tr_value}"'
        )
    cmd = [
        "schtasks", "/Create", "/SC", "DAILY", "/TN", "claude-migrate",
        "/TR", tr_value, "/ST", "04:17", "/F",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return TimerStatus(
        installed=res.returncode == 0,
        backend="task_scheduler",
        detail=(res.stdout + res.stderr).strip(),
    )


def _task_scheduler_uninstall() -> TimerStatus:
    res = subprocess.run(
        ["schtasks", "/Delete", "/TN", "claude-migrate", "/F"],
        capture_output=True, text=True, check=False,
    )
    return TimerStatus(
        installed=False,
        backend="task_scheduler",
        detail=(res.stdout + res.stderr).strip(),
    )


def _task_scheduler_status() -> TimerStatus:
    res = subprocess.run(
        ["schtasks", "/Query", "/TN", "claude-migrate"],
        capture_output=True, text=True, check=False,
    )
    return TimerStatus(
        installed=res.returncode == 0,
        backend="task_scheduler",
        detail=(res.stdout + res.stderr).strip(),
    )


def install_timer(profile: str = DEFAULT_PROFILE) -> TimerStatus:
    backend = detect_backend()
    if backend == "systemd":
        return _systemd_install(profile)
    if backend == "launchd":
        return _launchd_install(profile)
    if backend == "task_scheduler":
        return _task_scheduler_install(profile)
    if backend == "cron":
        return _cron_install(profile)
    return TimerStatus(installed=False, backend=backend, detail="No supported scheduler found.")


def uninstall_timer() -> TimerStatus:
    backend = detect_backend()
    if backend == "systemd":
        return _systemd_uninstall()
    if backend == "launchd":
        return _launchd_uninstall()
    if backend == "task_scheduler":
        return _task_scheduler_uninstall()
    if backend == "cron":
        return _cron_uninstall()
    return TimerStatus(installed=False, backend=backend, detail="nothing to uninstall")


def timer_status() -> TimerStatus:
    backend = detect_backend()
    if backend == "systemd":
        return _systemd_status()
    if backend == "launchd":
        return _launchd_status()
    if backend == "task_scheduler":
        return _task_scheduler_status()
    if backend == "cron":
        return _cron_status()
    return TimerStatus(installed=False, backend=backend, detail="unsupported platform")


# ---- cron fallback for systems without systemd ----------------------------

CRON_TAG = "# claude-migrate (managed)"


def _cron_install(profile: str) -> TimerStatus:
    argv = [*_claude_migrate_argv(), "--quiet", "backup", profile]
    cmd = _shell_quote_for_cron(argv)
    # Inline env for the timer so a custom CLAUDE_MIGRATE_DATA_DIR matches
    # what the user runs interactively. cron evaluates the line with sh, so
    # `VAR=value cmd` works.
    env_prefix = ""
    data_dir_env = os.environ.get("CLAUDE_MIGRATE_DATA_DIR")
    if data_dir_env:
        env_prefix = f"CLAUDE_MIGRATE_DATA_DIR={_shell_quote_for_cron([data_dir_env])} "
    line = f"17 4 * * * {env_prefix}{cmd}  {CRON_TAG}\n"
    existing = subprocess.run(
        ["crontab", "-l"], capture_output=True, text=True, check=False
    ).stdout
    kept = [ln for ln in existing.splitlines() if CRON_TAG not in ln]
    new = "\n".join([*kept, line.rstrip()]) + "\n"
    proc = subprocess.run(
        ["crontab", "-"], input=new, text=True, check=False
    )
    return TimerStatus(
        installed=proc.returncode == 0, backend="cron",
        detail=f"user={getpass.getuser()} profile={profile}",
    )


def _cron_uninstall() -> TimerStatus:
    existing = subprocess.run(
        ["crontab", "-l"], capture_output=True, text=True, check=False
    ).stdout
    kept = [ln for ln in existing.splitlines() if CRON_TAG not in ln]
    new = "\n".join(kept) + "\n"
    subprocess.run(
        ["crontab", "-"], input=new, text=True, check=False
    )
    return TimerStatus(installed=False, backend="cron", detail="uninstalled")


def _cron_status() -> TimerStatus:
    existing = subprocess.run(
        ["crontab", "-l"], capture_output=True, text=True, check=False
    ).stdout
    return TimerStatus(
        installed=CRON_TAG in existing, backend="cron",
        detail=existing or "(empty crontab)",
    )
