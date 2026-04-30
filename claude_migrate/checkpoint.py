"""Resumable state for incremental dump and restore."""

from __future__ import annotations

import sqlite3
from typing import Any

from .store import content_hash, now_iso


def needs_refresh(
    conn: sqlite3.Connection,
    object_type: str,
    object_uuid: str,
    *,
    updated_at: str | None,
    payload_hash: str | None = None,
) -> bool:
    """Decide whether to re-fetch this object's full body."""
    row = conn.execute(
        "SELECT last_seen_updated_at, content_hash FROM checkpoint "
        "WHERE object_type=? AND object_uuid=?",
        (object_type, object_uuid),
    ).fetchone()
    if row is None:
        return True
    if updated_at is not None and updated_at != row["last_seen_updated_at"]:
        return True
    return payload_hash is not None and payload_hash != row["content_hash"]


def mark_dumped(
    conn: sqlite3.Connection,
    object_type: str,
    object_uuid: str,
    *,
    updated_at: str | None,
    payload: Any | None = None,
) -> None:
    h = content_hash(payload) if payload is not None else None
    conn.execute(
        "INSERT INTO checkpoint(object_type, object_uuid, last_seen_updated_at, "
        "last_dumped_at, content_hash) VALUES (?,?,?,?,?) "
        "ON CONFLICT(object_type, object_uuid) DO UPDATE SET "
        "last_seen_updated_at=excluded.last_seen_updated_at, "
        "last_dumped_at=excluded.last_dumped_at, content_hash=excluded.content_hash",
        (object_type, object_uuid, updated_at, now_iso(), h),
    )
