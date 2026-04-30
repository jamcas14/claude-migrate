# claude-migrate

Migrate one Claude.ai consumer account into another, or back up an account incrementally to local SQLite. Conversations land in **target's Recents** with the original transcripts intact, projects and custom styles re-create natively, and memory imports through Anthropic's official paste flow.

```bash
claude-migrate login source                  # paste cookies once (~30s)
claude-migrate login target                  # second account
claude-migrate migrate source target --execute   # clone source onto target
```

> **Heads-up:** Anthropic's Consumer Terms forbid scraping (§3.4) and automation (§3.7). This tool exists for migrating between **your own** accounts. You accept the risk that the affected accounts may be rate-limited or suspended. The CLI prompts you to acknowledge this on first run.

---

## Install

```bash
git clone <repo-url>
cd claude-migrate
uv sync                       # creates .venv with all deps
uv run claude-migrate --help
```

If you'd rather have it on `$PATH`:

```bash
pipx install --editable .
claude-migrate --help
```

Requires Python 3.12+. macOS, Linux, and Windows are supported.

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
| `claude-migrate login NAME` | Walks you through pasting `sessionKey` and `cf_clearance` from DevTools. Idempotent — running against an existing name overwrites the stored cookies (use this to refresh after expiry). |
| `claude-migrate logout NAME` | Removes the profile from the keychain. |
| `claude-migrate rename OLD NEW` | Renames a stored profile (typo fix or naming change). Pure metadata — no re-paste, no network call. |
| `claude-migrate accounts` | Lists stored profiles + their last-known identity. No network. Prints management hints for `login`/`rename`/`logout`/`whoami`. |
| `claude-migrate whoami NAME` | Probes the profile against `/api/bootstrap`, prints the live identity, updates the stored `last_probe_ok` timestamp. |

### Migration

| Command | What it does |
|---|---|
| `claude-migrate backup PROFILE [--full]` | One-shot incremental archive of a profile. Use this if you only want a backup without migrating. |
| `claude-migrate migrate SOURCE TARGET` | Dry-run plan: backs up source, shows what would be migrated to target. |
| `claude-migrate migrate SOURCE TARGET --execute` | Actually do it. Re-running is idempotent — already-migrated objects are skipped via the `migration_log` table. |
| `claude-migrate verify TARGET [--reconcile]` | Probe each migrated chat on target to confirm it's still there. `--reconcile` drops `migration_log` rows for chats that have been deleted on the server. |
| `claude-migrate reorder TARGET [--execute]` | No-op PUT each migrated chat on target in source's `updated_at` order, so target's Recents matches the source's. No model calls. |
| `claude-migrate cleanup TARGET --since ISO [--execute]` | Delete empty (zero-message) chats on target created during a failed run. Each candidate is verified to have zero messages before deletion — real chats are never touched. |
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
# 1. Authenticate both accounts (cookies stored in OS keychain).
claude-migrate login source
claude-migrate login target

# 2. Dry-run preview (default — shows what would happen).
claude-migrate migrate source target

# 3. Actually migrate. Idempotent, re-runnable.
claude-migrate migrate source target --execute

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

## Auth walkthrough

`login` walks you through pasting two cookies from DevTools. Anthropic marks `sessionKey` as `HttpOnly`, so the JS console can't see it — you need DevTools' Application/Storage tab. About 30 seconds per profile.

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
$ claude-migrate login source

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
  → `claude-migrate login source`    re-paste cookies after expiry
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
| `403` no body | "Cloudflare blocked the TLS fingerprint…" | `pip install -U curl_cffi` and retry |
| network | "Network error while probing claude.ai…" | Check VPN/proxy/connection |

---

## Daily auto-backup

`schedule install` registers a per-OS native scheduler unit that runs `claude-migrate backup source --quiet` once a day. The action:

- Hits one paginated `/api/.../chat_conversations` request to find changed conversations
- Re-fetches only those (incremental — usually 0–5 detail requests)
- Writes raw gzipped JSON sidecars to `data/raw/{date}/`
- Updates SQLite + the `checkpoint` table
- Logs to `data/logs/dump.log`

