"""User-facing CLI.

Verb-first commands with positional arguments for the primary noun. Dry-run is
the default for any operation that mutates a remote account; pass `--execute`
to actually write. Profile names are arbitrary strings (`source`, `target`,
`work`, `personal-old`, etc.) — there's no special role beyond the name.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import UTC, datetime

import click
import structlog

from . import __version__
from .auth import (
    list_profiles,
    load_profile,
    remove_profile,
    run_auth_flow,
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

TOS_BANNER = (
    "Heads up: Anthropic's Consumer Terms (§3.4 prohibits scraping, §3.7\n"
    "prohibits automation) restrict the kind of API access this tool performs.\n"
    "By using claude-migrate you accept the risk that Anthropic may rate-limit,\n"
    "suspend, or terminate the affected accounts. The tool is intended for\n"
    "migrating between YOUR OWN accounts.\n\n"
    "Re-run with --i-understand-tos-risk to proceed.\n"
)

CLIENT_VERSION_HELP = """\
claude.ai's web app sends a few `anthropic-*` headers on every /api/* request
that act as a build/identity fingerprint. When stale or missing, Cloudflare or
Anthropic's backend can reject requests with HTTP 400/422. The header that
matters most in practice is `anthropic-client-sha` — it rotates every few
weeks as Anthropic ships new builds.

Most users never need to set these. Capture and configure them only if you hit
400/422 errors. Step-by-step:

  1. Open https://claude.ai (signed in) and press F12 to open DevTools.
  2. Click the Network tab.
  3. Refresh the page (or click around the UI) so requests appear in the list.
  4. Click any request whose path starts with /api/ — for example
     /api/bootstrap or /api/organizations/<uuid>/chat_conversations.
  5. In the right-side Headers panel, scroll to "Request Headers" and find:
       anthropic-client-version    (e.g. 1.0.0)
       anthropic-client-sha         (40-char hex like efac08e6...) — most important
       anthropic-anonymous-id       (claudeai.v1.<uuid>) — optional
       anthropic-device-id          (<uuid>)              — optional
     Copy each value.

Then set them one of two ways:

  # session-only
  export CLAUDE_MIGRATE_CLIENT_VERSION=1.0.0
  export CLAUDE_MIGRATE_CLIENT_SHA=<paste>
  export CLAUDE_MIGRATE_ANONYMOUS_ID=<paste>     # optional
  export CLAUDE_MIGRATE_DEVICE_ID=<paste>        # optional

  # persistent — append to ~/.config/claude-migrate/config.toml:
  mkdir -p ~/.config/claude-migrate
  cat >> ~/.config/claude-migrate/config.toml <<EOF
  client_version = "1.0.0"
  client_sha = "<paste>"
  anonymous_id = "<paste>"   # optional
  device_id = "<paste>"      # optional
  EOF

Verify with `claude-migrate doctor`. Re-run the failed command afterward —
work resumes from the last checkpoint.
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


def _run(coro: object) -> object:
    """Run an async coroutine with friendly mapping of typed errors → exit codes."""
    try:
        return asyncio.run(coro)  # type: ignore[arg-type]
    except KeyboardInterrupt:
        click.echo("\nInterrupted.", err=True)
        sys.exit(130)
    except AuthExpired as e:
        click.echo(f"\nSession expired: {e}", err=True)
        click.echo(
            "Run `claude-migrate login <profile>` to re-authenticate, then "
            "re-run the same command to resume.",
            err=True,
        )
        notify("claude-migrate", "Session expired — re-auth required.")
        sys.exit(EXIT_TEMPFAIL)
    except CloudflareChallenge as e:
        click.echo(f"\nCloudflare challenge: {e}", err=True)
        click.echo(
            "Refresh https://claude.ai once in your browser (this gets you a "
            "fresh cf_clearance), then `claude-migrate login <profile>`.",
            err=True,
        )
        sys.exit(EXIT_TEMPFAIL)
    except ClientVersionStale as e:
        settings = load_settings()
        click.echo(f"\n{e}", err=True)
        click.echo(
            f"\nCurrent value: client_version={settings.client_version!r}\n",
            err=True,
        )
        click.echo(CLIENT_VERSION_HELP, err=True)
        sys.exit(EXIT_TEMPFAIL)
    except (AuthInvalid, AuthMissing) as e:
        click.echo(f"\n{e}", err=True)
        sys.exit(2)
    except AuthError as e:
        click.echo(f"\nAuth error: {e}", err=True)
        sys.exit(2)
    except NetworkError as e:
        click.echo(f"\nNetwork error: {e}", err=True)
        sys.exit(EXIT_TEMPFAIL)
    except ClaudeMigrateError as e:
        click.echo(f"\nError: {e}", err=True)
        sys.exit(1)


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
        "Run `claude-migrate login source` to authenticate a profile, then\n"
        "`claude-migrate migrate source target --execute` to clone source to target."
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


