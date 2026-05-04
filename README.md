# claude-migrate

Migrate one Claude.ai consumer account into another, or back up an account incrementally to local SQLite. Conversations land in **target's Recents** with the original transcripts intact, projects and custom styles re-create natively, and memory imports through Anthropic's official paste flow.

```bash
claude-migrate add source                    # paste cookies once (~30s)
claude-migrate add target                    # second account
claude-migrate migrate source target         # clone source onto target (asks y/N)
```

> **Heads-up:** Anthropic's Consumer Terms forbid scraping (§3.4) and automation (§3.7). This tool exists for migrating between **your own** accounts. You accept the risk that the affected accounts may be rate-limited or suspended. The CLI prompts you to acknowledge this on first run.

## Which migration mode should I use?

There are three. **Pick once before you start** — re-running is idempotent and safe, but switching modes mid-migration produces a target account that's part one mode, part another.

| Your situation | Use this | Why |
|---|---|---|
| **Paid plan**, fine waiting hours, want full-fidelity per-chat continuation | **default** (no flag) | One `/completion` per chat, transcript pasted as the first user message. Slow on Pro (~22h for 200 chats), fast on Max. |
| Any plan, just want a **searchable archive** — don't care about per-chat Recents entries | **`--archive-only`** | Zero `/completion` calls. All transcripts become `.md` files inside one Project on target. 200 chats finishes in minutes. |
| **Free plan** (or paid + want to control which chats spend tokens), want **per-chat Recents entries**, willing to keep a terminal handy | **`--bookmark`** + `claude-migrate load` | Migration creates empty named stubs in Recents (zero `/completion`). You materialise individual chats on demand from the terminal — one `/completion` per chat *you actually want*. |

```bash
# default — full transcript pasted into each chat
claude-migrate migrate source target

# archive-only — one project, all chats as .md docs
claude-migrate migrate source target --archive-only

# bookmark — empty stubs in Recents, load on demand
claude-migrate migrate source target --bookmark
claude-migrate load target "react hooks"      # load by title fragment
claude-migrate load target                    # interactive picker
claude-migrate load target --all              # load everything (paces against the bucket)
```

The three modes are mutually exclusive — you can't combine flags. They're also independent: a `--bookmark` migration produces no Project clutter, `--archive-only` produces no per-chat Recents entries, default mode produces both per-chat Recents AND no Projects.

---

## Install

Pick whichever Python tool installer you have. Both put `claude-migrate` on your `$PATH` so you can run it from any directory.

```bash
# Recommended: uv (fast, modern; install with: curl -LsSf https://astral.sh/uv/install.sh | sh)
uv tool install git+https://github.com/jamcas14/claude-migrate.git

# Or: pipx
pipx install git+https://github.com/jamcas14/claude-migrate.git
```

Verify:

```bash
claude-migrate --help
```

To upgrade later: `uv tool upgrade claude-migrate` (or `pipx upgrade claude-migrate`).

Requires Python 3.12+. macOS, Linux, and Windows are supported.

### Optional: short alias

If `claude-migrate` is too long for daily use, alias it in your shell rc:

```bash
# bash / zsh
echo "alias cm='claude-migrate'" >> ~/.bashrc  # or ~/.zshrc
# fish
alias --save cm 'claude-migrate'
```

After re-sourcing, `cm migrate src tgt` works.

### Tab-completion of profile names

Add this once to your shell rc to get `<Tab>` completion on stored profile names everywhere `claude-migrate` takes a profile argument:

```bash
# bash — add to ~/.bashrc
eval "$(_CLAUDE_MIGRATE_COMPLETE=bash_source claude-migrate)"

# zsh — add to ~/.zshrc
eval "$(_CLAUDE_MIGRATE_COMPLETE=zsh_source claude-migrate)"

# fish — write once, no eval needed
_CLAUDE_MIGRATE_COMPLETE=fish_source claude-migrate > ~/.config/fish/completions/claude-migrate.fish
```

