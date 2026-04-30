"""Shared fixtures for pytest."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def tmp_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CLAUDE_MIGRATE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    return tmp_path


@pytest.fixture
def db_conn(tmp_data_dir: Path) -> Iterator[sqlite3.Connection]:
    from claude_migrate.store import open_db

    conn = open_db(tmp_data_dir / "claude.db")
    try:
        yield conn
    finally:
        conn.close()