@cli.command(help="Authenticate a profile (interactive cookie paste).")
@click.argument("name")
def login(name: str) -> None:
    """First-time login OR re-auth after the session expired. Idempotent — running
    against an existing profile name overwrites the stored cookies."""
    _run(run_auth_flow(name, refreshing=_profile_exists(name)))


@cli.command(help="Remove a stored profile from the OS keychain.")
@click.argument("name")
@click.confirmation_option(prompt="Delete this profile from the keychain?")
def logout(name: str) -> None:
    remove_profile(name)
    click.echo(f"Removed profile {name!r}.")


@cli.command(help="List stored profiles (no network calls).")
def accounts() -> None:
    names = list_profiles()
    if not names:
        click.echo("No profiles stored.")
        click.echo("  → `claude-migrate login <name>` to authenticate one.")
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
    click.echo("  → `claude-migrate login <name>`         re-paste cookies (refresh expired session)")
    click.echo("  → `claude-migrate rename OLD NEW`       rename a profile")
    click.echo("  → `claude-migrate logout <name>`        remove a profile from the keychain")
    click.echo("  → `claude-migrate whoami <name>`        live-probe a profile's credentials")


@cli.command(help="Rename a stored profile (no network call, no re-paste needed).")
@click.argument("old_name")
@click.argument("new_name")
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
            f"`claude-migrate logout {new_name}` first if you want to overwrite it.",
            err=True,
        )
        sys.exit(2)
    from .auth import store_profile  # local to keep top-level imports tight
    store_profile(new_name, profile)
    remove_profile(old_name)
    click.echo(f"Renamed {old_name!r} → {new_name!r}.")


@cli.command(help="Probe a profile to confirm its credentials still work.")
@click.argument("name")
def whoami(name: str) -> None:
    """Hits /api/bootstrap with the stored cookies, prints the authenticated
    identity, and updates the profile's `last_probe_ok` timestamp on success."""
    result = _run(verify_profile(name))
    if result is None:
        click.echo(f"\nVerification of {name!r} did not return a result.", err=True)
        click.echo(f"Run `claude-migrate login {name}` to re-authenticate.", err=True)
        sys.exit(1)
    p = load_profile(name)
    click.echo(f"  ✓ {p.email or 'unknown email'}")
    if getattr(result, "org_name", None):
        click.echo(f"    organization:  {result.org_name}")  # type: ignore[attr-defined]
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
@click.argument("profile")
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