Per-OS specifics:

| OS | Backend | Unit file |
|---|---|---|
| Linux (with systemd) | systemd `--user` | `~/.config/systemd/user/claude-migrate.{service,timer}` |
| Linux (no systemd) | cron | line in `crontab -l` tagged `# claude-migrate (managed)` |
| macOS | launchd | `~/Library/LaunchAgents/com.user.claudemigrate.plist` |
| Windows | Task Scheduler | task name `claude-migrate` |

When `sessionKey` eventually expires, the timer fires, hits 401, exits 75, and writes the failure to `dump.log`. Re-run `claude-migrate login source` to refresh.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `auth says "looks too short"` | DevTools Value column truncates the display | Click the row, copy from the details panel below the table |
| `auth says "Cloudflare is challenging the request"` | Stale `cf_clearance` | Refresh `claude.ai` in your browser, then `claude-migrate login <profile>` and re-paste |
| `auth says "TLS fingerprint reject"` | curl_cffi out of date | `pip install -U curl_cffi`, retry |
| Restore is hitting 429 every chat | Per-account rate limit on `/completion` | Default 90s/chat keeps most accounts under the limit. Override: `export CLAUDE_MIGRATE_CHAT_SLEEP_SEC=120`. The CLI auto-cools-down on 429 (capped exponential up to 600s). |
| `migrate` fails with HTTP 400/422 | `anthropic-client-sha` rotated | `claude-migrate headers-help` for the capture walkthrough; `claude-migrate config edit` to put the new values in `config.toml` |
| Restore was interrupted; what's left? | — | `claude-migrate status target` reads `migration_log` (no network) and prints done/total per object type plus recent failures |
| Failed restore left empty conversations on target | Worker died mid-flight | Find the timestamp of the failed run, then `claude-migrate cleanup target --since 2026-04-30T14:37 --execute`. Each candidate is verified to have zero messages before deletion. |
| Recents on target are in wrong order | Concurrency > 1 scrambled the migration order | `claude-migrate reorder target --execute` walks source archive in `updated_at ASC` order and bumps each chat's `updated_at` on target |
| Daily timer fires but does nothing | `sessionKey` expired | Check `data/logs/dump.log`. If it shows 401, run `claude-migrate login source` |

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
├── session.py     # `open_session(profile)` — load profile + create client + bind org_uuid
├── discover.py    # /api/bootstrap → org_uuid (no side effects)
├── fetch.py       # async fan-out: orgs → projects → docs → conversations
├── render.py      # JSON → XML transcript builder (uses rendering_mode=messages)
├── transport.py   # /completion SSE + multipart upload + send_payload dispatch
├── store.py       # SQLite schema + UPSERTs + raw-sidecar writer
├── state.py       # RestoreState — owns migration_log per (conn, target_profile)
├── runner.py      # WorkerOutcome + Pacer (rate-limit barrier) + migrate_row helper
├── restore.py     # Per-object-type restore loops on top of runner + state
├── migrate.py     # Top-level orchestrator (run_restore, dry_run_plan, verify_target_conversations)
├── memory.py      # Extraction prompt + clipboard helper
├── notify.py      # ntfy / osascript / Windows toast on failures
├── scheduler.py   # OS-native daily timer install/uninstall
├── checkpoint.py  # last_seen_updated_at + content-hash dedup
├── models.py      # Pydantic v2 models with extra="allow"
├── errors.py      # Typed exception hierarchy (AuthExpired, RateLimited, …)
└── cli.py         # Click commands (verb-first, positional args)
```

---

## Development

```bash
uv sync --all-extras
uv run pytest             # 148+ tests
uv run mypy claude_migrate # strict mode
uv run ruff check
```

The test suite covers auth normalization (every common paste mistake), the HTTP layer's status-code → typed-error mapping, transcript rendering (thinking summaries, tool-call placeholders, citations, files, project context), the restore-state log projection, the rate-limit pacer (cooldown growth, reset, parallel safety), and the per-payload wire format on the way to `/completion`.

---

## License

MIT. See [LICENSE](LICENSE).
