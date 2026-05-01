"""SQLite + FTS5 + gzipped raw-JSON sidecar storage."""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import hashlib
import json
import re
import sqlite3
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import data_dir, db_path, raw_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS org (
    uuid TEXT PRIMARY KEY,
    name TEXT,
    capabilities TEXT,
    raw TEXT
);

CREATE TABLE IF NOT EXISTS account (
    org_uuid TEXT PRIMARY KEY,
    profile TEXT,
    raw TEXT,
    fetched_at TEXT
);

CREATE TABLE IF NOT EXISTS project (
    uuid TEXT PRIMARY KEY,
    org_uuid TEXT,
    name TEXT,
    prompt_template TEXT,
    created_at TEXT,
    updated_at TEXT,
    raw TEXT
);

CREATE TABLE IF NOT EXISTS project_doc (
    uuid TEXT PRIMARY KEY,
    project_uuid TEXT,
    file_name TEXT,
    content TEXT,
    created_at TEXT,
    raw TEXT
);

CREATE TABLE IF NOT EXISTS conversation (
    uuid TEXT PRIMARY KEY,
    org_uuid TEXT,
    project_uuid TEXT,
    title TEXT,
    model TEXT,
    is_starred INTEGER,
    created_at TEXT,
    updated_at TEXT,
    message_count INTEGER,
    raw_path TEXT,
    transcript_md TEXT,
    transcript_token_estimate INTEGER
);

CREATE TABLE IF NOT EXISTS message (
    uuid TEXT PRIMARY KEY,
    conversation_uuid TEXT,
    sender TEXT,
    index_in_conversation INTEGER,
    created_at TEXT,
    content TEXT,
    raw TEXT
);

CREATE TABLE IF NOT EXISTS attachment (
    uuid TEXT PRIMARY KEY,
    message_uuid TEXT,
    file_name TEXT,
    file_kind TEXT,
    file_size INTEGER,
    local_path TEXT,
    original_url TEXT
);

CREATE TABLE IF NOT EXISTS custom_style (
    uuid TEXT PRIMARY KEY,
    org_uuid TEXT,
    name TEXT,
    prompt TEXT,
    examples TEXT,
    raw TEXT
);

CREATE TABLE IF NOT EXISTS checkpoint (
    object_type TEXT NOT NULL,
    object_uuid TEXT NOT NULL,
    last_seen_updated_at TEXT,
    last_dumped_at TEXT,
    content_hash TEXT,
    PRIMARY KEY (object_type, object_uuid)
);

CREATE TABLE IF NOT EXISTS migration_log (
    source_uuid TEXT NOT NULL,
    target_uuid TEXT,
    object_type TEXT NOT NULL,
    target_profile TEXT NOT NULL,
    migrated_at TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    PRIMARY KEY (source_uuid, target_profile)
);

CREATE INDEX IF NOT EXISTS idx_msg_conv ON message(conversation_uuid);
CREATE INDEX IF NOT EXISTS idx_conv_proj ON conversation(project_uuid);
CREATE INDEX IF NOT EXISTS idx_migration_target ON migration_log(target_profile, status);
"""

# FTS5 is best-effort — older sqlite3 builds lack it.
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS conversation_fts USING fts5(
    title, transcript_md, content=conversation, content_rowid=rowid
);
"""


_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def slugify(s: str, *, max_len: int = 40) -> str:
    base = _SLUG_RE.sub("-", s.lower()).strip("-")
    return (base or "untitled")[:max_len]


def open_db(path: Path | None = None) -> sqlite3.Connection:
    p = path or db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), isolation_level=None, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.executescript(SCHEMA)
    with contextlib.suppress(sqlite3.OperationalError):
        conn.executescript(FTS_SCHEMA)
    return conn


# Per-connection asyncio Lock keyed by id(conn). sqlite3.Connection lacks
# `__weakref__` so a WeakKeyDictionary won't work; the small per-process leak
# (one Lock per connection ever opened) is acceptable for a CLI tool.
_conn_locks: dict[int, asyncio.Lock] = {}