After re-sourcing your rc, `claude-migrate whoami so<Tab>` expands to `source` (etc).

### Development install (only if you're hacking on the code)

```bash
git clone https://github.com/jamcas14/claude-migrate.git
cd claude-migrate
uv sync --all-extras    # creates .venv with all deps including dev
uv run pytest           # run the test suite
uv run claude-migrate --help
```

The `uv run` prefix is only needed for the development checkout. End users get the bare `claude-migrate` command via the install commands above.

---

## What gets migrated

| Item | Mechanism | Fidelity |
|---|---|---|
| **Conversations** | Each becomes a new chat on target. The full original transcript (including thinking summaries, citations, file metadata) appears as the first user message; Claude replies `READY`. Title is `[YYYY-MM-DD] Original title`. | High — content is preserved verbatim, framing is slightly detached ("the transcript indicates we agreed…" vs "we agreed…"). |
| **Projects** | Re-created via internal API with the original `prompt_template` and knowledge files. | 100%. |
| **Custom styles** | POSTed to target. | 100%. |
| **Profile preferences** | Best-effort `PUT /api/account` — Anthropic's API accepts `full_name`, `settings`, `name`, `role`. | Best-effort. |
| **Memory** | The CLI prints a memory-extraction prompt; you paste it on the source and the response into `https://claude.com/import-memory`. | Manual, 95%. |

### What can't be recovered

These are properties of claude.ai's API, not the tool:

- **Original `created_at` / `updated_at` timestamps** — every migrated chat carries the migration run's timestamp. The `[YYYY-MM-DD]` title prefix mitigates; `claude-migrate reorder` re-aligns Recents to source's last-modified order.
- **Web search queries, tool-call inputs, and artifact bodies** — claude.ai strips tool_use/tool_result blocks from the API response in every `rendering_mode`. The transcript marks _where_ a tool ran but the inputs/outputs are not exposed by the API.
- **Mobile clients** — claude.ai mobile lacks export entirely. Tool is desktop-only.
- **Synthetic assistant turns** — the `/completion` endpoint always invokes the model and signs the assistant turn server-side. There's no way to inject a pre-existing assistant message and have it persist as a native turn. That's why the migration uses transcript-paste-as-first-message rather than recreating alternating turns.

---

## Commands

Profiles are arbitrary strings (`source`, `target`, `work`, `personal-old`, …). Cookies live in your OS keychain (Keychain on macOS, Credential Manager on Windows, Secret Service on Linux, encrypted-file fallback otherwise).

### Account lifecycle

| Command | What it does |
|---|---|
| `claude-migrate add NAME` | Walks you through pasting `sessionKey` and `cf_clearance` from DevTools. Idempotent — running against an existing name overwrites the stored cookies (use this to refresh after expiry). |
| `claude-migrate remove NAME` | Deletes the profile from the keychain. Local-only — does not invalidate the cookie on Anthropic's side. |
| `claude-migrate rename OLD NEW` | Renames a stored profile (typo fix or naming change). Pure metadata — no re-paste, no network call. |
| `claude-migrate accounts` | Lists stored profiles + their last-known identity. No network. Prints management hints for `add`/`rename`/`remove`/`whoami`. |
| `claude-migrate whoami NAME` | Probes the profile against `/api/bootstrap`, prints the live identity, updates the stored `last_probe_ok` timestamp. |

### Migration