@cli.command(help="Migrate SOURCE's archive to TARGET. Dry-run by default.")
@click.argument("source")
@click.argument("target")
@click.option("--execute", is_flag=True,
              help="Actually perform the migration. Default is dry-run preview.")
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
    execute: bool,
    prefs: bool,
    styles: bool,
    projects: bool,
    conversations: bool,
    concurrency: int,
    skip_backup: bool,
    skip_reorder: bool,
    tos_ack: bool,
) -> None:
    """Refresh the source archive, then create / update each chat & project on
    target. Idempotent — re-running picks up only what's new since last time."""
    _ensure_tos(tos_ack)
    ensure_data_dir()

    # Step 1: backup source (skippable for "I just want to push existing archive")
    if not skip_backup:
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

    # Step 2: dry-run plan (always shown)
    click.echo(f"Step 2/3: plan against target ({target})")
    plan = _run(dry_run_plan(target_profile=target))
    if not isinstance(plan, dict):
        click.echo(
            "Could not compute migration plan — run `claude-migrate backup "
            f"{source}` first to populate the local archive.",
            err=True,
        )
        sys.exit(1)

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

    if not execute:
        click.echo("\n(dry-run — pass --execute to migrate)")
        return

    # Probe target identity before any destructive call so the user can catch a
    # "wrong cookies on the target profile" mistake before we mutate.
    async def _confirm_target() -> tuple[str | None, str | None]:
        async with open_session(target) as session:
            return session.email, session.org_name

    confirmation = _run(_confirm_target())
    if isinstance(confirmation, tuple):
        email, org_name = confirmation
        click.echo(f"\nStep 3/3: migrating to target ({target})")
        click.echo(f"  ✓ {email or '?'}{f' ({org_name})' if org_name else ''}")
    else:
        click.echo(f"\nStep 3/3: migrating to target ({target})")

    summary = _run(run_restore(
        target_profile=target,
        dry_run=False,
        do_prefs=prefs,
        do_styles=styles,
        do_projects=projects,
        do_conversations=conversations,
        concurrency=concurrency,
    ))
    if summary is None:
        click.echo("Restore did not return a summary (it may have aborted early).", err=True)
        sys.exit(1)

    click.echo("\nDone.")
    if prefs:
        flag = "✓" if getattr(summary, "profile_prefs", False) else "—"
        click.echo(f"  prefs:                  {flag}")
    if styles:
        click.echo(f"  styles migrated:        {summary.styles_migrated}/{summary.styles_total}")  # type: ignore[attr-defined]
    if projects:
        click.echo(f"  projects migrated:      {summary.projects_migrated}/{summary.projects_total}")  # type: ignore[attr-defined]
    if conversations:
        click.echo(f"  conversations migrated: {summary.conversations_migrated}/{summary.conversations_total}")  # type: ignore[attr-defined]
    click.echo(f"  skipped (already done): {summary.skipped}")  # type: ignore[attr-defined]
    failed = getattr(summary, "failed", []) or []
    if failed:
        click.echo(f"\n  failures: {len(failed)} — first few:")
        for src_uuid, err in failed[:5]:
            click.echo(f"    {src_uuid[:8]} → {err}")
        click.echo(f"  Re-run `claude-migrate migrate {source} {target} --execute` to retry.")

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
@click.argument("target")
@click.option("--reconcile", is_flag=True,
              help="Drop migration_log rows for chats no longer on target so the "
              "next migrate re-creates them.")
def verify(target: str, reconcile: bool) -> None:
    result = _run(verify_target_conversations(
        target_profile=target, reconcile=reconcile,
    ))
    if not isinstance(result, dict):
        click.echo("Verification could not run (auth or network issue).", err=True)
        sys.exit(1)
    click.echo(f"  target: {result['email']}")
    click.echo(f"  ✓ {result['confirmed']} confirmed on target")
    missing = result["missing"]
    if missing:
        click.echo(f"  ✗ {len(missing)} missing on target:")
        for src_uuid, tgt_uuid in missing[:10]:
            click.echo(f"    source {src_uuid[:8]} → target {tgt_uuid[:8]}")
        if len(missing) > 10:
            click.echo(f"    ... and {len(missing) - 10} more")
        if result["reconciled"]:
            click.echo(
                f"  → dropped {len(missing)} migration_log row(s); "
                f"run `claude-migrate migrate <source> {target} --execute` to recreate."
            )
        else:
            click.echo(
                f"\n  → Run `claude-migrate verify {target} --reconcile` to drop "
                "these entries; the next migrate will recreate them."
            )
    else:
        click.echo("\n  All migrated chats are still on target.")


@cli.command(help="Re-PUT each migrated chat on TARGET in source updated_at order.")
@click.argument("target")
@click.option("--execute", is_flag=True,
              help="Actually reorder. Default is dry-run preview.")