def _conn_lock(conn: sqlite3.Connection) -> asyncio.Lock:
    """Lazy per-connection asyncio.Lock for `async_transaction`."""
    key = id(conn)
    lock = _conn_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _conn_locks[key] = lock
    return lock


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Synchronous BEGIN/COMMIT/ROLLBACK helper for autocommit-mode connections.

    Concurrency invariant: this synchronous variant must NOT have an `await`
    in its body — sqlite3.Connection serializes transactions per-connection,
    not per-coroutine. For coroutines that need to do async work as part of
    a transactional unit, use `async_transaction` which adds an asyncio.Lock
    around the whole block.
    """
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


@asynccontextmanager
async def async_transaction(conn: sqlite3.Connection) -> AsyncIterator[sqlite3.Connection]:
    """Async version of `transaction()` — holds a per-connection asyncio.Lock
    around the whole block. Use this when concurrent coroutines may both want
    to issue BEGIN/COMMIT against the same connection.

    For purely-synchronous transaction bodies (no `await` between BEGIN and
    COMMIT), the synchronous `transaction()` is sufficient. Reach for this
    helper only when an `await` inside is unavoidable — see CLAUDE.md.
    """
    async with _conn_lock(conn):
        conn.execute("BEGIN")
        try:
            yield conn
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")


def write_raw(slug: str, payload: Any) -> Path:
    """Gzip a payload to data/raw/{date}/{slug}-{uuid}.json.gz; returns absolute path."""
    date = datetime.now(UTC).strftime("%Y-%m-%d")
    folder = raw_dir(date)
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{slugify(slug)}-{uuid.uuid4().hex[:8]}.json.gz"
    path = folder / name
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    with gzip.open(path, "wb", compresslevel=6) as f:
        f.write(body)
    return path


def content_hash(payload: Any) -> str:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# UPSERT helpers — idempotent on uuid
# ---------------------------------------------------------------------------


def upsert_org(conn: sqlite3.Connection, org: dict[str, Any]) -> None:
    raw = json.dumps(org)
    conn.execute(
        "INSERT INTO org(uuid, name, capabilities, raw) VALUES (?,?,?,?) "
        "ON CONFLICT(uuid) DO UPDATE SET name=excluded.name, "
        "capabilities=excluded.capabilities, raw=excluded.raw",
        (org["uuid"], org.get("name"), json.dumps(org.get("capabilities")), raw),
    )


def upsert_account(conn: sqlite3.Connection, org_uuid: str, account: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO account(org_uuid, profile, raw, fetched_at) VALUES (?,?,?,?) "
        "ON CONFLICT(org_uuid) DO UPDATE SET profile=excluded.profile, "
        "raw=excluded.raw, fetched_at=excluded.fetched_at",
        (org_uuid, json.dumps(account.get("settings") or {}), json.dumps(account), now_iso()),
    )


def upsert_project(conn: sqlite3.Connection, org_uuid: str, project: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO project(uuid, org_uuid, name, prompt_template, created_at, updated_at, raw) "
        "VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(uuid) DO UPDATE SET name=excluded.name, "
        "prompt_template=excluded.prompt_template, created_at=excluded.created_at, "
        "updated_at=excluded.updated_at, raw=excluded.raw",
        (
            project["uuid"],
            org_uuid,
            project.get("name"),
            project.get("prompt_template"),
            project.get("created_at"),
            project.get("updated_at"),
            json.dumps(project),
        ),
    )


def upsert_project_doc(conn: sqlite3.Connection, project_uuid: str, doc: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO project_doc(uuid, project_uuid, file_name, content, created_at, raw) "
        "VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(uuid) DO UPDATE SET project_uuid=excluded.project_uuid, "
        "file_name=excluded.file_name, content=excluded.content, raw=excluded.raw",
        (
            doc["uuid"],
            project_uuid,
            doc.get("file_name"),
            doc.get("content"),
            doc.get("created_at"),
            json.dumps(doc),
        ),
    )


def upsert_conversation(
    conn: sqlite3.Connection,
    org_uuid: str,
    conv: dict[str, Any],
    raw_path: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO conversation(uuid, org_uuid, project_uuid, title, model, is_starred, "
        "created_at, updated_at, message_count, raw_path, transcript_md, "
        "transcript_token_estimate) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(uuid) DO UPDATE SET title=excluded.title, model=excluded.model, "
        "is_starred=excluded.is_starred, updated_at=excluded.updated_at, "
        "message_count=excluded.message_count, raw_path=excluded.raw_path, "
        "transcript_md=excluded.transcript_md, "
        "transcript_token_estimate=excluded.transcript_token_estimate",
        (
            conv["uuid"],
            org_uuid,
            conv.get("project_uuid"),
            conv.get("name") or conv.get("title"),
            conv.get("model"),
            int(bool(conv.get("is_starred"))),
            conv.get("created_at"),
            conv.get("updated_at"),
            len(conv.get("chat_messages") or []),
            raw_path,
            conv.get("transcript_md"),
            conv.get("transcript_token_estimate"),
        ),
    )


def upsert_message(conn: sqlite3.Connection, conversation_uuid: str, msg: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO message(uuid, conversation_uuid, sender, index_in_conversation, "
        "created_at, content, raw) "
        "VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(uuid) DO UPDATE SET sender=excluded.sender, "
        "index_in_conversation=excluded.index_in_conversation, "
        "content=excluded.content, raw=excluded.raw",
        (
            msg["uuid"],
            conversation_uuid,
            msg.get("sender"),
            msg.get("index"),
            msg.get("created_at"),
            json.dumps(msg.get("content") or msg.get("text")),
            json.dumps(msg),
        ),
    )


def upsert_attachment(conn: sqlite3.Connection, message_uuid: str, att: dict[str, Any]) -> None:
    auid = att.get("uuid") or att.get("file_uuid") or uuid.uuid4().hex
    conn.execute(
        "INSERT INTO attachment(uuid, message_uuid, file_name, file_kind, file_size, "
        "local_path, original_url) "
        "VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(uuid) DO UPDATE SET file_name=excluded.file_name, "
        "file_kind=excluded.file_kind, file_size=excluded.file_size, "
        "original_url=excluded.original_url",
        (
            auid,
            message_uuid,
            att.get("file_name"),
            att.get("file_kind"),
            att.get("file_size"),
            att.get("local_path"),
            att.get("original_url") or att.get("preview_url"),
        ),
    )


def upsert_style(conn: sqlite3.Connection, org_uuid: str, style: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO custom_style(uuid, org_uuid, name, prompt, examples, raw) "
        "VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(uuid) DO UPDATE SET name=excluded.name, prompt=excluded.prompt, "
        "examples=excluded.examples, raw=excluded.raw",
        (
            style["uuid"],
            org_uuid,
            style.get("name"),
            style.get("prompt"),
            json.dumps(style.get("examples") or []),
            json.dumps(style),
        ),
    )


def log_migration(
    conn: sqlite3.Connection,
    *,
    source_uuid: str,
    object_type: str,
    target_profile: str,
    target_uuid: str | None,
    status: str,
    error: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO migration_log(source_uuid, target_uuid, object_type, target_profile, "
        "migrated_at, status, error) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(source_uuid, target_profile) DO UPDATE SET "
        "target_uuid=excluded.target_uuid, object_type=excluded.object_type, "
        "migrated_at=excluded.migrated_at, status=excluded.status, error=excluded.error",
        (source_uuid, target_uuid, object_type, target_profile, now_iso(), status, error),
    )


def already_migrated(
    conn: sqlite3.Connection,
    source_uuid: str,
    target_profile: str,
) -> str | None:
    """Return the target_uuid if already successfully migrated, else None."""
    row = conn.execute(
        "SELECT target_uuid FROM migration_log "
        "WHERE source_uuid=? AND target_profile=? AND status='ok'",
        (source_uuid, target_profile),
    ).fetchone()
    return None if row is None else row["target_uuid"]


def fetch_one(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
    cur = conn.execute(sql, params)
    row: sqlite3.Row | None = cur.fetchone()
    return row


def fetch_all(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    cur = conn.execute(sql, params)
    return list(cur.fetchall())


def ensure_data_dir() -> None:
    data_dir().mkdir(parents=True, exist_ok=True)
