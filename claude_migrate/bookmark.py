"""Bookmark-mode migration: empty named chats, transcripts loaded on demand.

Two phases, separately invoked:

* **Bookmark** (`migrate ... --bookmark`): for each source conversation, POST
  `/chat_conversations` with name `[unloaded] [YYYY-MM-DD] Title`. No
  `/completion`, no transcript paste, no project. The chat appears in target's
  Recents as an empty stub. `migration_log` records each row with
  `status='bookmarked'`.

* **Load** (`claude-migrate load TARGET PATTERN`): pick one (or many) of the
  bookmarked stubs, render its transcript from the local archive, and paste
  it via `/completion` using the existing `transport.send_payload` path.
  On success: rename the chat to strip the `[unloaded]` prefix and flip
  `migration_log` from `status='bookmarked'` to `status='ok'`.

Why not put the transcript inside a per-chat Project (the alternative
considered)? Because that creates one Project per source chat, which clutters
the Projects panel for desktop users. Bookmark mode trades the auto-load-on-
first-message UX for a clean Projects panel — at the cost of needing the
terminal once per chat the user wants to materialise.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

import structlog

from .client import ClaudeClient
from .errors import (
    AuthExpired,
    ClaudeMigrateError,
    ClientVersionStale,
    CloudflareChallenge,
    EndpointChanged,
    NetworkError,
    RateLimited,
    SchemaDrift,
    TLSReject,
)
from .render import prepare_paste_payload
from .runner import Pacer, WorkerOutcome
from .session import open_session
from .state import RestoreState
from .store import fetch_all, fetch_one, open_db
from .transport import send_payload

log = structlog.get_logger(__name__)

UNLOADED_PREFIX_RE = re.compile(r"^\[ul(?:\||\])")
"""Detect a bookmark stub's name on target. Matches both `[ul|YYYY-MM-DD]`
(current format) and `[ul]` (no-date fallback). Used by maintenance scripts
that need to confirm a chat is in the unloaded state before mutating it."""

# Bookmark phase only hits /chat_conversations CRUD, no /completion. The WAF
# still enforces a per-account burst rate, so we pace lightly.
BOOKMARK_BASE_SLEEP_SEC = 0.5
BOOKMARK_RATE_LIMIT_SLEEP_SEC = 30.0

# Load phase hits /completion — same path as default-mode restore. Inherit
# the same conservative pacing so a multi-chat `load --all` run respects the
# 5-hour bucket the same way `migrate` does.
LOAD_BASE_SLEEP_SEC = 5.0
LOAD_RATE_LIMIT_SLEEP_SEC = 60.0

# Match the conversation-restore phase: 5 consecutive 429s with no successes
# means client-side pacing can't fix this; abort instead of burning more
# attempts.
CASCADE_ABORT_THRESHOLD = 5

# Treat a bare `[0-9a-f]{6,}$` as a UUID prefix lookup, OR pull a full UUID
# out of any longer text (URL paste, Cookie row paste, etc.) and exact-match
# it. Otherwise the pattern is a substring against the chat title.
_UUID_PREFIX_RE = re.compile(r"^[0-9a-f]{6,}$", re.IGNORECASE)
_UUID_FULL_IN_TEXT_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)


@dataclass
class BookmarkSummary:
    """Per-run accounting for the `--bookmark` migrate phase."""

    conversations_total: int = 0
    conversations_bookmarked: int = 0
    skipped: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)
    cascade_aborted: bool = False


@dataclass
class LoadCandidate:
    """One bookmarked chat in the picker / pattern-filter pipeline."""

    source_uuid: str
    target_uuid: str
    title: str
    """Source-side title (resolved from local archive). Used for matching;
    the *target* chat's name carries the `[unloaded]` prefix."""


