"""Cross-platform desktop notifications. Best-effort, never raise."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import urllib.request
from urllib.error import URLError
from xml.sax.saxutils import escape as xml_escape

import structlog

log = structlog.get_logger(__name__)

NTFY_TOPIC_ENV = "CLAUDE_MIGRATE_NTFY_TOPIC"
NTFY_BASE_URL = "https://ntfy.sh"


def notify(title: str, body: str) -> None:
    """Fire-and-forget notification across all available channels."""
    _ntfy(title, body)
    if sys.platform == "darwin":
        _osascript(title, body)
    elif sys.platform == "win32":
        _windows_toast(title, body)
    else:
        _notify_send(title, body)


def _ntfy(title: str, body: str) -> None:
    topic = os.environ.get(NTFY_TOPIC_ENV)
    if not topic:
        return
    url = f"{NTFY_BASE_URL}/{topic}"
    req = urllib.request.Request(
        url,
        data=body.encode("utf-8"),
        headers={"Title": title, "Priority": "high"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5):
            pass
    except (URLError, OSError) as e:
        log.debug("ntfy_failed", err=str(e))


def _osascript(title: str, body: str) -> None:
    if shutil.which("osascript") is None:
        return
    script = (
        f'display notification "{_escape_applescript(body)}" '
        f'with title "{_escape_applescript(title)}"'
    )
    try:
        subprocess.run(
            ["osascript", "-e", script], check=False, timeout=5
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.debug("osascript_failed", err=str(e))


def _escape_applescript(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _windows_toast(title: str, body: str) -> None:
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        return
    # Escape for both the XML payload (`<`, `>`, `&`, plus quotes) AND the
    # PowerShell single-quoted string ('' for embedded '). Doing only the
    # latter leaves the toast vulnerable to XML-malformed titles and lets a
    # title containing `</text>` break the binding template.
    extra = {'"': "&quot;", "'": "&apos;"}
    xml_title = xml_escape(title, extra).replace("'", "''")
    xml_body = xml_escape(body, extra).replace("'", "''")
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager,"
        "Windows.UI.Notifications,ContentType=WindowsRuntime] | Out-Null;"
        f"$xml = [xml]\"<toast><visual><binding template='ToastText02'>"
        f"<text id='1'>{xml_title}</text>"
        f"<text id='2'>{xml_body}</text></binding></visual></toast>\";"
        "$doc = New-Object Windows.Data.Xml.Dom.XmlDocument;"
        "$doc.LoadXml($xml.OuterXml);"
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($doc);"
        "[Windows.UI.Notifications.ToastNotificationManager]::"
        "CreateToastNotifier('claude-migrate').Show($toast);"
    )
    try:
        subprocess.run(
            [powershell, "-NoProfile", "-Command", script],
            check=False, timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.debug("toast_failed", err=str(e))


def _notify_send(title: str, body: str) -> None:
    if shutil.which("notify-send") is None:
        return
    try:
        subprocess.run(
            ["notify-send", "--app-name=claude-migrate", title, body],
            check=False, timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.debug("notify_send_failed", err=str(e))
