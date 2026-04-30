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
| `claude-migrate login NAME` | Walks you through pasting `sessionKey` and `cf_clearance` from DevTools. Idempotent — running against an existing name overwrites the stored cookies. |
| `claude-migrate logout NAME` | Removes the profile from the keychain. |
| `claude-migrate accounts` | Lists stored profiles + their last-known identity. No network. |
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
| `claude-migrate headers-help` | Step-by-step guide to capturing `anthropic-client-version` / `anthropic-client-sha` from your browser (only needed if `/api/*` returns 400/422). |

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

`login` walks you through pasting two cookies from DevTools. Anthropic marks `sessionKey` as `HttpOnly`, so the JS console can't see it — you need DevTools' Application/Storage tab. The prompt itself shows the exact steps for Chromium-based browsers (Chrome, Edge, Brave, Arc, Opera, Vivaldi), Firefox, and Safari. About 30 seconds.

```
$ claude-migrate login source

To authenticate, you'll paste two cookies from your browser.
This takes about 30 seconds and you only do it once per account.

────────────────────────────────────────────────────────────────
Step 1 — Open https://claude.ai signed in to the SOURCE account.
Step 2 — Press F12 to open DevTools.
Step 3 — Find the cookies viewer:
         ▸ Chromium browsers: "Application" tab → Cookies → claude.ai
         ▸ Firefox:           "Storage" tab → Cookies → claude.ai
         ▸ Safari:            Develop → Show Web Inspector → Storage → Cookies → claude.ai
Step 4 — Find the row named `sessionKey`. The Value column is often
         truncated! Click the row, then look at the panel below to
         see the full value. Triple-click and copy.

Paste the value of `sessionKey` (starts with `sk-ant-sid<NN>-`):
> sk-ant-sid01-…
✓ format OK

Paste cf_clearance:
> Zk0c.W3.…
✓ format OK

Probing claude.ai/api/bootstrap to confirm credentials...
  ✓ Authenticated as: foo@example.com
  ✓ Organization:     Foo's Workspace (uuid: a1b2c3d4-…)

Stored as profile: source.
```

The CLI silently strips common copy-paste mistakes (surrounding quotes, `sessionKey:` / `name=value` prefixes, trailing semicolons, URL-encoded characters) and validates format before any network call. Failure modes have specific recovery messages — `401` → re-copy sessionKey, `403` + Cloudflare HTML → refresh `claude.ai` once and re-paste cf_clearance only, etc.

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
| `migrate` fails with HTTP 400/422 | `anthropic-client-sha` rotated | Run `claude-migrate headers-help`, capture the new value, re-run |
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