@dataclass
class LoadSummary:
    """Per-run accounting for `claude-migrate load`."""

    matched: int = 0
    loaded: int = 0
    skipped_already_loaded: int = 0
    skipped_non_empty: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _date_prefix(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _bookmark_chat_name(title: str, date: str) -> str:
    """`[ul|YYYY-MM-DD] Title` — compact stub indicator combined with the
    source `created_at` date in one bracket-pair. The `ul` token is the
    in-UI signal that the chat is unloaded (don't type yet). Total prefix
    is ~16 chars vs `[unloaded] [YYYY-MM-DD]` (~24), giving the actual
    title ~3 more words of room in claude.ai's Recents sidebar while still
    showing chronological context.

    On load, `_loaded_chat_name` produces `[YYYY-MM-DD] Title` (default-mode
    shape) so loaded-from-bookmark chats are visually identical to default-
    mode migrations.
    """
    base = (title or "").strip() or "(untitled)"
    inner = f"[ul|{date}]" if date else "[ul]"
    return f"{inner} {base}"


def _loaded_chat_name(title: str, date: str) -> str:
    """Name applied AFTER a successful load — matches default-mode format
    `[YYYY-MM-DD] Title` so loaded-from-bookmark chats look identical to
    default-mode migrations in Recents."""
    base = (title or "").strip() or "(untitled)"
    return f"[{date}] {base}" if date else base


# ---------------------------------------------------------------------------
# Bookmark phase: create empty stubs in Recents
# ---------------------------------------------------------------------------


async def restore_bookmarks(
    client: ClaudeClient,
    conn: sqlite3.Connection,
    target_org: str,
    state: RestoreState,
    *,
    dry_run: bool,
    summary: BookmarkSummary,
) -> None:
    """For each source conversation, POST `/chat_conversations` with the
    `[unloaded]` prefix. No transcript paste, no project membership.

    Strictly serial under a Pacer. Re-runs are idempotent: chats already
    bookmarked OR already fully loaded are skipped via `migration_log`.
    """
    rows = fetch_all(
        conn,
        "SELECT uuid, title, created_at, updated_at FROM conversation "
        "ORDER BY COALESCE(updated_at, created_at) ASC",
    )
    summary.conversations_total = len(rows)
    if dry_run or not rows:
        return

    pacer = Pacer(
        base_sleep_sec=BOOKMARK_BASE_SLEEP_SEC,
        rate_limit_sleep_sec=BOOKMARK_RATE_LIMIT_SLEEP_SEC,
    )
    total = len(rows)

    for idx, row in enumerate(rows):
        if pacer.consecutive_rate_limits >= CASCADE_ABORT_THRESHOLD:
            log.error(
                "bookmark_cascade_abort",
                consecutive=pacer.consecutive_rate_limits,
                hint=(
                    "Account is rate-limited on /chat_conversations; "
                    "aborting. Re-run later (idempotent)."
                ),
            )
            summary.cascade_aborted = True
            return

        source_uuid = row["uuid"]
        # Skip if loaded OR already bookmarked.
        if state.already_migrated(source_uuid) or state.already_bookmarked(source_uuid):
            summary.skipped += 1
            continue

        title = (row["title"] or "(untitled)")[:60]
        date = _date_prefix(row["created_at"])
        chat_name = _bookmark_chat_name(row["title"] or "", date)

        await pacer.before()
        log.info(
            "bookmark_start",
            progress=f"{idx + 1}/{total}",
            source=source_uuid[:8],
            title=title,
        )
        outcome = await _create_one_bookmark(client, target_org, chat_name)
        if outcome.target_uuid is not None:
            state.mark_bookmarked(
                source_uuid=source_uuid, target_uuid=outcome.target_uuid,
            )
            summary.conversations_bookmarked += 1
            log.info(
                "bookmark_ok",
                progress=f"{idx + 1}/{total}",
                source=source_uuid[:8],
                target=outcome.target_uuid[:8],
            )
        else:
            summary.failures.append((source_uuid, outcome.error or "unknown"))
            log.warning(
                "bookmark_fail",
                progress=f"{idx + 1}/{total}",
                source=source_uuid[:8],
                err=outcome.error,
            )
        await pacer.after(outcome)


async def _create_one_bookmark(
    client: ClaudeClient, target_org: str, chat_name: str,
) -> WorkerOutcome:
    """Single POST /chat_conversations. No project_uuid, no body beyond the
    name. Returns a `WorkerOutcome` so the caller can feed it to the Pacer."""
    try:
        created = await client.post_json(
            f"/api/organizations/{target_org}/chat_conversations",
            body={"name": chat_name},
        )
    except RateLimited as e:
        return WorkerOutcome.failed(
            f"bookmark: {e}",
            rate_limited=True,
            retry_after_sec=e.retry_after_sec,
        )
    except (AuthExpired, CloudflareChallenge, TLSReject):
        # Session-fatal — let the orchestrator surface re-auth.
        raise
    except (
        ClientVersionStale, EndpointChanged, NetworkError, SchemaDrift,
    ) as e:
        return WorkerOutcome.failed(f"bookmark: {type(e).__name__}: {e}")

    chat_uuid = created.get("uuid") if isinstance(created, dict) else None
    if not isinstance(chat_uuid, str):
        return WorkerOutcome.failed(
            "bookmark: claude.ai's POST /chat_conversations didn't return a "
            "`uuid` field for the new empty stub. Schema drift on Anthropic's "
            "side; the tool needs updating to match."
        )
    return WorkerOutcome.ok(chat_uuid)


# ---------------------------------------------------------------------------
# Load phase: materialise a bookmarked stub by pasting the transcript
# ---------------------------------------------------------------------------


async def load_bookmarks(
    target_profile: str,
    candidates: list[LoadCandidate] | None = None,
    *,
    force: bool = False,
) -> LoadSummary:
    """Materialise the given bookmarked chats.

    `candidates`: the exact list to load — selection (pattern matching,
    interactive picker, --all) is the caller's job. Pass `None` (the
    default) to load every bookmark on this profile, equivalent to the CLI's
    `--all` flag.

    `force=False`: skip target chats that already have any messages. The
    typical "user typed a question into the empty stub before running load"
    case — appending a transcript afterward produces a confusing chat
    history. `force=True` overrides for cases where the user knows what
    they're doing.

    Earlier versions of this function did pattern filtering + an interactive
    picker callback internally, which led to a load_all/pick interaction bug
    where `--all` would silently bypass the pick callback. Splitting
    selection (now in the CLI) from loading (here) eliminates that whole
    class of mistake.
    """
    summary = LoadSummary()
    async with open_session(target_profile) as session:
        conn = open_db()
        try:
            state = RestoreState(conn, target_profile)
            if candidates is None:
                candidates = _resolve_candidates(conn, state)
            summary.matched = len(candidates)
            if not candidates:
                return summary

            pacer = Pacer(
                base_sleep_sec=LOAD_BASE_SLEEP_SEC,
                rate_limit_sleep_sec=LOAD_RATE_LIMIT_SLEEP_SEC,
            )
            total = len(candidates)

            for idx, cand in enumerate(candidates):
                if pacer.consecutive_rate_limits >= CASCADE_ABORT_THRESHOLD:
                    log.error(
                        "load_cascade_abort",
                        consecutive=pacer.consecutive_rate_limits,
                    )
                    break

                await pacer.before()
                log.info(
                    "load_start",
                    progress=f"{idx + 1}/{total}",
                    source=cand.source_uuid[:8],
                    target=cand.target_uuid[:8],
                    title=cand.title[:60],
                )
                outcome = await _materialise_one_bookmark(
                    client=session.client,
                    conn=conn,
                    target_org=session.org_uuid,
                    state=state,
                    candidate=cand,
                    force=force,
                )
                if outcome.target_uuid is not None:
                    summary.loaded += 1
                    log.info(
                        "load_ok",
                        progress=f"{idx + 1}/{total}",
                        target=cand.target_uuid[:8],
                    )
                elif outcome.error and "already loaded" in outcome.error:
                    summary.skipped_already_loaded += 1
                elif outcome.error and "non_empty" in outcome.error:
                    summary.skipped_non_empty += 1
                    summary.failures.append((cand.source_uuid, outcome.error))
                else:
                    summary.failures.append(
                        (cand.source_uuid, outcome.error or "unknown"),
                    )
                    log.warning(
                        "load_fail",
                        target=cand.target_uuid[:8],
                        err=outcome.error,
                    )
                await pacer.after(outcome)
        finally:
            conn.close()
    return summary


def _resolve_candidates(
    conn: sqlite3.Connection, state: RestoreState,
) -> list[LoadCandidate]:
    """Build the list of bookmarked chats with their source-side titles
    resolved from the local archive."""
    out: list[LoadCandidate] = []
    for source_uuid, target_uuid in state.bookmarked_conversations():
        row = fetch_one(
            conn, "SELECT title FROM conversation WHERE uuid=?", (source_uuid,),
        )
        title = (row["title"] if row else None) or "(untitled)"
        out.append(LoadCandidate(
            source_uuid=source_uuid,
            target_uuid=target_uuid,
            title=title,
        ))
    out.sort(key=lambda c: c.title.lower())
    return out


def _filter_candidates(
    candidates: list[LoadCandidate],
    *,
    pattern: str | None,
    load_all: bool,
) -> list[LoadCandidate]:
    """Filter `candidates` by the user's pattern. Match precedence:

    1. **`load_all=True` or empty pattern** → every candidate.
    2. **Full UUID anywhere in the input** → exact-match against
       `target_uuid` OR `source_uuid`. Lets the user paste
       `https://claude.ai/chat/<uuid>` straight from the browser URL
       bar, or just the bare UUID, without trimming.
    3. **Bare hex prefix `[0-9a-f]{6,}`** → prefix-match the same fields.
       Useful when the user has a partial uuid in clipboard.
    4. **Anything else** → case-insensitive substring against the title.
    """
    if load_all:
        return list(candidates)
    if pattern is None:
        return list(candidates)
    p = pattern.strip()
    if not p:
        return list(candidates)

    # 2. URL / full-uuid extraction.
    m = _UUID_FULL_IN_TEXT_RE.search(p)
    if m:
        full_uuid = m.group(0).lower()
        return [
            c for c in candidates
            if c.target_uuid.lower() == full_uuid
            or c.source_uuid.lower() == full_uuid
        ]

    # 3. Bare hex prefix.
    if _UUID_PREFIX_RE.match(p):
        p_lower = p.lower()
        return [
            c for c in candidates
            if c.target_uuid.lower().startswith(p_lower)
            or c.source_uuid.lower().startswith(p_lower)
        ]

    # 4. Title substring.
    p_lower = p.lower()
    return [c for c in candidates if p_lower in c.title.lower()]


async def _materialise_one_bookmark(
    *,
    client: ClaudeClient,
    conn: sqlite3.Connection,
    target_org: str,
    state: RestoreState,
    candidate: LoadCandidate,
    force: bool,
) -> WorkerOutcome:
    """For one bookmarked chat: fetch current state, render transcript,
    paste via /completion, rename to strip the `[unloaded]` prefix, flip
    migration_log to `status='ok'`."""
    # Fetch the target chat to (a) know if it already has messages, and
    # (b) read the current name for renaming. One GET serves both needs.
    try:
        existing = await client.get_json(
            f"/api/organizations/{target_org}"
            f"/chat_conversations/{candidate.target_uuid}",
            params={"tree": "True", "rendering_mode": "messages"},
            timeout=15.0,
        )
    except EndpointChanged:
        # 404: the bookmarked chat was deleted from claude.ai's UI. Tell
        # the user how to reconcile.
        return WorkerOutcome.failed(
            f"load: target chat {candidate.target_uuid[:8]} not found "
            f"(deleted from claude.ai?). Run `claude-migrate verify "
            f"<target> --reconcile` to drop the migration_log entry, then "
            f"re-bookmark."
        )
    except RateLimited as e:
        return WorkerOutcome.failed(
            f"load: {e}",
            rate_limited=True,
            retry_after_sec=e.retry_after_sec,
        )
    except (AuthExpired, CloudflareChallenge, TLSReject):
        raise
    except (ClientVersionStale, NetworkError, SchemaDrift) as e:
        return WorkerOutcome.failed(f"load: {type(e).__name__}: {e}")

    if not isinstance(existing, dict):
        return WorkerOutcome.failed(
            f"load: GET /chat_conversations/{candidate.target_uuid} returned "
            f"a {type(existing).__name__} (expected an object). Schema drift."
        )

    msgs = existing.get("chat_messages")
    has_messages = isinstance(msgs, list) and len(msgs) > 0
    if has_messages and not force:
        return WorkerOutcome.failed(
            f"load: target chat {candidate.target_uuid[:8]} is non_empty "
            f"({len(msgs) if isinstance(msgs, list) else '?'} message(s)) — "
            f"refusing to append the transcript on top. Pass --force to "
            f"override, or delete the existing messages on claude.ai first."
        )

    # Render and paste. Reuses the same payload sizing + send strategy as
    # the default-mode restore — inline / attachment / chunked are all
    # handled by `send_payload`.
    payload = prepare_paste_payload(conn, candidate.source_uuid)
    try:
        await send_payload(
            client, target_org, candidate.target_uuid, payload,
        )
    except RateLimited as e:
        return WorkerOutcome.failed(
            f"load: {e}",
            rate_limited=True,
            retry_after_sec=e.retry_after_sec,
        )
    except (AuthExpired, CloudflareChallenge, TLSReject):
        raise
    except (ClientVersionStale, NetworkError, SchemaDrift) as e:
        return WorkerOutcome.failed(f"load: {type(e).__name__}: {e}")

    # Rename to default-mode shape `[YYYY-MM-DD] Title` so loaded-from-
    # bookmark chats are visually identical to default-mode migrations.
    # Bookmark titles only carry `[unloaded] Title` (no date) — the date
    # comes back from the source archive here. Best-effort: a rename
    # failure doesn't undo the paste, so we still mark the chat as loaded.
    src_row = fetch_one(
        conn,
        "SELECT title, created_at FROM conversation WHERE uuid=?",
        (candidate.source_uuid,),
    )
    src_title = (src_row["title"] if src_row else None) or candidate.title
    src_date = _date_prefix(src_row["created_at"] if src_row else None)
    new_name = _loaded_chat_name(src_title, src_date)
    try:
        await client.put_json(
            f"/api/organizations/{target_org}"
            f"/chat_conversations/{candidate.target_uuid}",
            body={"name": new_name},
            timeout=15.0,
        )
    except ClaudeMigrateError as e:
        log.warning(
            "load_rename_failed",
            target=candidate.target_uuid[:8],
            err=str(e),
        )

    # Flip migration_log: bookmarked → ok. The composite-PK upsert in
    # `log_migration` overwrites the existing row in place.
    state.mark_ok(
        source_uuid=candidate.source_uuid,
        object_type="conversation",
        target_uuid=candidate.target_uuid,
    )
    return WorkerOutcome.ok(candidate.target_uuid)


# ---------------------------------------------------------------------------
# Convenience: discover bookmarked chats for the CLI (no network).
# ---------------------------------------------------------------------------


def list_bookmarked(target_profile: str) -> list[LoadCandidate]:
    """Read-only list of all bookmarked chats for one target profile.
    Used by `claude-migrate load`'s no-arg interactive picker."""
    conn = open_db()
    try:
        state = RestoreState(conn, target_profile)
        return _resolve_candidates(conn, state)
    finally:
        conn.close()


# Convenience re-export so tests can import the lifecycle worker directly.
__all__ = [
    "UNLOADED_PREFIX_RE",
    "BookmarkSummary",
    "LoadCandidate",
    "LoadSummary",
    "_bookmark_chat_name",
    "_filter_candidates",
    "_loaded_chat_name",
    "list_bookmarked",
    "load_bookmarks",
    "restore_bookmarks",
]