| Command | What it does |
|---|---|
| `claude-migrate backup PROFILE [--full]` | One-shot incremental archive of a profile. Use this if you only want a backup without migrating. |
| `claude-migrate migrate SOURCE TARGET` | **Default mode**: backs up source, shows the plan, asks `Proceed? [y/N]`, then pastes each chat as its own conversation on target. Idempotent — already-migrated chats are skipped via the `migration_log` table. |
| `claude-migrate migrate SOURCE TARGET --archive-only` | **Archive mode**: bundle every conversation as a `.md` doc inside one Project on target. Zero `/completion` calls; finishes in minutes. No per-chat Recents entries. |
| `claude-migrate migrate SOURCE TARGET --bookmark` | **Bookmark mode**: create empty `[unloaded]`-prefixed stubs in target's Recents — no transcripts pasted, no `/completion` calls. Materialise specific chats on demand with `claude-migrate load`. |
| `claude-migrate migrate SOURCE TARGET --dry-run` | Plan only — show what would happen, exit without prompting or running. |
| `claude-migrate migrate SOURCE TARGET --yes` | Skip the y/N prompt (for scripts/automation). Same effect as answering `y`. |
| `claude-migrate load TARGET [PATTERN]` | Materialise one or more chats created by `--bookmark` mode. With a `PATTERN` it does a case-insensitive substring match against the source title; `>=6 hex chars` is treated as a UUID prefix lookup against the target chat (paste from your URL bar). With no args it shows an interactive numbered picker. With `--all` it loads every bookmarked chat, paced the same way as `migrate`. Idempotent — already-loaded chats are skipped. |
| `claude-migrate verify TARGET [--reconcile]` | Probe each migrated chat on target to confirm it's still there. `--reconcile` drops `migration_log` rows for chats that have been deleted on the server. |
| `claude-migrate reorder TARGET` | No-op PUT each migrated chat on target in source's `updated_at` order, so target's Recents matches the source's. No model calls. Confirms before running; pass `--dry-run` for preview or `--yes` to skip the prompt. |
| `claude-migrate cleanup TARGET --since ISO` | Delete empty (zero-message) chats on target created during a failed run. Each candidate is verified to have zero messages before deletion — real chats are never touched. Confirms before running; `--dry-run` / `--yes` available. |
| `claude-migrate preview UUID` | Print the transcript that would be sent for one source conversation. Use `--show-payload` to see the kind (inline/attachment/chunked) and token count. |

### Status & diagnostics

| Command | What it does |
|---|---|
| `claude-migrate status TARGET` | Local archive vs target migration counts, recent failures, recovery hints. No network. |
| `claude-migrate doctor` | Paths, scheduler backend, captured `anthropic-*` headers, stored profiles. |
| `claude-migrate headers-help` | One-screen guide to capturing `anthropic-client-version` / `anthropic-client-sha` from your browser (only needed if `/api/*` returns 400/422). |

### Configuration

| Command | What it does |
|---|---|
| `claude-migrate config show` | Print the resolved config (env vars + `config.toml`). |
| `claude-migrate config path` | Print the path to `config.toml`. |
| `claude-migrate config edit` | Open `config.toml` in `$EDITOR` (creates a commented template if missing). |

The same fields can be set via environment variables — `CLAUDE_MIGRATE_CLIENT_VERSION`, `CLAUDE_MIGRATE_CLIENT_SHA`, `CLAUDE_MIGRATE_ANONYMOUS_ID`, `CLAUDE_MIGRATE_DEVICE_ID`, `CLAUDE_MIGRATE_CHAT_SLEEP_SEC`. Env vars override `config.toml`.

**Data location.** The local SQLite archive + raw transcripts live in your platform's user data directory:
- Linux: `$XDG_DATA_HOME/claude-migrate` (default `~/.local/share/claude-migrate`)
- macOS: `~/Library/Application Support/claude-migrate`
- Windows: `%LOCALAPPDATA%\claude-migrate`

Override via `CLAUDE_MIGRATE_DATA_DIR=/some/path`. Run `claude-migrate doctor` to see the resolved path.

### Memory

| Command | What it does |
|---|---|
| `claude-migrate memory` | Prints the extraction prompt (also copied to clipboard), import instructions, and a one-liner about the manual paste step. Pass `--open` to open `https://claude.com/import-memory` in the browser. |

