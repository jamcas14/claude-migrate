"""User-facing CLI.

Verb-first commands with positional arguments for the primary noun. Operations
that mutate a remote account confirm with `Proceed? [y/N]` before running; pass
`--yes` to skip the prompt or `--dry-run` to preview without prompting. Profile
names are arbitrary strings (`source`, `target`, `work`, `personal-old`, etc.)
— there's no special role beyond the name.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn

import click
import structlog

from . import __version__
from .auth import (
    list_profiles,
    load_profile,
    remove_profile,
    run_auth_flow,
    validate_profile_name,
    verify_profile,
)
from .config import (
    ANTHROPIC_CLIENT_VERSION_DEFAULT,
    config_dir,
    data_dir,
    db_path,
    load_settings,
)
from .errors import (
    AuthError,
    AuthExpired,
    AuthInvalid,
    AuthMissing,
    ClaudeMigrateError,
    ClientVersionStale,
    CloudflareChallenge,
    NetworkError,
)
from .fetch import dump_all
from .memory import prepare as memory_prepare
from .memory import verify_open as memory_verify_open
from .migrate import dry_run_plan, migration_status, run_restore, verify_target_conversations
from .notify import notify
from .render import (
    AttachmentPayload,
    ChunkedPayload,
    InlinePayload,
    prepare_paste_payload,
    render_transcript,
)
from .restore import delete_conversation, find_orphan_conversations, reorder_conversations
from .scheduler import (
    detect_backend,
    install_timer,
    timer_status,
    uninstall_timer,
)
from .session import open_session
from .state import RestoreState
from .store import ensure_data_dir, open_db

EXIT_TEMPFAIL = 75
EXIT_TOS = 64


def _profile_arg_callback(
    ctx: click.Context, param: click.Parameter, value: str | None,
) -> str | None:
    """Click callback. Rejects profile names that could leak into scheduler
    subprocess args (schtasks /TR, systemd ExecStart, cron lines), SQL keys,
    or filesystem paths. Allowed: ``[A-Za-z0-9._-]{1,64}``."""
    if value is None:
        return None
    try:
        validate_profile_name(value)
    except AuthInvalid as e:
        raise click.BadParameter(str(e)) from e
    return value

TOS_BANNER = (
    "Heads up: Anthropic's Consumer Terms (§3.4 prohibits scraping, §3.7\n"
    "prohibits automation) restrict the kind of API access this tool performs.\n"
    "By using claude-migrate you accept the risk that Anthropic may rate-limit,\n"
    "suspend, or terminate the affected accounts. The tool is intended for\n"
    "migrating between YOUR OWN accounts.\n\n"
    "Re-run with --i-understand-tos-risk to proceed.\n"
)

CLIENT_VERSION_HELP = """\
Only needed if /api/* returns HTTP 400 or 422. claude.ai sends a few
`anthropic-*` request headers as a build fingerprint that rotates every few
weeks; when stale, the API rejects.

Capture them once from your browser:

  1. Open https://claude.ai (signed in) → F12 → Network tab.
  2. Click any /api/* request → right-side "Request Headers" → copy:
       anthropic-client-version  (e.g. "1.0.0")
       anthropic-client-sha      (40-char hex; rotates most often)
       anthropic-anonymous-id    (optional, claudeai.v1.<uuid>)
       anthropic-device-id       (optional, <uuid>)

Set them via either:
  • `claude-migrate config edit` — opens config.toml in your $EDITOR
  • environment variables: CLAUDE_MIGRATE_CLIENT_VERSION,
    CLAUDE_MIGRATE_CLIENT_SHA, CLAUDE_MIGRATE_ANONYMOUS_ID,
    CLAUDE_MIGRATE_DEVICE_ID

Verify with `claude-migrate doctor`, then re-run the failed command.
See the README for screenshots of the DevTools steps.
"""

CONFIG_TEMPLATE = """\
# claude-migrate configuration
#
# All fields are optional. Set the `anthropic-*` headers below ONLY if you hit
# HTTP 400/422 from /api/* — the headers rotate every few weeks. Capture from
# DevTools → Network → any /api/* request → Request Headers.
#
# Run `claude-migrate headers-help` for the capture walkthrough.

# client_version = "1.0.0"
# client_sha     = ""    # 40-char hex, anthropic-client-sha — most important
# anonymous_id   = ""    # optional, anthropic-anonymous-id (claudeai.v1.<uuid>)
# device_id      = ""    # optional, anthropic-device-id

# Per-chat sleep during restore. Default 90s keeps most accounts under the
# /completion rate limit. Lower values are faster but more likely to 429.
# chat_sleep_sec = 90.0
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_logging(quiet: bool, verbose: bool) -> None:
    level = logging.WARNING if quiet else (logging.DEBUG if verbose else logging.INFO)
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(level),
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()),
        ],
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    )


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run a coroutine, mapping typed errors → friendly exit codes.

    On any caught error the function exits the process via `sys.exit`, so the
    declared return type holds: callers receive `T` or never return.
    """
    try:
        return asyncio.run(coro)
    except KeyboardInterrupt:
        click.echo("\nInterrupted.", err=True)
        _exit(130)
    except AuthExpired as e:
        click.echo(f"\nSession expired: {e}", err=True)
        click.echo(
            "Run `claude-migrate add <profile>` to re-paste cookies, then "
            "re-run the same command to resume.",
            err=True,
        )
        notify("claude-migrate", "Session expired — re-auth required.")
        _exit(EXIT_TEMPFAIL)
    except CloudflareChallenge as e:
        click.echo(f"\nCloudflare challenge: {e}", err=True)
        click.echo(
            "Refresh https://claude.ai once in your browser (this gets you a "
            "fresh cf_clearance), then `claude-migrate add <profile>`.",
            err=True,
        )
        _exit(EXIT_TEMPFAIL)
    except ClientVersionStale as e:
        settings = load_settings()
        click.echo(f"\n{e}", err=True)
        click.echo(
            f"\nCurrent value: client_version={settings.client_version!r}\n",
            err=True,
        )
        click.echo(CLIENT_VERSION_HELP, err=True)
        _exit(EXIT_TEMPFAIL)
    except (AuthInvalid, AuthMissing) as e:
        click.echo(f"\n{e}", err=True)
        _exit(2)
    except AuthError as e:
        click.echo(f"\nAuth error: {e}", err=True)
        _exit(2)
    except NetworkError as e:
        click.echo(f"\nNetwork error: {e}", err=True)
        _exit(EXIT_TEMPFAIL)
    except ClaudeMigrateError as e:
        click.echo(f"\nError: {e}", err=True)
        _exit(1)


def _exit(code: int) -> NoReturn:
    """Indirection so mypy sees `_run` as `T | NoReturn`. Inlined would let mypy
    fall through past the except blocks and infer an implicit `None` return."""
    sys.exit(code)


def _ensure_tos(ack: bool) -> None:
    """First-run TOS acknowledgement. Stored as `~/.config/claude-migrate/tos.ack`."""
    state = config_dir() / "tos.ack"
    if state.exists():
        return
    if not ack:
        click.echo(TOS_BANNER, err=True)
        sys.exit(EXIT_TOS)
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(datetime.now(UTC).isoformat() + "\n", "utf-8")


# ---------------------------------------------------------------------------
# Top-level group
# ---------------------------------------------------------------------------


@click.group(
    help=(
        "Migrate or back up a Claude.ai consumer account.\n\n"
        "Run `claude-migrate add source` to store credentials for a profile, then\n"
        "`claude-migrate migrate source target` to clone source to target."
    ),
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.version_option(__version__)
@click.option("--quiet", is_flag=True, help="Suppress info-level logs.")
@click.option("--verbose", is_flag=True, help="Enable debug-level logs.")
@click.pass_context
def cli(ctx: click.Context, quiet: bool, verbose: bool) -> None:
    ctx.ensure_object(dict)
    ctx.obj["quiet"] = quiet
    _setup_logging(quiet=quiet, verbose=verbose)


# ---------------------------------------------------------------------------
# Account / profile lifecycle
# ---------------------------------------------------------------------------


@cli.command(help="Add a profile (interactive cookie paste).")
@click.argument("name", callback=_profile_arg_callback)
def add(name: str) -> None:
    """Adds a stored profile, or re-pastes cookies if NAME already exists.
    Idempotent — running against an existing name overwrites the stored
    cookies (use this to refresh after the session expires).

    Note: this is purely local — no real "session" is created on Anthropic's
    side. The cookies live in your OS keychain only.
    """
    _run(run_auth_flow(name, refreshing=_profile_exists(name)))


@cli.command(help="Remove a stored profile from the OS keychain.")
@click.argument("name", callback=_profile_arg_callback)
@click.confirmation_option(prompt="Delete this profile from the keychain?")
def remove(name: str) -> None:
    """Local-only — deletes the cookie blob from your keychain. Does NOT
    invalidate the cookie on Anthropic's side; the original browser session
    keeps working."""
    remove_profile(name)
    click.echo(f"Removed profile {name!r}.")


@cli.command(help="List stored profiles (no network calls).")
def accounts() -> None:
    names = list_profiles()
    if not names:
        click.echo("No profiles stored.")
        click.echo("  → `claude-migrate add <name>` to store credentials for one.")
        return
    width = max(len("PROFILE"), max(len(n) for n in names))
    click.echo(f"  {'PROFILE':<{width}}  EMAIL                            LAST PROBE")
    for n in names:
        try:
            p = load_profile(n)
            email = p.email or "?"
            probe = p.last_probe_ok or "(never probed)"
            click.echo(f"  {n:<{width}}  {email:<32} {probe}")
        except AuthError as e:
            click.echo(f"  {n:<{width}}  (error: {e})")
    click.echo("")
    click.echo("  → `claude-migrate add <name>`     add or re-paste cookies (idempotent)")
    click.echo("  → `claude-migrate rename OLD NEW` rename a profile")
    click.echo("  → `claude-migrate remove <name>`  delete a profile from the keychain")
    click.echo("  → `claude-migrate whoami <name>`  live-probe a profile's credentials")


@cli.command(help="Rename a stored profile (no network call, no re-paste needed).")
@click.argument("old_name", callback=_profile_arg_callback)
@click.argument("new_name", callback=_profile_arg_callback)
def rename(old_name: str, new_name: str) -> None:
    """Local metadata operation. Useful for typo fixes (`source` → `soource`)
    or naming changes (`work` → `work-old`). The cookies and the discovered
    org/email move under the new name; the OLD name is removed."""
    if old_name == new_name:
        click.echo(f"Source and destination are both {old_name!r}; nothing to do.")
        return
    profile = load_profile(old_name)  # raises AuthMissing if not found
    if _profile_exists(new_name):
        click.echo(
            f"A profile named {new_name!r} already exists. Run "
            f"`claude-migrate remove {new_name}` first if you want to overwrite it.",
            err=True,
        )
        sys.exit(2)
    from .auth import store_profile  # local to keep top-level imports tight
    store_profile(new_name, profile)
    try:
        remove_profile(old_name)
    except Exception as e:
        # Roll back the new write so we don't leave both names live with
        # identical credentials. The user can retry; a re-run of `rename`
        # will cleanly find only the old name.
        with contextlib.suppress(Exception):
            remove_profile(new_name)
        click.echo(
            f"Could not remove old profile {old_name!r}: {e}. "
            f"Rolled back the rename. No credentials were lost.",
            err=True,
        )
        sys.exit(1)
    click.echo(f"Renamed {old_name!r} → {new_name!r}.")


@cli.command(help="Probe a profile to confirm its credentials still work.")
@click.argument("name", callback=_profile_arg_callback)
def whoami(name: str) -> None:
    """Hits /api/bootstrap with the stored cookies, prints the authenticated
    identity, and updates the profile's `last_probe_ok` timestamp on success."""
    result = _run(verify_profile(name))
    p = load_profile(name)
    click.echo(f"  ✓ {p.email or 'unknown email'}")
    if result.org_name:
        click.echo(f"    organization:  {result.org_name}")
    click.echo(f"    last probe ok: {p.last_probe_ok}")


def _profile_exists(name: str) -> bool:
    try:
        load_profile(name)
    except AuthMissing:
        return False
    except AuthError:
        return False
    return True


# ---------------------------------------------------------------------------
# Backup (one-shot dump)
# ---------------------------------------------------------------------------


@cli.command(help="Pull a profile's archive into local SQLite (incremental by default).")
@click.argument("profile", callback=_profile_arg_callback)
@click.option("--full", "mode", flag_value="full", help="Re-fetch everything, ignore checkpoints.")
@click.option("--incremental", "mode", flag_value="incremental",
              default=True, help="Only fetch changed objects (default).")
@click.option("--i-understand-tos-risk", "tos_ack", is_flag=True,
              help="Acknowledge that automating claude.ai is against TOS (one-time).")
@click.pass_context
def backup(ctx: click.Context, profile: str, mode: str, tos_ack: bool) -> None:
    """One-off archive of a profile. For a daily timer, use `schedule install`."""
    _ensure_tos(tos_ack)
    ensure_data_dir()

    async def run() -> None:
        async with open_session(profile) as session:
            click.echo(
                f"  ✓ {session.email} → {session.org_name} ({session.org_uuid[:8]}...)"
            )
            conn = open_db()
            try:
                counts = await dump_all(
                    session.client, conn, session.org_uuid,
                    org_name=session.org_name, incremental=(mode != "full"),
                )
            finally:
                conn.close()
            click.echo(
                f"  projects={counts['projects']} styles={counts['styles']} "
                f"conversations={counts['conversations']} "
                f"refreshed={counts['refreshed']} skipped={counts['skipped']}"
            )

    _run(run())


# ---------------------------------------------------------------------------
# Migrate (the happy path: backup source + restore to target + reorder)
# ---------------------------------------------------------------------------


@cli.command(help="Migrate SOURCE's archive to TARGET. Asks `Proceed? [y/N]` before mutating.")
@click.argument("source", callback=_profile_arg_callback)
@click.argument("target", callback=_profile_arg_callback)
@click.option("--dry-run", is_flag=True,
              help="Show the plan and exit without prompting or running.")
@click.option("--yes", "-y", "skip_prompt", is_flag=True,
              help="Skip the y/N confirmation (for scripts/automation).")
@click.option("--prefs/--no-prefs", default=True, show_default=True,
              help="Include profile preferences (name/role/traits).")
@click.option("--styles/--no-styles", default=True, show_default=True,
              help="Include custom styles.")
@click.option("--projects/--no-projects", default=True, show_default=True,
              help="Include projects (system prompts + knowledge files).")
@click.option("--conversations/--no-conversations", default=True, show_default=True,
              help="Include chat conversations.")
@click.option("--concurrency", type=click.IntRange(1, 5), default=1, show_default=True,
              help="Conversations to migrate in parallel (>1 trades Recents "
              "ordering for speed; reorder runs automatically afterwards).")
@click.option("--skip-backup", is_flag=True,
              help="Skip the source backup step (use existing local archive).")
@click.option("--skip-reorder", is_flag=True,
              help="Skip the post-migration reorder step.")
@click.option("--i-understand-tos-risk", "tos_ack", is_flag=True,
              help="Acknowledge TOS risk (one-time).")
def migrate(
    source: str,
    target: str,
    dry_run: bool,
    skip_prompt: bool,
    prefs: bool,
    styles: bool,
    projects: bool,
    conversations: bool,
    concurrency: int,
    skip_backup: bool,
    skip_reorder: bool,
    tos_ack: bool,
) -> None:
    """Refresh the source archive, show the plan, ask `Proceed? [y/N]`, then
    create/update each chat & project on target. Idempotent — re-running picks
    up only what's new since last time. `--dry-run` shows the plan without
    prompting; `--yes` skips the prompt for automation."""
    _ensure_tos(tos_ack)
    ensure_data_dir()

    # Step 1: backup source (skippable for "I just want to push existing archive").
    # --dry-run implies skip-backup: a "preview" that pulls every conversation
    # from claude.ai is a contradiction in terms.
    if not skip_backup and not dry_run:
        click.echo(f"Step 1/3: backup source ({source})")

        async def _backup() -> None:
            async with open_session(source) as session:
                click.echo(
                    f"  ✓ {session.email} → {session.org_name} "
                    f"({session.org_uuid[:8]}...)"
                )
                conn = open_db()
                try:
                    counts = await dump_all(
                        session.client, conn, session.org_uuid,
                        org_name=session.org_name, incremental=True,
                    )
                finally:
                    conn.close()
                click.echo(
                    f"  projects={counts['projects']} styles={counts['styles']} "
                    f"conversations={counts['conversations']} "
                    f"refreshed={counts['refreshed']} skipped={counts['skipped']}"
                )

        _run(_backup())
        click.echo("")
    elif dry_run and not skip_backup:
        click.echo(
            f"Step 1/3: backup source ({source}) — skipped (dry-run)"
        )
        click.echo("")

    # Step 2: show the plan (always)
    click.echo(f"Step 2/3: plan against target ({target})")
    plan = _run(dry_run_plan(target_profile=target))

    def _row(label: str, pending: int, total: int, enabled: bool) -> str:
        done = total - pending
        flag = "" if enabled else "  (skipped via flag)"
        return f"  {label:14} {pending} new + {done} already done = {total} total{flag}"

    click.echo(_row("prefs:", 0 if not prefs else 1, 1, prefs))
    click.echo(_row("styles:", plan["styles_pending"], plan["styles_total"], styles))
    click.echo(_row("projects:", plan["projects_pending"], plan["projects_total"], projects))
    click.echo(_row(
        "conversations:",
        plan["conversations_pending"], plan["conversations_total"], conversations,
    ))

    if dry_run:
        click.echo("\n(dry-run — no changes made)")
        return

    pending_total = (
        (plan["styles_pending"] if styles else 0)
        + (plan["projects_pending"] if projects else 0)
        + (plan["conversations_pending"] if conversations else 0)
        + (1 if prefs else 0)
    )
    if pending_total == 0:
        click.echo("\n  ✓ Nothing to migrate — target already matches archive.")
        return

    # Probe target identity before any destructive call so the user can catch a
    # "wrong cookies on the target profile" mistake before we mutate.
    async def _confirm_target() -> tuple[str | None, str | None]:
        async with open_session(target) as session:
            return session.email, session.org_name

    email, org_name = _run(_confirm_target())
    target_label = f"{email or '?'}{f' ({org_name})' if org_name else ''}"

    click.echo("")
    click.echo(f"Step 3/3: about to migrate to {target_label}.")

    if not skip_prompt and not click.confirm(
        f"Proceed with {pending_total} pending item(s)?", default=False
    ):
        click.echo("Aborted.")
        return

    summary = _run(run_restore(
        target_profile=target,
        dry_run=False,
        do_prefs=prefs,
        do_styles=styles,
        do_projects=projects,
        do_conversations=conversations,
        concurrency=concurrency,
    ))

    click.echo("\nDone.")
    if prefs:
        flag = "✓" if summary.profile_prefs else "—"
        click.echo(f"  prefs:                  {flag}")
    if styles:
        click.echo(f"  styles migrated:        {summary.styles_migrated}/{summary.styles_total}")
    if projects:
        click.echo(f"  projects migrated:      {summary.projects_migrated}/{summary.projects_total}")
    if conversations:
        click.echo(f"  conversations migrated: {summary.conversations_migrated}/{summary.conversations_total}")
    click.echo(f"  skipped (already done): {summary.skipped}")
    failed = summary.failed
    if failed:
        click.echo(f"\n  failures: {len(failed)} — first few:")
        for src_uuid, err in failed[:5]:
            click.echo(f"    {src_uuid[:8]} → {err}")
        click.echo(f"  Re-run `claude-migrate migrate {source} {target}` to retry.")

    # Step 4 (optional): reorder
    if conversations and not skip_reorder and not failed:
        click.echo("\nReordering target Recents to match source updated_at order...")
        _run(_reorder_run(target))

    click.echo(
        f"\n→ Memory: run `claude-migrate memory` to import memory.\n"
        f"→ Verify: run `claude-migrate verify {target}` to re-probe each chat."
    )


async def _reorder_run(profile: str) -> None:
    async with open_session(profile) as session:
        conn = open_db()
        try:
            state = RestoreState(conn, profile)
            touched, missing, errors = await reorder_conversations(
                session.client, conn, session.org_uuid, state, dry_run=False,
            )
        finally:
            conn.close()
        click.echo(f"  touched {touched} chat(s)")
        if missing:
            click.echo(f"  {missing} source chats had no migration_log entry (skipped)")
        if errors:
            click.echo(f"  errors: {len(errors)}")


# ---------------------------------------------------------------------------
# Verify, reorder, cleanup, preview
# ---------------------------------------------------------------------------


@cli.command(help="Probe each migrated chat on TARGET to confirm it's still there.")
@click.argument("target", callback=_profile_arg_callback)
@click.option("--reconcile", is_flag=True,
              help="Drop migration_log rows for chats no longer on target so the "
              "next migrate re-creates them.")
def verify(target: str, reconcile: bool) -> None:
    result = _run(verify_target_conversations(
        target_profile=target, reconcile=reconcile,
    ))
    click.echo(f"  target: {result['email']}")
    click.echo(f"  ✓ {result['confirmed']} confirmed on target")
    missing = result["missing"]
    unknown = result.get("unknown", [])
    if missing:
        click.echo(f"  ✗ {len(missing)} missing on target:")
        for src_uuid, tgt_uuid in missing[:10]:
            click.echo(f"    source {src_uuid[:8]} → target {tgt_uuid[:8]}")
        if len(missing) > 10:
            click.echo(f"    ... and {len(missing) - 10} more")
        if result["reconciled"]:
            click.echo(
                f"  → dropped {len(missing)} migration_log row(s); "
                f"run `claude-migrate migrate <source> {target}` to recreate."
            )
        else:
            click.echo(
                f"\n  → Run `claude-migrate verify {target} --reconcile` to drop "
                "these entries; the next migrate will recreate them."
            )
    if unknown:
        click.echo(f"  ? {len(unknown)} unknown (probe failed — not classified):")
        for src_uuid, tgt_uuid, err in unknown[:5]:
            click.echo(
                f"    source {src_uuid[:8]} → target {tgt_uuid[:8]}: {err[:80]}"
            )
        if len(unknown) > 5:
            click.echo(f"    ... and {len(unknown) - 5} more")
        click.echo(
            "  (unknown rows are NOT reconciled — re-run verify after the "
            "transient issue is gone.)"
        )
    if not missing and not unknown:
        click.echo("\n  All migrated chats are still on target.")


@cli.command(help="Re-PUT each migrated chat on TARGET in source updated_at order.")
@click.argument("target", callback=_profile_arg_callback)
@click.option("--dry-run", is_flag=True,
              help="Show how many chats would be touched and exit without prompting.")
@click.option("--yes", "-y", "skip_prompt", is_flag=True,
              help="Skip the y/N confirmation (for scripts/automation).")
def reorder(target: str, dry_run: bool, skip_prompt: bool) -> None:
    """Each PUT bumps the chat's `updated_at`, so iterating in source-ASC order
    leaves Recents matching the source. No model calls — safe to re-run.

    Asks `Proceed? [y/N]` before running. `--dry-run` previews without prompting;
    `--yes` skips the prompt for automation."""

    async def preview() -> tuple[str, str, int, int]:
        async with open_session(target) as session:
            email = session.email or "?"
            org = session.org_uuid
            conn = open_db()
            try:
                state = RestoreState(conn, target)
                touched, missing, _errors = await reorder_conversations(
                    session.client, conn, session.org_uuid, state,
                    dry_run=True,
                )
            finally:
                conn.close()
        return email, org, touched, missing

    email, org, touched, missing = _run(preview())
    click.echo(f"  target: {email} ({org[:8]}...)")
    click.echo(f"  would touch {touched} chat(s)")
    if missing:
        click.echo(f"  {missing} source chats had no migration_log entry (skipped)")

    if dry_run:
        click.echo("\n(dry-run — no changes made)")
        return
    if touched == 0:
        click.echo("\n  ✓ Nothing to reorder.")
        return
    if not skip_prompt and not click.confirm(
        f"Reorder {touched} chat(s) on {email}?", default=False
    ):
        click.echo("Aborted.")
        return

    async def run() -> None:
        async with open_session(target) as session:
            conn = open_db()
            try:
                state = RestoreState(conn, target)
                done, _miss, errors = await reorder_conversations(
                    session.client, conn, session.org_uuid, state, dry_run=False,
                )
            finally:
                conn.close()
            click.echo(f"  touched {done} chat(s)")
            if errors:
                click.echo(f"  errors: {len(errors)}")
                for src_uuid, err in errors[:5]:
                    click.echo(f"    {src_uuid[:8]} → {err}")

    _run(run())


def _parse_window_arg(s: str) -> datetime:
    """Parse a --since/--until argument for `cleanup`.

    Accepts: ``2026-04-30``, ``2026-04-30T14:37``, ``2026-04-30T14:37:00``, with
    optional ``Z`` or ``+HH:MM`` / ``-HH:MM`` offset. Bare-naive input is
    interpreted as UTC; tz-aware input is converted to UTC so window
    comparisons against API ISO timestamps are consistent without silently
    rewriting the user's intended offset.
    """
    s = s.strip()
    if "T" not in s:
        s = s + "T00:00:00"
    date_part, time_and_tz = s.split("T", 1)
    # Find where the tz suffix begins (Z / + / - that follows HH:MM).
    tz_pos = -1
    for sep in ("Z", "+"):
        i = time_and_tz.find(sep)
        if i > 0:
            tz_pos = i
            break
    if tz_pos == -1:
        i = time_and_tz.rfind("-")
        if i >= 4:  # past at least HH:MM (so we don't mistake the year sep)
            tz_pos = i
    time_part = time_and_tz if tz_pos == -1 else time_and_tz[:tz_pos]
    tz_part = "" if tz_pos == -1 else time_and_tz[tz_pos:]
    if time_part.count(":") == 1:
        time_part += ":00"
    dt = datetime.fromisoformat(f"{date_part}T{time_part}{tz_part}")
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


@cli.command(help="Delete empty conversations on TARGET created during a failed run.")
@click.argument("target", callback=_profile_arg_callback)
@click.option(
    "--since", "since_iso", required=True,
    help="When the failed run started, e.g. 2026-04-30T14:37 (printed on the "
    "`Step 2/3: plan against target...` line of the failed run output).",
)
@click.option(
    "--until", "until_iso", default=None,
    help="Upper bound. Defaults to --since + 1 hour.",
)
@click.option("--dry-run", is_flag=True,
              help="Scan and list orphans, exit without prompting or deleting.")
@click.option("--yes", "-y", "skip_prompt", is_flag=True,
              help="Skip the y/N confirmation (for scripts/automation).")
def cleanup(
    target: str, since_iso: str, until_iso: str | None,
    dry_run: bool, skip_prompt: bool,
) -> None:
    """Each candidate is fetched and verified to have ZERO messages before
    deletion — so a real chat with content can never be touched.

    Asks `Proceed? [y/N]` before deleting. `--dry-run` lists orphans without
    prompting; `--yes` skips the prompt for automation."""
    try:
        since_dt = _parse_window_arg(since_iso)
        until_dt = (
            _parse_window_arg(until_iso) if until_iso else since_dt + timedelta(hours=1)
        )
    except ValueError as e:
        click.echo(
            f"Could not parse time bound: {e}\n"
            "Accepted shapes:\n"
            "  2026-04-30T14:37            (date + minute)\n"
            "  2026-04-30T14:37:00         (with seconds)\n"
            "  2026-04-30T14:37:00Z        (with optional Z suffix)\n"
            "  2026-04-30                  (whole-day midnight start)",
            err=True,
        )
        sys.exit(2)

    # We have to scan first to find candidates — that's read-only, safe before
    # any prompt. Deletion only happens after confirm.
    async def scan() -> tuple[str, str, list[dict[str, object]]]:
        async with open_session(target) as session:
            click.echo(f"  target: {session.email} ({session.org_uuid[:8]}...)")
            click.echo(f"  window: {since_dt.isoformat()} → {until_dt.isoformat()}")
            click.echo("  scanning conversations and verifying each is empty...")
            orphans = await find_orphan_conversations(
                session.client, session.org_uuid,
                created_after=since_dt, created_before=until_dt,
                require_empty_name=False,
            )
            return session.email or "?", session.org_uuid, orphans

    email, _org_uuid, orphans = _run(scan())
    click.echo(f"  confirmed orphans: {len(orphans)}")
    for c in orphans[:20]:
        cu = str(c.get("uuid") or "?")
        ca = c.get("created_at") or "?"
        click.echo(f"    {cu[:8]}  created {ca}")
    if len(orphans) > 20:
        click.echo(f"    ... and {len(orphans) - 20} more")

    if dry_run:
        click.echo("\n(dry-run — no chats deleted)")
        return
    if not orphans:
        click.echo("\n  ✓ Nothing to clean up.")
        return
    if not skip_prompt and not click.confirm(
        f"Delete {len(orphans)} orphan chat(s) from {email}?", default=False
    ):
        click.echo("Aborted.")
        return

    async def delete() -> None:
        # Pace deletes: a tight DELETE loop hits the same rate-limit
        # window /completion does. 0.5s between calls + a Pacer that
        # respects 429 cooldowns lets cleanup of large orphan sets finish
        # without abandoning halfway through.
        from .runner import Pacer, WorkerOutcome
        pacer = Pacer(base_sleep_sec=0.5, rate_limit_sleep_sec=60.0)
        async with open_session(target) as session:
            deleted = 0
            for c in orphans:
                cu = c.get("uuid")
                if not isinstance(cu, str):
                    continue
                await pacer.before()
                ok = await delete_conversation(session.client, session.org_uuid, cu)
                if ok:
                    deleted += 1
                    await pacer.after(WorkerOutcome.ok(cu))
                else:
                    # delete_conversation only fails for EndpointChanged or
                    # NetworkError — both already logged. Treat as ordinary
                    # outcome (no rate-limited flag without explicit signal).
                    await pacer.after(WorkerOutcome.failed("delete failed"))
            click.echo(f"  deleted {deleted}/{len(orphans)} orphans")

    _run(delete())


@cli.command(help="Print the transcript that would be sent for a stored conversation.")
@click.argument("conversation_uuid")
@click.option("--show-payload", is_flag=True,
              help="Print payload kind (inline / attachment / chunked) and token count.")
def preview(conversation_uuid: str, show_payload: bool) -> None:
    conn = open_db()
    try:
        if show_payload:
            payload = prepare_paste_payload(conn, conversation_uuid)
            kind = (
                "inline" if isinstance(payload, InlinePayload)
                else "attachment" if isinstance(payload, AttachmentPayload)
                else "chunked"
            )
            click.echo(f"# kind={kind} tokens={payload.token_estimate}")
            if isinstance(payload, ChunkedPayload):
                click.echo(f"# chunks={len(payload.chunks)}")
        click.echo(render_transcript(conn, conversation_uuid))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Status, doctor, headers-help, memory
# ---------------------------------------------------------------------------


@cli.command(help="Show local archive vs target migration counts (no network calls).")
@click.argument("target", callback=_profile_arg_callback)
def status(target: str) -> None:
    s = migration_status(target)
    archive = s["archive"]
    ok = s["target_ok"]
    last = s["last_activity"]
    failures = s["failures"]
    click.echo(f"Migration status for target={target}:")
    click.echo("")
    click.echo(f"  conversations: {ok['conversations']}/{archive['conversations']} migrated")
    click.echo(f"  projects:      {ok['projects']}/{archive['projects']} migrated")
    click.echo(f"  styles:        {ok['styles']}/{archive['styles']} migrated")
    click.echo("")
    if last is not None:
        click.echo(f"  last activity: {last['migrated_at']} ({last['status']})")
    else:
        click.echo("  last activity: (no migration_log rows yet)")
    if failures:
        click.echo("")
        click.echo(f"  recent failures: {len(failures)}")
        for f in failures[:5]:
            err_short = (f.get("error") or "")[:80]
            click.echo(f"    {f['source_uuid'][:8]}  {f['object_type']}  {err_short}")
        click.echo("")
        click.echo(
            f"  → Re-run `claude-migrate migrate <source> {target}` "
            "to retry failed objects."
        )
    else:
        all_done = (
            ok["conversations"] >= archive["conversations"]
            and ok["projects"] >= archive["projects"]
            and ok["styles"] >= archive["styles"]
        )
        click.echo("")
        if all_done and archive["conversations"] > 0:
            click.echo("  ✓ All caught up.")
        elif archive["conversations"] == 0:
            click.echo(
                "  → Run `claude-migrate backup <source>` to populate the local archive."
            )
        else:
            click.echo(
                f"  → Run `claude-migrate migrate <source> {target}` "
                "to migrate remaining items."
            )


@cli.command(help="Diagnostic: paths, scheduler backend, headers, profiles.")
def doctor() -> None:
    settings = load_settings()
    click.echo(f"claude-migrate v{__version__}")
    click.echo(f"  data dir:        {data_dir()}")
    click.echo(f"  config dir:      {config_dir()}")
    click.echo(f"  db path:         {db_path()}")
    click.echo(f"  scheduler:       {detect_backend()}")
    cv = settings.client_version
    suffix = (
        "  (default; only set this if /api/* returns 400/422 — "
        "see `claude-migrate headers-help`)"
        if cv == ANTHROPIC_CLIENT_VERSION_DEFAULT
        else ""
    )
    click.echo(f"  client_version:  {cv}{suffix}")
    sha_suffix = (
        "  (only set if /api/* returns 400/422 — see `headers-help`)"
        if not settings.client_sha
        else ""
    )
    click.echo(f"  client_sha:      {settings.client_sha or '(unset)'}{sha_suffix}")
    aid = settings.anonymous_id
    click.echo(f"  anonymous_id:    {aid or '(unset)'}{'  (optional)' if not aid else ''}")
    did = settings.device_id
    click.echo(f"  device_id:       {did or '(unset)'}{'  (optional)' if not did else ''}")
    profiles = list_profiles()
    click.echo(f"  profiles:        {', '.join(profiles) or '(none — run `add <name>`)'}")


@cli.command("headers-help",
             help="How to capture anthropic-* headers from your browser "
             "(only needed if /api/* returns 400/422).")
def headers_help() -> None:
    click.echo(CLIENT_VERSION_HELP)


# ---------------------------------------------------------------------------
# Config (open config.toml in $EDITOR; show resolved settings)
# ---------------------------------------------------------------------------


def _config_path() -> Path:
    return Path(config_dir()) / "config.toml"


@cli.group(help="Manage the config.toml file (anthropic-* headers, chat sleep, etc.).")
def config() -> None:
    pass


@config.command("path", help="Print the path to config.toml.")
def config_path_cmd() -> None:
    click.echo(str(_config_path()))


@config.command("show", help="Print the resolved config (env vars + config.toml).")
def config_show() -> None:
    settings = load_settings()
    click.echo(f"  client_version: {settings.client_version!r}")
    click.echo(f"  client_sha:     {settings.client_sha!r}")
    click.echo(f"  anonymous_id:   {settings.anonymous_id!r}")
    click.echo(f"  device_id:      {settings.device_id!r}")
    click.echo(f"  chat_sleep_sec: {settings.chat_sleep_sec}")
    click.echo(f"  base_url:       {settings.base_url}")
    click.echo("")
    p = _config_path()
    if p.exists():
        click.echo(f"  config file:    {p}")
    else:
        click.echo(f"  config file:    {p}  (not yet created — run `config edit` to create)")


@config.command("edit", help="Open config.toml in $EDITOR (creates it with a template if missing).")
def config_edit() -> None:
    """Creates `~/.config/claude-migrate/config.toml` with a commented template
    on first run, then opens it in `$EDITOR` (or `$VISUAL`, falling back to a
    sensible per-OS default). Use this after `headers-help` to set the
    anthropic-* headers when /api/* is returning 400/422."""
    p = _config_path()
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(CONFIG_TEMPLATE, "utf-8")
        click.echo(f"Created template: {p}")
    click.edit(filename=str(p))


@cli.command(help="Print extraction prompt + claude.com/import-memory instructions.")
@click.option("--no-copy", is_flag=True, help="Skip copying to clipboard.")
@click.option("--open", "open_browser", is_flag=True,
              help="Also open https://claude.com/import-memory in your browser.")
def memory(no_copy: bool, open_browser: bool) -> None:
    """Memory is the only manual step — run on the source, paste into target's
    Settings → Memory → Start import."""
    memory_prepare(copy=not no_copy)
    if open_browser:
        memory_verify_open()


# ---------------------------------------------------------------------------
# Schedule (daily backup timer)
# ---------------------------------------------------------------------------


@cli.group(help="Manage the daily backup timer (best-effort, optional).")
def schedule() -> None:
    pass


@schedule.command("install", help="Install the daily incremental backup timer.")
@click.option("--profile", default="source", show_default=True,
              callback=_profile_arg_callback,
              help="Profile to back up daily.")
def schedule_install(profile: str) -> None:
    s = install_timer(profile)
    click.echo(f"  backend:   {s.backend}")
    click.echo(f"  profile:   {profile}")
    click.echo(f"  installed: {s.installed}")
    if s.detail:
        click.echo(f"  detail:    {s.detail}")


@schedule.command("status", help="Show whether the timer is installed.")
def schedule_status_cmd() -> None:
    s = timer_status()
    click.echo(f"  backend:   {s.backend}")
    click.echo(f"  installed: {s.installed}")
    if s.detail:
        click.echo(f"  detail:    {s.detail}")


@schedule.command("uninstall", help="Remove the daily timer.")
def schedule_uninstall() -> None:
    s = uninstall_timer()
    click.echo(f"  backend: {s.backend}")
    click.echo(f"  detail:  {s.detail}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    cli(obj={})


if __name__ == "__main__":
    main()
