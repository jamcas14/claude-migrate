"""Application paths and pydantic-settings config."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path

from pydantic import Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

KEYRING_SERVICE = "claude-migrate"
"""Stable service name used by the OS keychain."""

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

ANTHROPIC_CLIENT_VERSION_DEFAULT = "unknown"
"""Captured from a real browser session in DevTools → Network → any /api/* call.
Update via config.toml or CLAUDE_MIGRATE_CLIENT_VERSION env var. Doctor warns
when stale."""

IMPERSONATE = "chrome131"

BASE_URL = "https://claude.ai"

CONCURRENCY = 5
"""Hard cap on async fan-out. Real consumer accounts hit /completion sliding
windows fast above 5 parallel; the cap also limits Cloudflare burst-detection
exposure."""

RESTORE_CHAT_SLEEP_SEC = 90.0
"""Per-chat sleep during restore. Empirically 90s/chat keeps 429s rare on
real consumer accounts whose /completion rate window is longer than the 30s
documentation suggests. Override via CLAUDE_MIGRATE_CHAT_SLEEP_SEC."""

RESTORE_CHAT_RATE_LIMIT_SLEEP_SEC = 300.0
"""Extra cool-down when /completion returns 429. 5 minutes lets the sliding
window meaningfully recover before the next attempt."""


def config_dir() -> Path:
    """Cross-platform XDG-style config directory."""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "claude-migrate"
    if env := os.environ.get("XDG_CONFIG_HOME"):
        return Path(env) / "claude-migrate"
    return Path.home() / ".config" / "claude-migrate"


def data_dir() -> Path:
    """Cross-platform data directory.

    Override priority:
      1. `CLAUDE_MIGRATE_DATA_DIR` env var (explicit, wins).
      2. Per-OS XDG-style default:
         * Linux:  `$XDG_DATA_HOME/claude-migrate` or `~/.local/share/claude-migrate`
         * macOS:  `~/Library/Application Support/claude-migrate`
         * Windows: `%LOCALAPPDATA%\\claude-migrate` (or `~/AppData/Local/...`)

    Defaulting to `Path.cwd() / "data"` was a footgun: invoking the CLI from
    different working directories silently used different SQLite archives,
    and the systemd timer's `WorkingDirectory` snapshot then diverged from
    where the user normally ran the tool.
    """
    if env := os.environ.get("CLAUDE_MIGRATE_DATA_DIR"):
        return Path(env)
    # Dict-keyed lookup defeats mypy's `sys.platform`/`os.name` narrowing —
    # otherwise the non-host branches get flagged unreachable on each
    # `--platform` mypy pass. Same trick as scheduler.detect_backend.
    return _DATA_DIR_RESOLVERS.get(sys.platform, _xdg_data_dir)()


def _windows_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "claude-migrate"


def _macos_data_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "claude-migrate"


def _xdg_data_dir() -> Path:
    if env := os.environ.get("XDG_DATA_HOME"):
        return Path(env) / "claude-migrate"
    return Path.home() / ".local" / "share" / "claude-migrate"


_DATA_DIR_RESOLVERS: dict[str, Callable[[], Path]] = {
    "win32": _windows_data_dir,
    "darwin": _macos_data_dir,
}


class Settings(BaseSettings):
    """Pydantic-settings — reads env CLAUDE_MIGRATE_* and config.toml.

    The TOML path is resolved per-instance via `settings_customise_sources`
    rather than via `model_config["toml_file"]`, which would freeze the path
    to whatever XDG_CONFIG_HOME held the moment this module was imported —
    breaking tests and any code that swaps env vars after import.
    """

    model_config = SettingsConfigDict(
        env_prefix="CLAUDE_MIGRATE_",
        extra="ignore",
    )

    client_version: str = Field(default=ANTHROPIC_CLIENT_VERSION_DEFAULT)
    client_sha: str | None = Field(default=None)
    """Build SHA. Browser sends `anthropic-client-sha` per request — rotates
    every few weeks. When set, claude-migrate sends it; when None, omits."""

    anonymous_id: str | None = Field(default=None)
    """Per-browser-session id like `claudeai.v1.<uuid>`. Optional passthrough."""

    device_id: str | None = Field(default=None)
    """Per-browser-session UUID. Optional passthrough."""

    base_url: str = Field(default=BASE_URL)
    # Hard cap of 5 matches the Click `--concurrency` flag and the README's
    # documented limit; raising it above 5 trips Cloudflare burst-detection
    # on real consumer accounts.
    concurrency: int = Field(default=CONCURRENCY, ge=1, le=5)
    chat_sleep_sec: float = Field(default=RESTORE_CHAT_SLEEP_SEC, ge=0.0)
    user_agent: str = Field(default=USER_AGENT)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Priority: init kwargs > env > toml > file secrets > defaults.
        # toml_file is computed *here*, at instantiation, so XDG_CONFIG_HOME
        # changes (e.g. test fixtures) take effect.
        return (
            init_settings,
            env_settings,
            TomlConfigSettingsSource(settings_cls, toml_file=config_dir() / "config.toml"),
            file_secret_settings,
        )


def load_settings() -> Settings:
    return Settings()


def db_path() -> Path:
    return data_dir() / "claude.db"


def raw_dir(date: str) -> Path:
    return data_dir() / "raw" / date


def state_path() -> Path:
    return data_dir() / "state.json"
