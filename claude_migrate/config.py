"""Application paths and pydantic-settings config."""

from __future__ import annotations

import os
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
"""Hard cap on async fan-out per Section 3 constraint #4."""

INTER_BATCH_SLEEP_SEC = 0.5
RESTORE_CHAT_SLEEP_SEC = 90.0
"""Per-chat sleep during restore. The brief targeted 1.0s, but real consumer
accounts have a sliding /completion rate window that 30s wasn't long enough
to respect — empirically 90s/chat keeps 429s rare. Override via
CLAUDE_MIGRATE_CHAT_SLEEP_SEC."""

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
    """Cross-platform data directory. Override via CLAUDE_MIGRATE_DATA_DIR."""
    if env := os.environ.get("CLAUDE_MIGRATE_DATA_DIR"):
        return Path(env)
    return Path.cwd() / "data"


class Settings(BaseSettings):
    """Pydantic-settings — reads env CLAUDE_MIGRATE_* and config.toml."""

    model_config = SettingsConfigDict(
        env_prefix="CLAUDE_MIGRATE_",
        toml_file=config_dir() / "config.toml",
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
    concurrency: int = Field(default=CONCURRENCY, ge=1, le=10)
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
        # env > toml > defaults
        return (
            init_settings,
            env_settings,
            TomlConfigSettingsSource(settings_cls),
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