def reorder(target: str, execute: bool) -> None:
    """Each PUT bumps the chat's `updated_at`, so iterating in source-ASC order
    leaves Recents matching the source. No model calls — safe to re-run."""

    async def run() -> None:
        async with open_session(target) as session:
            click.echo(f"  target: {session.email} ({session.org_uuid[:8]}...)")
            conn = open_db()
            try:
                state = RestoreState(conn, target)
                touched, missing, errors = await reorder_conversations(
                    session.client, conn, session.org_uuid, state,
                    dry_run=not execute,
                )
            finally:
                conn.close()
            verb = "would touch" if not execute else "touched"
            click.echo(f"  {verb} {touched} chat(s)")
            if missing:
                click.echo(f"  {missing} source chats had no migration_log entry (skipped)")
            if errors:
                click.echo(f"  errors: {len(errors)}")
                for src_uuid, err in errors[:5]:
                    click.echo(f"    {src_uuid[:8]} → {err}")
            if not execute:
                click.echo("\n(dry-run — pass --execute to actually reorder)")

    _run(run())


@cli.command(help="Delete empty conversations on TARGET created during a failed run.")
@click.argument("target")
@click.option(
    "--since", "since_iso", required=True,
    help="When the failed run started, e.g. 2026-04-30T14:37 (printed on the "
    "`Step 2/3: plan against target...` line of the failed run output).",
)
@click.option(
    "--until", "until_iso", default=None,
    help="Upper bound. Defaults to --since + 1 hour.",
)
@click.option("--execute", is_flag=True,
              help="Actually delete. Default is dry-run preview.")
def cleanup(target: str, since_iso: str, until_iso: str | None, execute: bool) -> None:
    """Each candidate is fetched and verified to have ZERO messages before
    deletion — so a real chat with content can never be touched."""
    from datetime import UTC, datetime, timedelta

    def _parse(s: str) -> datetime:
        s = s.strip().rstrip("Z")
        if "T" not in s:
            s = s + "T00:00:00"
        if s.count(":") == 1:
            s = s + ":00"
        return datetime.fromisoformat(s).replace(tzinfo=UTC)

    try:
        since_dt = _parse(since_iso)
        until_dt = _parse(until_iso) if until_iso else since_dt + timedelta(hours=1)
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

    async def run() -> None:
        async with open_session(target) as session:
            click.echo(f"  target: {session.email} ({session.org_uuid[:8]}...)")
            click.echo(f"  window: {since_dt.isoformat()} → {until_dt.isoformat()}")
            click.echo("  scanning conversations and verifying each is empty...")
            orphans = await find_orphan_conversations(
                session.client, session.org_uuid,
                created_after=since_dt, created_before=until_dt,
                require_empty_name=False,
            )
            click.echo(f"  confirmed orphans: {len(orphans)}")
            for c in orphans[:20]:
                click.echo(
                    f"    {c.get('uuid', '?')[:8]}  created {c.get('created_at', '?')}"
                )
            if len(orphans) > 20:
                click.echo(f"    ... and {len(orphans) - 20} more")
            if not execute:
                click.echo("\n(dry-run — pass --execute to delete)")
                return
            deleted = 0
            for c in orphans:
                cu = c.get("uuid")
                if not isinstance(cu, str):
                    continue
                if await delete_conversation(session.client, session.org_uuid, cu):
                    deleted += 1
            click.echo(f"  deleted {deleted}/{len(orphans)} orphans")

    _run(run())


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
@click.argument("target")
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
            f"  → Re-run `claude-migrate migrate <source> {target} --execute` "
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
                f"  → Run `claude-migrate migrate <source> {target} --execute` "
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
    click.echo(f"  profiles:        {', '.join(profiles) or '(none — run `login <name>`)'}")


@cli.command("headers-help",
             help="Show how to capture the anthropic-* request headers from your "
             "browser (only needed if /api/* returns 400/422).")
def headers_help() -> None:
    click.echo(CLIENT_VERSION_HELP, err=True)


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
def schedule_install() -> None:
    s = install_timer()
    click.echo(f"  backend:   {s.backend}")
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