### Daily auto-backup

| Command | What it does |
|---|---|
| `claude-migrate schedule install` | Registers a daily incremental backup with the OS scheduler (systemd / launchd / Task Scheduler / cron). |
| `claude-migrate schedule status` | Shows whether the timer is installed. |
| `claude-migrate schedule uninstall` | Removes the timer. |

---

## End-to-end recipe

Migrate everything from `source` to a fresh `target`, then keep `source` backed up daily:

```bash
# 1. Store cookies for both accounts (OS keychain).
claude-migrate add source
claude-migrate add target

# 2. (Optional) Preview what will happen — exits without prompting.
claude-migrate migrate source target --dry-run

# 3. Migrate. Shows the plan, asks "Proceed? [y/N]", then runs. Idempotent.
claude-migrate migrate source target

# 4. (Optional) Re-probe each migrated chat to confirm it's still on target.
claude-migrate verify target

# 5. Manual memory import.
claude-migrate memory --open
# → paste the prompt into source's chat, copy Claude's reply,
#   then paste it into target's claude.com/import-memory.

# 6. (Optional) Daily incremental backup of source.
claude-migrate schedule install
```

---

## Tuning the migration speed

For all but the smallest accounts, `claude-migrate migrate source target` is **rate-limited by Anthropic's account-side usage bucket**, not by anything in this tool. This section explains what that means and how to work with it.

### The hard wall — how Anthropic rate-limits /completion

Anthropic caps `/completion` calls (the only endpoint that can write a message) on a 5-hour rolling token bucket per account:

| Plan | ≈ messages per 5-hour window |
|---|---:|
| Free | ~15–40 |
| Pro ($20/mo) | ~45 |
| Max 5x ($100/mo) | ~225 |
| Max 20x ($200/mo) | ~900 |

So **a 200-chat migration on Pro takes ≥22 hours of wall-clock**, split across ≥5 windows, no matter how clever the client. The community-converged numbers above can change without notice (Anthropic doesn't publish exact figures).

Two further wrinkles:

- **Peak hours (Mon–Fri 13:00–19:00 UTC) drain the bucket ~2× faster.** Running on a Saturday morning UTC is a free ~2× speedup. The migrate command will warn you if you start during peak.
- The bucket is per-account, not per-IP. VPN won't help.

### Speedup options, ranked by impact

1. **Upgrade to Max 5x for one month ($100).** A 200-chat run finishes in one 5-hour window. Highest leverage for full-fidelity migration.
2. **`--archive-only`.** Skips `/completion` entirely. Bundles every conversation as a markdown doc inside one Project on target. **200 chats finishes in minutes.** Trade-off: chats live in one Project (searchable as project knowledge, ask it questions), not as individual entries in Recents.
   ```bash
   claude-migrate migrate source target --archive-only
   ```
3. **`--bookmark` + `claude-migrate load`.** Migration creates empty named stubs in Recents — no `/completion` calls during migrate, no project clutter, finishes in minutes. Each stub is titled `[ul|YYYY-MM-DD] Original title` (the `ul` token signals "unloaded — don't type yet"). To resume one, run `claude-migrate load target "<title fragment>"` — it pastes the transcript via `/completion` and renames the chat to the default-mode `[YYYY-MM-DD] Title` shape. Per-chat token cost is identical to default mode, but **you only pay it on the chats you actually want**. Ideal when most of your archive is "just in case I want to look at it" rather than "I'll definitely use this."
   ```bash
   claude-migrate migrate source target --bookmark
   # later, when you want to resume one of them:
   claude-migrate load target "react hooks"
   # or pick from a list:
   claude-migrate load target
   # or load everything (paces against the bucket the same way `migrate` does):
   claude-migrate load target --all
   ```
3. **`--fast` flag.** Shortcut for `--concurrency=3`. On accounts with slack in the bucket, runs three conversations in parallel. Auto-reorder runs at the end so Recents matches source `updated_at` ordering.
   ```bash
   claude-migrate migrate source target --fast
   ```
4. **Run on a weekend morning UTC.** Off-peak ~2× speedup.
5. **Tune `chat_sleep_sec`.** Default 30s. The Pacer adapts within `[5s, chat_sleep_sec]` based on observed 429s. Lower if your account doesn't 429; raise if it always does. Override via `CLAUDE_MIGRATE_CHAT_SLEEP_SEC=60`.

### What the tool already does for you

- **Adaptive pacing (AIMD)**: starts at 5s/chat, doubles on 429, divides by 1.5 after 3 consecutive successes. Adapts to your account's actual rate, not a fixed conservative default.
- **Honors server-side `Retry-After`**: when claude.ai sends a header telling us how long to wait, we wait that long instead of a hardcoded 300s cooldown. A hard 10s floor is enforced regardless — Anthropic occasionally sends `Retry-After: 0`, which would otherwise become a tight retry loop.
- **Drops wasted client-side retries on 429**: the conversation-restore loop's outer cooldown is the right place to retry; inner retries inside the SSE handshake just burn ~14s of extra sleep.
- **Cascade-abort**: if 5 chats in a row hit a rate limit with no successes in between, the migration stops cleanly instead of grinding through every remaining chat creating orphan empty stubs on target. The summary explains the recovery options (`--archive-only`, wait, or `cleanup`).
- **Empirical instrumentation**: each 429 is logged with the response headers (`Retry-After`, `anthropic-ratelimit-*`) so you can verify what the server is sending.

### What `--archive-only` looks like on target

After running it, the target account has one new project named `[archive] {source-email} {YYYY-MM-DD}`. Inside, every source conversation is a `.md` file with the full transcript. Open the project and ask "what did I discuss about X?" — Claude searches the docs.

This is **functionally equivalent to a backup-and-search use case** for many users. If you don't actually need each chat as its own entry in Recents (most don't, especially for chats older than a few weeks), `--archive-only` is the right choice.

