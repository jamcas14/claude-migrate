"""Archive-only migration mode: bundle every conversation as a project doc.

Skips the per-chat /completion path entirely. For each conversation in the
local source archive, render its transcript and POST it to a single Project
on target as a markdown knowledge file. The Project endpoint is plain CRUD,
not subject to the consumer /completion 5-hour usage bucket — 200 chats
finishes in *minutes* instead of hours.

Trade-off: chats live in one Project, not as individual entries in Recents.
Continuation ("open this chat and reply") isn't possible. For backup +
searchable archive use cases, functionally equivalent. Documented in README.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime

import structlog

from .errors import (
    AuthExpired,
    ClaudeMigrateError,
    CloudflareChallenge,
    EndpointChanged,
    NetworkError,
    RateLimited,
    SchemaDrift,
    TLSReject,
)
from .render import render_transcript
from .session import open_session
from .store import fetch_all, fetch_one, open_db, slugify

log = structlog.get_logger(__name__)

# We reuse the existing Pacer to space the doc-create calls. The /docs
# endpoint is not /completion, so it doesn't drain the 5-hour bucket — but
# claude.ai still has a per-account WAF rule on burst connection counts and
# server-side rate limits on CRUD endpoints, so a small inter-call sleep
# keeps us out of trouble.
ARCHIVE_DOC_BASE_SLEEP_SEC = 0.5
ARCHIVE_DOC_RATE_LIMIT_SLEEP_SEC = 30.0


@dataclass
class ArchiveSummary:
    """Per-run accounting for the archive-only path."""

    project_uuid: str | None = None
    project_name: str | None = None
    docs_created: int = 0
    docs_failed: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)


async def archive_to_project(
    target_profile: str,
    *,
    project_name: str | None = None,
) -> ArchiveSummary:
    """Migrate the entire source archive into a single Project on target.

    `project_name`: override for the auto-generated name. Default is
    ``"[archive] {source-email} {YYYY-MM-DD}"``.
    """
    summary = ArchiveSummary()
    async with open_session(target_profile) as session:
        conn = open_db()
        try:
            # Source identity for naming. The archive lives in the local DB;
            # we read the source-side `account` row to grab an email/profile.
            source_email = _source_email(conn) or "(unknown source)"
            today = datetime.now(UTC).strftime("%Y-%m-%d")
            name = project_name or f"[archive] {source_email} {today}"
            summary.project_name = name

            # Create the destination project. is_private=True so it doesn't
            # clutter shared organization space.
            created = await session.client.post_json(
                f"/api/organizations/{session.org_uuid}/projects",
                body={
                    "name": name,
                    "description": (
                        f"Archive of {source_email} migrated by claude-migrate "
                        f"on {today}. Each .md file is one conversation; ask "
                        f"this project questions about your prior history."
                    ),
                    "is_private": True,
                },
            )
            if not isinstance(created, dict) or not isinstance(created.get("uuid"), str):
                raise SchemaDrift(
                    "claude.ai's /projects POST didn't return a `uuid` field "
                    "for the new archive project. Schema drift on Anthropic's "
                    "side; the tool can't proceed without somewhere to write "
                    "the docs."
                )
            project_uuid = created["uuid"]
            summary.project_uuid = project_uuid
            log.info(
                "archive_project_created",
                project_uuid=project_uuid, name=name,
            )

            # Stream every conversation in the source archive into project docs.
            rows = fetch_all(
                conn,
                "SELECT uuid, title, created_at FROM conversation "
                "ORDER BY COALESCE(updated_at, created_at) ASC",
            )
            total = len(rows)
            for idx, row in enumerate(rows):
                conv_uuid = row["uuid"]
                title = row["title"] or "(untitled)"
                created_at = row["created_at"] or ""
                try:
                    body = render_transcript(conn, conv_uuid)
                except KeyError:
                    log.warning("archive_render_skipped", uuid=conv_uuid)
                    summary.docs_failed += 1
                    summary.failures.append((conv_uuid, "render failed"))
                    continue

                # Filename: include date prefix + slugified title for searchability.
                date_prefix = (created_at[:10] + "-") if created_at else ""
                file_name = f"{date_prefix}{slugify(title, max_len=60) or 'untitled'}.md"
                try:
                    await session.client.post_json(
                        f"/api/organizations/{session.org_uuid}"
                        f"/projects/{project_uuid}/docs",
                        body={"file_name": file_name, "content": body},
                    )
                    summary.docs_created += 1
                    if (idx + 1) % 25 == 0 or (idx + 1) == total:
                        log.info(
                            "archive_progress", done=idx + 1, total=total,
                        )
                except (
                    AuthExpired, CloudflareChallenge, TLSReject,
                ):
                    # Session-fatal — let it propagate so the orchestrator
                    # can prompt re-auth. Project + docs created so far
                    # remain on target; user can re-run to resume (the
                    # idempotency story is "duplicate file_name silently
                    # creates a second doc," so they should rename or
                    # delete the partial project before re-running).
                    raise
                except RateLimited as e:
                    log.warning(
                        "archive_doc_rate_limited",
                        uuid=conv_uuid, retry_after_sec=e.retry_after_sec,
                    )
                    summary.docs_failed += 1
                    summary.failures.append((conv_uuid, f"rate-limited: {e}"))
                except (
                    EndpointChanged, NetworkError, ClaudeMigrateError,
                ) as e:
                    log.warning(
                        "archive_doc_failed",
                        uuid=conv_uuid, err=str(e), err_type=type(e).__name__,
                    )
                    summary.docs_failed += 1
                    summary.failures.append((conv_uuid, str(e)))
        finally:
            conn.close()
    return summary


def _source_email(conn: sqlite3.Connection) -> str | None:
    """Best-effort source email lookup from the local archive's `account` row."""
    row = fetch_one(conn, "SELECT raw FROM account LIMIT 1")
    if row is None:
        return None
    try:
        import json as json_mod
        parsed = json_mod.loads(row["raw"])
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    email = parsed.get("email_address") or parsed.get("email")
    return email if isinstance(email, str) else None
