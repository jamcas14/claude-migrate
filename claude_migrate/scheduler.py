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

from .config import data_dir

UNIT_NAME = "claude-migrate"
LAUNCHD_LABEL = "com.user.claudemigrate"
DEFAULT_PROFILE = "source"


@dataclass
class TimerStatus:
    installed: bool
    backend: str
    detail: str


def detect_backend() -> str:
    if sys.platform == "darwin":
        return "launchd"
    if sys.platform == "win32":
        return "task_scheduler"
    if shutil.which("systemctl"):
        return "systemd"
    if shutil.which("crontab"):
        return "cron"
    return "unsupported"


def _claude_migrate_path() -> str:
    p = shutil.which("claude-migrate")
    if p:
        return p
    return f"{sys.executable} -m claude_migrate"


def _systemd_dir() -> Path:
    if env := os.environ.get("XDG_CONFIG_HOME"):
        return Path(env) / "systemd" / "user"
    return Path.home() / ".config" / "systemd" / "user"


def _systemd_install(profile: str) -> TimerStatus:
    udir = _systemd_dir()
    udir.mkdir(parents=True, exist_ok=True)
    exe = _claude_migrate_path()
    log_dir = data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    service = (
        "[Unit]\n"
        f"Description=Daily incremental backup of profile {profile!r}\n"
        "Wants=network-online.target\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={exe} --quiet backup {profile}\n"
        f"WorkingDirectory={data_dir().parent}\n"
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
    exe = _claude_migrate_path()
    log_dir = data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    args = [*exe.split(), "--quiet", "backup", profile]
    program_args = "".join(f"<string>{a}</string>" for a in args)
    plist = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n<dict>\n'
        f'  <key>Label</key><string>{LAUNCHD_LABEL}</string>\n'
        '  <key>ProgramArguments</key>\n'
        f'  <array>{program_args}</array>\n'
        '  <key>StartCalendarInterval</key>\n'
        '  <dict><key>Hour</key><integer>4</integer><key>Minute</key><integer>17</integer></dict>\n'
        f'  <key>StandardOutPath</key><string>{log_dir}/backup.log</string>\n'
        f'  <key>StandardErrorPath</key><string>{log_dir}/backup.log</string>\n'
        '  <key>RunAtLoad</key><false/>\n'
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
    exe = _claude_migrate_path()
    cmd = [
        "schtasks", "/Create", "/SC", "DAILY", "/TN", "claude-migrate",
        "/TR", f'"{exe}" --quiet backup {profile}',
        "/ST", "04:17", "/F",
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
    exe = _claude_migrate_path()
    line = f"17 4 * * * {exe} --quiet backup {profile}  {CRON_TAG}\n"
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