### What `--bookmark` looks like on target

After running it, the target account has one empty chat in Recents per source chat, each titled `[ul|YYYY-MM-DD] Original title`. No projects, no transcripts pasted, zero `/completion` calls. The `[ul|...]` prefix is the in-UI signal that the chat is an unloaded stub — **don't type in it before running `load`** (your message would land before the transcript paste, producing an awkward chat history).

To resume a chat, run any of these:

```bash
# By title fragment (case-insensitive substring):
claude-migrate load target "react hooks"

# By full URL — paste straight from your browser bar:
claude-migrate load target https://claude.ai/chat/abc12345-...-...

# By bare full UUID:
claude-migrate load target abc12345-1234-5678-90ab-cdef12345678

# By UUID prefix (≥6 hex chars):
claude-migrate load target abc12345

# Interactive picker over every bookmarked chat:
claude-migrate load target

# Load every bookmark in one go (paces against the bucket the same way migrate does):
claude-migrate load target --all
```

Tab completion on bookmark titles works the same way profile-name completion does — wire your shell once (see the **Tab-completion of profile names** section above) and `claude-migrate load jamie cre<Tab>` expands to "Creating a memeable...", etc.

`load` pastes the transcript via `/completion` (one call per chat — same per-chat cost as default mode), strips the `[unloaded]` prefix, and flips the `migration_log` row from `bookmarked` to `ok`. Re-running is idempotent.

If you accidentally typed in an `[unloaded]` chat before loading it, `load` refuses by default — pass `--force` to override. If you deleted a bookmark from claude.ai's UI, `load` surfaces a clean error pointing at `verify --reconcile`.

### Mode safety

The three modes share `migration_log` for idempotency, so they cooperate sensibly:

