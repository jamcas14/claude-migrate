"""Projection of `migration_log` onto a single (conn, target_profile) pair.

Every restore-time read or write of the idempotency table goes through here so
the schema, the composite-PK semantics, the status='ok' filter, and the
transactional wrapping live in one place.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .store import already_migrated, log_migration, transaction


class RestoreState:
    """Owns `migration_log` for one target profile.

    Methods are keyed off `source_uuid`; the (conn, target_profile) pair is
    captured once at construction time. The composite-PK on the table means
    `mark_ok` after a previous `mark_error` overwrites the error row — that's
    intentional and how the tool resumes after a partial failure.
    """

    def __init__(self, conn: sqlite3.Connection, target_profile: str) -> None:
        self.conn = conn
        self.target_profile = target_profile

    # -- per-row writes --------------------------------------------------

    def mark_ok(
        self,
        *,
        source_uuid: str,
        object_type: str,
        target_uuid: str | None,
    ) -> None:
        with transaction(self.conn):
            log_migration(
                self.conn,
                source_uuid=source_uuid,
                object_type=object_type,
                target_profile=self.target_profile,
                target_uuid=target_uuid,
                status="ok",
            )

    def mark_error(
        self,
        *,
        source_uuid: str,
        object_type: str,
        error: str,
    ) -> None:
        with transaction(self.conn):
            log_migration(
                self.conn,
                source_uuid=source_uuid,
                object_type=object_type,
                target_profile=self.target_profile,
                target_uuid=None,
                status="error",
                error=error,
            )

    def drop(self, source_uuid: str) -> None:
        """Delete a row so the next restore re-attempts the object.

        Single statement, autocommit mode → no surrounding transaction needed.
        (`with self.conn:` would be a no-op here: sqlite3.Connection only
        wraps BEGIN/COMMIT when `isolation_level is not None`.)
        """
        self.conn.execute(
            "DELETE FROM migration_log WHERE source_uuid=? AND target_profile=?",
            (source_uuid, self.target_profile),
        )

    # -- per-row reads ---------------------------------------------------

    def already_migrated(self, source_uuid: str) -> str | None:
        return already_migrated(self.conn, source_uuid, self.target_profile)

    # -- aggregate reads -------------------------------------------------

    def project_map(self) -> dict[str, str]:
        """source_uuid → target_uuid for projects already migrated `ok`.

        Used to wire conversations to their parent project on the target even
        when projects were migrated in a previous run.
        """
        rows = self.conn.execute(
            "SELECT source_uuid, target_uuid FROM migration_log "
            "WHERE object_type='project' AND target_profile=? AND status='ok' "
            "AND target_uuid IS NOT NULL",
            (self.target_profile,),
        ).fetchall()
        return {r["source_uuid"]: r["target_uuid"] for r in rows}

    def migrated_count(self, object_type: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM migration_log "
            "WHERE target_profile=? AND object_type=? AND status='ok'",
            (self.target_profile, object_type),
        ).fetchone()
        return int(row[0])

    _PENDING_TABLES = frozenset({"project", "custom_style", "conversation"})

    def pending_count(self, source_table: str) -> int:
        """Rows in `source_table` that don't yet have a status='ok' log entry."""
        # Whitelist guard: source_table is interpolated into SQL, so refuse
        # anything we don't recognize. Belt-and-suspenders against future
        # callers passing an attacker-controlled value.
        if source_table not in self._PENDING_TABLES:
            raise ValueError(
                f"pending_count: refusing unknown table {source_table!r}; "
                f"allowed: {sorted(self._PENDING_TABLES)}"
            )
        row = self.conn.execute(
            f"SELECT COUNT(*) FROM {source_table} t "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM migration_log m "
            "  WHERE m.source_uuid=t.uuid AND m.target_profile=? AND m.status='ok'"
            ")",
            (self.target_profile,),
        ).fetchone()
        return int(row[0])

    def recent_failures(self, *, limit: int = 10) -> list[dict[str, Any]]:
        # rowid DESC tiebreaker: Windows clock resolution can stamp two writes
        # with identical migrated_at, leaving the order undefined otherwise.
        rows = self.conn.execute(
            "SELECT source_uuid, object_type, error, migrated_at "
            "FROM migration_log "
            "WHERE target_profile=? AND status='error' "
            "ORDER BY migrated_at DESC, rowid DESC LIMIT ?",
            (self.target_profile, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def last_activity(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT migrated_at, status FROM migration_log "
            "WHERE target_profile=? ORDER BY migrated_at DESC, rowid DESC LIMIT 1",
            (self.target_profile,),
        ).fetchone()
        return dict(row) if row else None

    def confirmed_conversations(self) -> list[tuple[str, str]]:
        """(source_uuid, target_uuid) for every conversation logged ok with a target_uuid."""
        rows = self.conn.execute(
            "SELECT source_uuid, target_uuid FROM migration_log "
            "WHERE target_profile=? AND object_type='conversation' "
            "AND status='ok' AND target_uuid IS NOT NULL",
            (self.target_profile,),
        ).fetchall()
        return [(r["source_uuid"], r["target_uuid"]) for r in rows]
