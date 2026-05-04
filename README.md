# claude-migrate

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![Tests](https://img.shields.io/badge/tests-318%20passing-brightgreen.svg)](#development)

A free, open-source command-line tool for moving data between **your own** claude.ai consumer accounts — or for backing up an account to local SQLite.

`claude-migrate` is a community project. It is not built, sponsored, or endorsed by Anthropic.

- **Conversations** are recreated with their full original transcripts.
- **Projects, custom styles, profile preferences** re-create natively.
- **Memory** imports through Anthropic's own paste flow at `claude.com/import-memory`.
- **No browser automation** — auth is a one-time cookie paste from DevTools.
- **No telemetry, no analytics**, no third-party services beyond claude.ai itself.

> **Anthropic's Consumer Terms restrict the kind of API access this tool performs** (§3.4 prohibits scraping, §3.7 prohibits automation). Use it only for migrating between accounts **you own**. Anthropic may rate-limit, suspend, or terminate the affected accounts. The CLI requires a one-time acknowledgement on first run.

---

## Quick start

```bash
# 1. Install once.
uv tool install git+https://github.com/jamcas14/claude-migrate.git

# 2. Paste cookies for each account (~30 seconds each — only the first time).
claude-migrate add source
claude-migrate add target

# 3. Migrate. Idempotent — safe to re-run if anything fails.
claude-migrate migrate source target
```

`uv` not installed? `pipx install …` works just as well — see [Install](#install).

---

## Contents

- [Install](#install)
- [How migration works](#how-migration-works)
- [Choosing a migration mode](#choosing-a-migration-mode)
- [Command reference](#command-reference)
- [Authentication walkthrough](#authentication-walkthrough)
- [Migration speed and rate limits](#migration-speed-and-rate-limits)
- [Daily backups](#daily-backups)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)
- [Architecture](#architecture)
- [Development](#development)
- [License](#license)

---

## Install

`claude-migrate` is a Python 3.12+ package. Either of these installers put the `claude-migrate` binary on your `$PATH`:

```bash
# uv (recommended — fast, modern)
# Get uv: curl -LsSf https://astral.sh/uv/install.sh | sh
uv tool install git+https://github.com/jamcas14/claude-migrate.git

# pipx (alternative)
pipx install git+https://github.com/jamcas14/claude-migrate.git
```

Verify:

```bash
claude-migrate --help
```

Upgrade later with `uv tool upgrade claude-migrate` (or `pipx upgrade claude-migrate`). Supported on macOS, Linux, and Windows.

### Optional shell alias

If `claude-migrate` is too long for daily use, alias it in your shell rc:

```bash
# bash / zsh
echo "alias cm='claude-migrate'" >> ~/.bashrc   # or ~/.zshrc

# fish
alias --save cm 'claude-migrate'
```

After re-sourcing, `cm migrate src tgt` works.

### Optional shell completion

Tab-completion for profile names and (in `claude-migrate load`) bookmarked chat titles. Set up once per shell:

```bash
# bash — add to ~/.bashrc
eval "$(_CLAUDE_MIGRATE_COMPLETE=bash_source claude-migrate)"

# zsh — add to ~/.zshrc
eval "$(_CLAUDE_MIGRATE_COMPLETE=zsh_source claude-migrate)"

# fish — write once, no eval needed
_CLAUDE_MIGRATE_COMPLETE=fish_source claude-migrate > ~/.config/fish/completions/claude-migrate.fish
```

After re-sourcing your rc, `claude-migrate whoami so<Tab>` expands to `source`, `claude-migrate load jamie cre<Tab>` expands to a matching bookmark title, etc.

### Development install

If you're hacking on the code rather than just using it:

```bash
git clone https://github.com/jamcas14/claude-migrate.git
cd claude-migrate
uv sync --all-extras       # creates .venv with dev deps
uv run pytest              # ~318 tests
uv run claude-migrate --help
```

---

## How migration works

`claude-migrate` makes authenticated requests to claude.ai's web API on your behalf, using cookies you paste once. It pulls everything from a **source** account into a local SQLite archive, then recreates the relevant objects on a **target** account. Re-runs are idempotent — already-migrated objects are skipped via a `migration_log` table keyed on `(source_uuid, target_profile)`.

### What's recreated and how

| Object | Mechanism | Fidelity |
|---|---|---|
| Conversations | Each becomes a new chat on target. The full original transcript (including thinking-block summaries, citations, file metadata) appears as the first user message; Claude replies `READY`. Title is `[YYYY-MM-DD] Original title`. | High — content is preserved verbatim; framing is slightly detached ("the transcript indicates we agreed…" vs "we agreed…"). |
| Projects | Re-created via the internal API with original `prompt_template` and knowledge files. | 100%. |
| Custom styles | POSTed to target. | 100%. |
| Profile preferences | Best-effort `PUT /api/account` (claude.ai accepts `full_name`, `settings`, `name`, `role`). | Best-effort. |
| Memory | The CLI prints the extraction prompt and copies it to your clipboard. You paste it on the source, then paste Claude's reply into `claude.com/import-memory` on target. | Manual; ~95%. |

### What can't be recovered

These limitations come from claude.ai's API surface, not from the tool:

- **Original `created_at` / `updated_at` timestamps.** Every migrated chat carries the migration run's timestamp on target. The `[YYYY-MM-DD]` title prefix mitigates this; `claude-migrate reorder` re-aligns Recents to the source's last-modified order.
- **Tool-call inputs / outputs and artifact bodies.** claude.ai strips `tool_use`/`tool_result` blocks from API responses in every rendering mode. The transcript marks _where_ a tool ran, but the inputs/outputs aren't exposed.
- **Synthetic assistant turns.** `/completion` always invokes the model and signs the assistant turn server-side; there's no documented way to inject pre-existing assistant messages. That's why migration uses transcript-paste-as-first-message rather than recreating alternating turns.

---

## Choosing a migration mode

There are three. **Pick once before you start.** All three are idempotent on re-run, but switching modes mid-migration produces a target account that's part one mode, part another.

> Note: the choice depends on **your target account's claude.ai plan** and how you want chats organised on the destination side. It has nothing to do with `claude-migrate` itself, which is free and open source.

| Want | Use | claude.ai plan it works well on |
|---|---|---|
| Each source chat as its own entry in target's Recents, willing to wait for the rate-limit window | **default** (no flag) | claude.ai Max 5x/20x finishes in minutes. claude.ai Pro is workable but slow (~22h for 200 chats). claude.ai Free is technically possible but very slow. |
| One searchable archive — every chat as a `.md` file inside a single Project on target. No per-chat Recents entries. | **`--archive-only`** | Any claude.ai plan. The Project endpoint isn't on the same rate-limit bucket as `/completion`, so this finishes in minutes regardless of plan. |
| Per-chat Recents entries, but only burn `/completion` budget on chats you actually use later | **`--bookmark`** + `claude-migrate load` | Any claude.ai plan. Migration itself is free of `/completion` calls; per-chat cost is paid one-at-a-time when you `load` a specific stub. |

```bash
# Default — full transcript pasted into each chat.
claude-migrate migrate source target

# Archive-only — one Project on target, all chats as .md docs inside it.
claude-migrate migrate source target --archive-only

# Bookmark — empty stubs in Recents; materialise specific chats later.
claude-migrate migrate source target --bookmark
claude-migrate load target "react hooks"     # by title fragment
claude-migrate load target                   # interactive picker
claude-migrate load target --all             # load every bookmark
```

### How `--bookmark` looks on target

Each source chat becomes an empty conversation in Recents named `[ul|YYYY-MM-DD] Original title`. The `[ul|...]` prefix is the in-UI signal that the chat is an unloaded stub — **don't type in it before running `load`**, otherwise your message lands as the first turn before the transcript paste.

`claude-migrate load TARGET PATTERN` accepts four kinds of `PATTERN`:

```bash
# 1. Title substring (case-insensitive).
claude-migrate load jamie "react hooks"

# 2. Full URL paste (just paste it from your browser bar).
claude-migrate load jamie https://claude.ai/chat/abc12345-1234-5678-9abc-def012345678

# 3. Bare full UUID, or hex prefix (≥6 chars).
claude-migrate load jamie abc12345-1234-5678-9abc-def012345678
claude-migrate load jamie abc12345

# 4. No pattern → interactive numbered picker (supports ranges like "1-5", lists like "1,3,7", or "all").
claude-migrate load jamie
```

`--all` loads every bookmarked chat in one go, paced against claude.ai's `/completion` rate-limit bucket the same way `migrate` is. Idempotent — already-loaded chats are skipped.

### How `--archive-only` looks on target

After the run, the target account has one new Project named `[archive] {source-email} {YYYY-MM-DD}`. Inside, every source conversation is a `.md` file with the full transcript. Open the project and ask "what did I discuss about X?" — Claude searches the docs.

For many users this is functionally a backup-and-search tool. If you don't actually need each chat as its own Recents entry (most people don't, especially for chats older than a few weeks), `--archive-only` is the right choice.

---

## Command reference

Profiles are arbitrary strings (`source`, `target`, `work`, `personal-old`, …). Cookies live in your OS keychain (Keychain on macOS, Credential Manager on Windows, Secret Service on Linux, encrypted-file fallback otherwise).

### Account lifecycle

| Command | What it does |
|---|---|
| `claude-migrate add NAME` | Stores cookies for a new profile, or refreshes them for an existing one (idempotent). |
| `claude-migrate remove NAME` | Deletes a profile from your keychain. Local-only — does not invalidate the cookie on Anthropic's side. |
| `claude-migrate rename OLD NEW` | Renames a stored profile (typo fix or naming change). No re-paste, no network. |
| `claude-migrate accounts` | Lists stored profiles + last-known identity. No network. |
| `claude-migrate whoami NAME` | Live-probes a profile's credentials against `/api/bootstrap`. |

### Migration

| Command | What it does |
|---|---|
| `claude-migrate backup PROFILE [--full]` | One-shot incremental archive of a profile to local SQLite. |
| `claude-migrate migrate SOURCE TARGET` | **Default mode** — pastes each chat as its own conversation on target. |
| `claude-migrate migrate SOURCE TARGET --archive-only` | **Archive mode** — bundle every chat as a `.md` doc in one Project on target. Zero `/completion` calls. |
| `claude-migrate migrate SOURCE TARGET --bookmark` | **Bookmark mode** — empty `[ul|...]` stubs in Recents; materialise on demand with `load`. Zero `/completion` calls. |
| `claude-migrate migrate SOURCE TARGET --dry-run` | Plan only — show what would happen, exit without prompting. |
| `claude-migrate migrate SOURCE TARGET --yes` | Skip the y/N prompt (for scripts and automation). |
| `claude-migrate load TARGET [PATTERN]` | Materialise one or more stubs created by `--bookmark`. With `PATTERN`: substring match against title, hex UUID prefix, full UUID, or full URL. With no pattern: interactive picker. With `--all`: every bookmark. |
| `claude-migrate verify TARGET [--reconcile]` | Probe each migrated chat on target to confirm it still exists. `--reconcile` drops `migration_log` rows for deleted chats. |
| `claude-migrate reorder TARGET` | Re-PUT each migrated chat in source `updated_at` order so target's Recents matches. No model calls. |
| `claude-migrate cleanup TARGET --since ISO` | Delete empty (zero-message) chats on target left over from a failed run. Each candidate is verified to have zero messages **and** to NOT be in `migration_log` before deletion. |
| `claude-migrate preview UUID` | Print the rendered transcript for one source conversation. |

### Status, diagnostics, configuration

| Command | What it does |
|---|---|
| `claude-migrate status TARGET` | Local archive vs target migration counts, including loaded/bookmarked breakdown. No network. |
| `claude-migrate doctor` | Paths, scheduler backend, captured `anthropic-*` headers, stored profiles. |
| `claude-migrate headers-help` | One-screen guide to capturing `anthropic-client-version` / `anthropic-client-sha` from your browser (only needed if `/api/*` returns HTTP 400 or 422). |
| `claude-migrate memory [--open]` | Prints the memory-extraction prompt (also copies to clipboard). `--open` also opens `claude.com/import-memory` in your browser. |
| `claude-migrate config show` | Print the resolved config (env vars + `config.toml`). |
| `claude-migrate config edit` | Open `config.toml` in `$EDITOR` (creates it from a template if missing). |
| `claude-migrate config path` | Print the path to `config.toml`. |
| `claude-migrate schedule install` | Register a daily incremental backup with the OS scheduler (systemd / launchd / Task Scheduler / cron). |
| `claude-migrate schedule status` / `uninstall` | Inspect / remove the daily timer. |

---

## Authentication walkthrough

`claude-migrate add` walks you through pasting two cookies from DevTools. Anthropic marks `sessionKey` as `HttpOnly`, so the JS console can't see it — you need DevTools' Application/Storage tab. About 30 seconds per profile.

### The two cookies

| Cookie | Where to find it | What it looks like |
|---|---|---|
| `sessionKey` | DevTools → Application/Storage → Cookies → `https://claude.ai` | `sk-ant-sid01-…` (or `sid02-`, etc.) — ~120 chars |
| `cf_clearance` | Same table | Longer alphanumeric — ~50+ chars |

> The Value column in DevTools usually truncates the displayed string. **Click the row** and look at the details panel below the cookie table for the full value, then copy from there. This is the single most common cause of "looks too short" errors.

### Per-browser steps

| Browser | Where the cookies live |
|---|---|
| Chrome / Edge / Brave / Arc / Opera / Vivaldi | F12 → **Application** tab → Storage → Cookies → `https://claude.ai` |
| Firefox | F12 → **Storage** tab → Cookies → `https://claude.ai` |
| Safari | Enable: Safari → Settings → Advanced → "Show Develop menu". Then Develop → Show Web Inspector → Storage → Cookies → `claude.ai` |

### What `add` looks like

```
$ claude-migrate add source

Authenticating profile source. You'll paste two cookies from your browser
(~30 seconds, once per account).

  1. Open https://claude.ai signed in to the source account.
  2. Press F12, then go to:
       Chromium browsers:  Application tab → Cookies → claude.ai
       Firefox:            Storage tab → Cookies → claude.ai
       Safari:             Develop → Show Web Inspector → Storage → Cookies → claude.ai
  3. Copy the full Value for `sessionKey` and `cf_clearance`.

sessionKey (starts with sk-ant-sid01- or sk-ant-sid02-, ~120 chars):
> sk-ant-sid01-…
  ✓ sessionKey format OK

cf_clearance:
> Zk0c.W3.…
  ✓ cf_clearance format OK

Confirming credentials with claude.ai...
  ✓ Authenticated as foo@example.com (Foo's Workspace)
    Stored as profile 'source' in the OS keychain.
```

### Common copy-paste mistakes (silently fixed)

The CLI strips these before validation, so you don't have to think about them:

- Surrounding quotes (`"sk-ant-…"`)
- `sessionKey:` or `sessionKey=` prefixes (from copying header lines)
- Trailing semicolons (from `Cookie:` header copies)
- URL-encoded characters (`%2B` → `+`)
- `Bearer ` prefixes
- Leading/trailing whitespace and newlines

---

## Migration speed and rate limits

For all but the smallest accounts, default-mode `claude-migrate migrate` is **rate-limited by claude.ai's per-account `/completion` bucket**, not by anything in the tool. This section explains the wall and how to work around it.

### claude.ai's `/completion` rate limit

claude.ai caps `/completion` calls (the only endpoint that can write a message) on a 5-hour rolling token bucket per account. Approximate ceilings, by **target account's claude.ai plan**:

| claude.ai plan | ≈ messages per 5-hour window |
|---|---:|
| Free | ~15–40 |
| Pro ($20/mo) | ~45 |
| Max 5x ($100/mo) | ~225 |
| Max 20x ($200/mo) | ~900 |

So **a 200-chat default-mode migration to a Pro target takes ≥22 hours of wall-clock**, split across ≥5 windows, no matter how clever the client. The community-converged numbers above can change without notice (Anthropic doesn't publish exact figures).

Two further wrinkles:

- **Peak hours (Mon–Fri 13:00–19:00 UTC) drain the bucket ~2× faster.** Running on a Saturday morning UTC is a free ~2× speedup. The migrate command warns if you start during peak.
- The bucket is **per-account, not per-IP**. VPN won't help.

### Speedup options, ranked by impact

1. **Switch to `--archive-only` or `--bookmark`.** Both skip `/completion` entirely at migration time, finishing in minutes. See [Choosing a migration mode](#choosing-a-migration-mode).
2. **Upgrade your target's claude.ai plan to Max 5x for one month** ($100). 200 chats default-mode finishes in one 5-hour window. Highest leverage if you specifically want the default mode's full-fidelity per-chat continuation.
3. **`--fast` flag.** Shortcut for `--concurrency=3`. On accounts with slack in the bucket, runs three conversations in parallel. Auto-reorder runs at the end so Recents matches source `updated_at` ordering.
4. **Run on a weekend morning UTC.** Off-peak ~2× speedup at zero cost.
5. **Tune `chat_sleep_sec`.** Default 30s upper bound; the Pacer adapts within `[5s, chat_sleep_sec]` based on observed 429s. Lower if your account doesn't 429; raise if it does. Override via `CLAUDE_MIGRATE_CHAT_SLEEP_SEC=15`.

### What the tool already does for you

- **Adaptive pacing (AIMD)** — starts at 5s/chat, doubles on 429, divides by 1.5 after 3 consecutive successes. Adapts to your account's actual rate, not a fixed conservative default.
- **Honors server-side `Retry-After`** — when claude.ai sends a header telling us how long to wait, we wait that long instead of a hardcoded cooldown. A 10s floor is enforced regardless (claude.ai occasionally sends `Retry-After: 0`).
- **Cascade-abort** — if 5 chats in a row hit a rate limit with no successes between, the run stops cleanly instead of grinding through every remaining chat creating orphan empty stubs on target.
- **Empirical instrumentation** — each 429 is logged with the response headers (`Retry-After`, `anthropic-ratelimit-*`) so you can verify what the server is sending.

---

## Daily backups

`claude-migrate schedule install` registers a per-OS native scheduler unit that runs `claude-migrate backup source --quiet` once a day. The action:

- Hits one paginated `/api/.../chat_conversations` request to find changed conversations.
- Re-fetches only those (incremental — usually 0–5 detail requests).
- Writes raw gzipped JSON sidecars to `data/raw/{date}/`.
- Updates SQLite + the `checkpoint` table.
- Logs to `data/logs/backup.log`.

Per-OS specifics:

| OS | Backend | Unit file |
|---|---|---|
| Linux (with systemd) | systemd `--user` | `~/.config/systemd/user/claude-migrate.{service,timer}` |
| Linux (no systemd) | cron | line in `crontab -l` tagged `# claude-migrate (managed)` |
| macOS | launchd | `~/Library/LaunchAgents/com.user.claudemigrate.plist` |
| Windows | Task Scheduler | task name `claude-migrate` |

When `sessionKey` eventually expires, the timer fires, hits 401, exits 75, and writes the failure to `backup.log`. Re-run `claude-migrate add source` to refresh.

---

## Configuration

| Field | Where | Default | When you'd set it |
|---|---|---|---|
| `client_version` | `config.toml` or `CLAUDE_MIGRATE_CLIENT_VERSION` | bundled | Only if `/api/*` returns HTTP 400/422 — claude.ai rotated headers. |
| `client_sha` | `config.toml` or `CLAUDE_MIGRATE_CLIENT_SHA` | unset | Same; rotates more often than `client_version`. |
| `anonymous_id`, `device_id` | `config.toml` or env | unset | Optional fingerprint headers. |
| `chat_sleep_sec` | `config.toml` or `CLAUDE_MIGRATE_CHAT_SLEEP_SEC` | 30 | Per-chat sleep ceiling for default-mode migration. |

`claude-migrate config edit` opens `config.toml` in `$EDITOR`, creating a commented template if missing. `claude-migrate doctor` shows the resolved values.

**Data location.** The local SQLite archive + raw transcripts live in your platform's user data directory:

- Linux: `$XDG_DATA_HOME/claude-migrate` (default `~/.local/share/claude-migrate`)
- macOS: `~/Library/Application Support/claude-migrate`
- Windows: `%LOCALAPPDATA%\claude-migrate`

Override via `CLAUDE_MIGRATE_DATA_DIR=/some/path`. Run `claude-migrate doctor` to see the resolved path.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `add` says "looks too short" | DevTools' Value column truncates the display | Click the row, copy from the details panel below the table. |
| `add` says "Cloudflare is challenging the request" | Stale `cf_clearance` | Refresh `claude.ai` in your browser, then `claude-migrate add <profile>` and re-paste. |
| `add` says "403 without a Cloudflare challenge" | Stale session cookie (usually) or out-of-date TLS fingerprint (rare) | First try re-pasting cookies. If multiple profiles fail the same way, run `pip install -U curl_cffi`. |
| Migration is hitting 429 on every chat | claude.ai's per-account `/completion` bucket is drained | Migration aborts after 5 consecutive 429s. Either wait several hours and re-run (idempotent), or switch to `--archive-only` / `--bookmark` (no `/completion` at migration time). See [Migration speed and rate limits](#migration-speed-and-rate-limits). |
| Migration is slow (each chat takes a minute) | Pacer is in conservative mode after past 429s | Lower the ceiling: `CLAUDE_MIGRATE_CHAT_SLEEP_SEC=15`. Conversely, raise it on accounts that 429 often: `=60`. |
| `migrate` fails with HTTP 400 or 422 | `anthropic-client-sha` rotated | `claude-migrate headers-help` for the capture walkthrough; `claude-migrate config edit` to set the new values. |
| Migration was interrupted; what's left? | — | `claude-migrate status <target>` reads `migration_log` (no network) and prints done/total per object type plus recent failures. |
| Failed migration left empty conversations on target | Worker died mid-flight, or cascade-abort tripped | `claude-migrate cleanup <target> --since 2026-04-30T14:37`. Each candidate is verified to have zero messages AND not be in `migration_log` before deletion. |
| Recents on target are in wrong order | `--concurrency > 1` scrambled the order | `claude-migrate reorder <target>` re-aligns. |
| Daily timer fires but does nothing | `sessionKey` expired | Check `data/logs/backup.log`. If 401, run `claude-migrate add <source>`. |

---

## Architecture

| Concern | Choice |
|---|---|
| HTTP | `curl_cffi` impersonating Chrome 131 (defeats Cloudflare TLS fingerprinting on `claude.ai`; plain `requests`/`httpx` get 403'd). |
| Concurrency | Hard cap of 5 (`asyncio.Semaphore(5)`). 429 backoff: capped exponential `2 → 4 → 8 → 16 → 32 → 60`. |
| Storage | SQLite + FTS5 with gzipped raw-JSON sidecars at `data/raw/{date}/`. |
| Idempotency | `migration_log(source_uuid, target_profile)` — re-running any command is safe. |
| Secrets | OS-native keychain via `keyring`, with AES-256-GCM file fallback. |
| Auth | Cookie paste only — no Playwright, no Selenium, no browser automation. |
| Scheduling | OS-native (systemd / launchd / Task Scheduler / cron). |

```
claude_migrate/
├── auth.py        # cookie paste flow, normalisation, format validation, keyring storage
├── client.py      # one HTTP layer, retry/backoff, 429 cooldown, typed errors
├── config.py      # paths + pydantic-settings (env vars, config.toml)
├── session.py     # `open_session(profile)` — load profile + create client + bind org_uuid
├── discover.py    # /api/bootstrap → org_uuid (no side effects)
├── fetch.py       # async fan-out: orgs → projects → docs → conversations
├── render.py      # JSON → XML transcript builder (uses rendering_mode=messages)
├── transport.py   # /completion SSE + multipart upload + send_payload dispatch
├── store.py       # SQLite schema + UPSERTs + raw-sidecar writer
├── state.py       # RestoreState — owns migration_log per (conn, target_profile)
├── runner.py      # WorkerOutcome + Pacer (AIMD rate-limit barrier) + migrate_row helper
├── restore.py     # Per-object-type restore loops + cascade-abort on top of runner/state
├── archive.py     # `--archive-only` worker: bundle conversations as docs in one Project
├── bookmark.py    # `--bookmark` worker + `claude-migrate load` materialiser
├── migrate.py     # Top-level orchestrator (run_restore, dry_run_plan, verify_target_conversations)
├── memory.py      # Extraction prompt + clipboard helper
├── notify.py      # ntfy / osascript / Windows toast on failures
├── scheduler.py   # OS-native daily timer install/uninstall
├── checkpoint.py  # last_seen_updated_at + content-hash dedup
├── errors.py      # Typed exception hierarchy (AuthExpired, RateLimited, …)
└── cli.py         # Click commands (verb-first, positional args)
```

---

## Development

```bash
git clone https://github.com/jamcas14/claude-migrate.git
cd claude-migrate
uv sync --all-extras
uv run pytest              # ~318 tests
uv run mypy claude_migrate # strict mode
uv run ruff check
```

The test suite covers auth normalisation (every common paste mistake), the HTTP layer's status-code → typed-error mapping, `Retry-After` parsing, transcript rendering (thinking summaries, tool-call placeholders, citations, files, project context), the restore-state log projection, the rate-limit pacer (server-hint cooldown floor, AIMD growth/decay, cascade-abort detection, parallel safety), the `--archive-only` and `--bookmark`/`load` workers, and the per-payload wire format on the way to `/completion`.

Contributions welcome — open an issue or PR at https://github.com/jamcas14/claude-migrate.

---

## License

[MIT](LICENSE). Free, open source, no warranty. The tool is a community project not affiliated with Anthropic.