- **Re-running the same mode** is always safe — already-done items are skipped.
- **Switching modes** is allowed but doesn't retroactively re-organise. If you ran `--bookmark` and now want default mode for one chat, run `claude-migrate load <target> "<title>"` — that's the right tool. Default-mode `migrate` will skip every already-bookmarked chat and only process new ones.
- **`cleanup --since`** refuses to delete any chat in `migration_log` regardless of mode — bookmarked stubs are protected from a stray `--since` window the same way loaded chats are.

---

## Auth walkthrough

`add` walks you through pasting two cookies from DevTools. Anthropic marks `sessionKey` as `HttpOnly`, so the JS console can't see it — you need DevTools' Application/Storage tab. About 30 seconds per profile.

### The two cookies

| Name | Where | Looks like |
|---|---|---|
| `sessionKey` | DevTools → Application/Storage → Cookies → `https://claude.ai` | `sk-ant-sid01-…` (or `sid02-`, etc.) — ~120 chars |
| `cf_clearance` | same table | longer alphanumeric — ~50+ chars |

> The Value column in DevTools usually truncates the displayed string. **Click the row** and look at the details panel below the table for the full value, then copy from there. This is the single most common cause of "looks too short" errors.

### Per-browser steps

| Browser | DevTools cookie viewer |
|---|---|
| Chrome / Edge / Brave / Arc / Opera / Vivaldi | F12 → **Application** tab → Storage → Cookies → `https://claude.ai` |
| Firefox | F12 → **Storage** tab → Cookies → `https://claude.ai` |
| Safari | Enable: Safari → Settings → Advanced → "Show Develop menu". Then Develop → Show Web Inspector → Storage → Cookies → `claude.ai` |

### What the CLI shows

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

  → `claude-migrate whoami source`   live-probe this profile later
  → `claude-migrate add source`      re-paste cookies after expiry
```

### Common copy-paste mistakes (silently fixed)

The CLI strips these before format validation, so you don't have to think about them:

- Surrounding quotes (`"sk-ant-…"`)
- `sessionKey:` or `sessionKey=` prefixes (from copying header lines)
- Trailing semicolon (from a `Cookie:` header copy)
- URL-encoded characters (`%2B` → `+`)
- `Bearer ` prefix
- Leading/trailing whitespace and newlines

### Failure modes (each has a specific recovery message)

| HTTP | Message | Fix |
|---|---|---|
| `401` | "Your sessionKey was not accepted (HTTP 401)…" | Re-copy `sessionKey` (most common cause: truncation) |
| `403` + Cloudflare HTML | "Cloudflare is challenging the request…" | Refresh `claude.ai` once in your browser to get a fresh `cf_clearance`, re-paste cf_clearance only |
| `403` no body | "403 without a Cloudflare challenge…" | Cookies are fresh (you just pasted them), so this is most likely an outdated TLS fingerprint: `pip install -U curl_cffi` and retry. If that doesn't help, your IP may be flagged — try from a different network. |
| network | "Network error while probing claude.ai…" | Check VPN/proxy/connection |

---

## Daily auto-backup

`schedule install` registers a per-OS native scheduler unit that runs `claude-migrate backup source --quiet` once a day. The action:

- Hits one paginated `/api/.../chat_conversations` request to find changed conversations
- Re-fetches only those (incremental — usually 0–5 detail requests)
- Writes raw gzipped JSON sidecars to `data/raw/{date}/`
- Updates SQLite + the `checkpoint` table
- Logs to `data/logs/backup.log`

Per-OS specifics:

| OS | Backend | Unit file |
|---|---|---|
| Linux (with systemd) | systemd `--user` | `~/.config/systemd/user/claude-migrate.{service,timer}` |
| Linux (no systemd) | cron | line in `crontab -l` tagged `# claude-migrate (managed)` |
| macOS | launchd | `~/Library/LaunchAgents/com.user.claudemigrate.plist` |
| Windows | Task Scheduler | task name `claude-migrate` |

When `sessionKey` eventually expires, the timer fires, hits 401, exits 75, and writes the failure to `backup.log`. Re-run `claude-migrate add source` to refresh.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `add` says "looks too short" | DevTools Value column truncates the display | Click the row, copy from the details panel below the table |
| `add` says "Cloudflare is challenging the request" | Stale `cf_clearance` | Refresh `claude.ai` in your browser, then `claude-migrate add <profile>` and re-paste |
| `add` says "403 without a Cloudflare challenge" | Stale session cookie (usually) or out-of-date TLS fingerprint (rare) | First try re-pasting cookies: `claude-migrate add <profile>`. If multiple profiles fail the same way: `pip install -U curl_cffi`. |
| Migration is hitting 429 on every chat | Per-account `/completion` rate limit (drained for the current 5-hour window) | Migration aborts after 5 consecutive cascading 429s. Either wait several hours and re-run (idempotent), or switch to `--archive-only` (skips `/completion` entirely). See **Tuning the migration speed** above. |
| Migration is slow (each chat takes a minute) | Pacer is in conservative mode after past 429s | The Pacer adapts within `[5s, chat_sleep_sec]` using AIMD. Lower the ceiling: `export CLAUDE_MIGRATE_CHAT_SLEEP_SEC=15`. Conversely, raise it on accounts that 429 often: `=60`. |
| `migrate` fails with HTTP 400/422 | `anthropic-client-sha` rotated | `claude-migrate headers-help` for the capture walkthrough; `claude-migrate config edit` to put the new values in `config.toml` |
| Migration was interrupted; what's left? | — | `claude-migrate status <target>` reads `migration_log` (no network) and prints done/total per object type plus recent failures |
| Failed migration left empty conversations on target | Worker died mid-flight, or cascade-abort tripped | `claude-migrate cleanup <target> --since 2026-04-30T14:37`. Each candidate is verified to have zero messages before deletion. Asks `Proceed? [y/N]` before deleting; pass `--dry-run` to preview. |
| Recents on target are in wrong order | Concurrency > 1 scrambled the migration order | `claude-migrate reorder <target>` walks the source archive in `updated_at ASC` order and bumps each chat's `updated_at` on target. Confirms before running; `--dry-run` previews. |
| Daily timer fires but does nothing | `sessionKey` expired | Check `data/logs/backup.log`. If it shows 401, run `claude-migrate add <source>` |

---

## Architecture

| Concern | Choice |
|---|---|
| HTTP | `curl_cffi` impersonating Chrome 131 (defeats Cloudflare TLS fingerprinting on `claude.ai`; plain `requests`/`httpx` get 403'd). |
| Concurrency | Hard cap of 5 (`asyncio.Semaphore(5)`). 429 backoff: capped exponential `2 → 4 → 8 → 16 → 32 → 60`. |
| Storage | SQLite + FTS5 with gzipped raw-JSON sidecars at `data/raw/{date}/`. |
| Idempotency key | `migration_log(source_uuid, target_profile)` — re-running any command is safe. |
| Secrets | OS-native keychain via `keyring`, with AES-256-GCM file fallback. |
| Auth | Cookie paste only — no Playwright, no browser automation. |
| Scheduling | OS-native (systemd / launchd / Task Scheduler / cron). |

```
claude_migrate/
├── auth.py        # cookie paste flow, normalization, format validation, keyring storage
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
uv run pytest              # ~290 tests
uv run mypy claude_migrate # strict mode
uv run ruff check
```

The test suite covers auth normalization (every common paste mistake), the HTTP layer's status-code → typed-error mapping, `Retry-After` parsing, transcript rendering (thinking summaries, tool-call placeholders, citations, files, project context), the restore-state log projection, the rate-limit pacer (server-hint cooldown floor, AIMD growth/decay, cascade-abort detection, parallel safety), the archive-only worker, and the per-payload wire format on the way to `/completion`.

---

## License

MIT. See [LICENSE](LICENSE).
