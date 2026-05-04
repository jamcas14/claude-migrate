# claude-migrate — contributor / Claude Code context

User-facing docs live in [README.md](README.md). This file is for contributors
working in the codebase (humans or Claude Code).

## Mission

Move a Claude.ai consumer account's conversations, projects, custom styles,
and profile prefs into another account, preserving original transcript
content as the first user message of each new chat. Memory is manual via
Anthropic's `claude.com/import-memory`.

## Hard constraints

1. **`/completion` is the only message-write endpoint** and it always invokes
   the model. There is no synthetic-history parameter. Migration uses
   transcript-paste as a single first user message; we do **not** call
   `/completion` for any other purpose.
2. **No browser automation.** No Playwright, no `pycookiecheat`, no Selenium.
   The auth flow is exclusively the manual cookie paste.
3. **All HTTP via `curl_cffi`** with `impersonate="chrome131"`. Plain
   `requests`/`httpx` get 403'd by Cloudflare on `claude.ai`.
4. **Concurrency cap of 5** (`asyncio.Semaphore(5)` at the HTTP layer).
   HTTP-layer 429 retry: capped exponential `2 → 4 → 8 → 16 → 32 → 60`.
   Outer-loop 429 cooldown is owned by `runner.Pacer` (AIMD on per-success
   sleep + server-`Retry-After` floor + cascade-abort after 5 in a row).
5. **Encrypt secrets at rest.** Cookies live in OS keychain via `keyring`,
   with AES-256-GCM file fallback. Plaintext credentials on disk = critical bug.
6. **Raw-first storage.** Every API response is gzipped to
   `data/raw/{date}/{slug}-{uuid}.json.gz` *before* parsing — schema breakage
   must never lose data.
7. **Idempotency.** `migration_log(source_uuid, target_profile)` is the
   primary key. Re-running any command must be safe and ~instant when there's
   nothing to do.
8. **Confirm before mutating a remote account.** Every command that writes
   to a remote account prints a plan and asks `Proceed? [y/N]`. `--dry-run`
   exits after the plan; `--yes` skips the prompt for automation. There is
   no `--execute` flag — the default *is* execute-after-confirm.
9. **Auth never silently fails.** Every auth failure produces a specific
   error message naming the likely cause and the exact recovery action.

## Architecture pointers

| Module | Job |
|---|---|
| `auth.py` | Cookie paste flow + normalization + format validation + keyring storage |
| `client.py` | One HTTP layer: cookie + header injection, retry/backoff, 429 cooldown, status → typed-error mapping |
| `session.py` | `open_session(profile)` async context manager — loads profile, instantiates client, discovers org, binds creds. Use this everywhere instead of repeating the incantation. |
| `discover.py` | `discover_org(client) → (uuid, name, email)`. Pure read — no side effects on the client. |
| `fetch.py` | Async fan-out: orgs → projects → docs → conversations. Uses `rendering_mode=messages` (not `raw`) — see "rendering mode" note below. |
| `render.py` | Stored conversation → XML transcript. Thinking blocks render as their `summaries[0].summary` (one-line UI summary), NOT raw thinking. |
| `transport.py` | `/completion` SSE + multipart upload + `send_payload(...)` dispatch over `InlinePayload | AttachmentPayload | ChunkedPayload`. |
| `store.py` | SQLite schema + UPSERTs + raw-sidecar writer. Idempotent on uuid. |
| `state.py` | `RestoreState` — owns `migration_log` for one (conn, target_profile). Methods: `mark_ok`, `mark_error`, `mark_bookmarked`, `already_migrated`, `already_bookmarked`, `project_map`, `pending_count`, `recent_failures`, `confirmed_conversations`, `bookmarked_conversations`, `all_migrated_target_uuids`, `drop`. Statuses: `'ok'` (loaded), `'bookmarked'` (`--bookmark`-mode stub, transcript not yet pasted), `'error'`. `already_migrated` filters on `'ok'` only — bookmark mode reads via `already_bookmarked`. |
| `runner.py` | `WorkerOutcome` (typed result), `migrate_row(state, work)` (the per-row idempotency lifecycle), `Pacer` (AIMD per-success sleep + server-`Retry-After`-floored cooldown + cascade detection via `consecutive_rate_limits`). |
| `restore.py` | Per-object-type restore loops on top of `runner` + `state`. Each loop is a small worker + `migrate_row`; the runner handles already-migrated checks, log writes, and pacing. The conversation loop adds cascade-abort: 5 consecutive 429s with no successes ends the run cleanly instead of orphaning more empty chats. |
| `archive.py` | `--archive-only` worker. Renders every conversation as a markdown doc and POSTs to `/api/.../projects/{p}/docs` on a single new project on target. Bypasses `/completion` entirely — finishes in minutes, but chats live inside one Project rather than as Recents entries. |
| `bookmark.py` | `--bookmark` worker + `claude-migrate load` command. **Bookmark phase**: for each source chat, POST `/chat_conversations` with name `[ul|YYYY-MM-DD] Title`. No project, no `/completion`. `migration_log` row stamped `status='bookmarked'`. **Load phase**: pattern-match bookmarked chats from migration_log (URL paste / full UUID / hex prefix / title substring all supported), render via existing `render.prepare_paste_payload`, paste via existing `transport.send_payload`, rename to default-mode `[YYYY-MM-DD] Title` shape, flip `status='bookmarked'` → `status='ok'`. Refuses to load into a non-empty target chat without `--force`. |
| `migrate.py` | Top-level orchestrator — `run_restore`, `dry_run_plan`, `migration_status`, `verify_target_conversations`. Uses `open_session`. |
| `scheduler.py` | Per-OS daily timer install/uninstall (systemd/launchd/Task Scheduler/cron). |
| `cli.py` | Click commands. Verb-first, positional args. `Proceed? [y/N]` confirm by default; `--dry-run` for preview, `--yes` to skip the prompt. |
| `errors.py` | Typed exception hierarchy. Catch specifically; never `except Exception`. |
| `config.py` | Paths (`data_dir`, `config_dir`) + pydantic-settings `Settings` (env vars + `config.toml`). |

## Rendering mode

claude.ai's `/api/.../chat_conversations/{c}` accepts `rendering_mode=raw` and
`rendering_mode=messages`. Use **`messages`**:

- `raw` flattens everything into one `text` field with mobile-style placeholders
  ("This block is not supported on your current device yet.") for tool calls.
- `messages` returns `content` as a structured list of typed blocks (`text`,
  `thinking` with `summaries[0].summary`). Tool_use/tool_result blocks are
  still server-stripped in `messages` mode — they appear as the placeholder
  string inside `text` blocks. The renderer collapses these to a single
  `<tool_use name="(stripped)" />` marker.

Citations field exists on every text block but is empty in production. Render
defensively in case the API ever populates them.

## Conventions

- **`mypy --strict`.** Allow `Any` only in API-boundary parsing (`fetch.py`,
  `discover.py` shape probing) since claude.ai's payloads have unstable shape.
- **All I/O is async.** No blocking calls in the hot path.
- **One thin HTTP layer.** Every request goes through `client.py::request()`.
- **API → SQLite directly.** Validate at usage sites (`isinstance(payload,
  dict)`, key checks); we don't run Pydantic models since claude.ai's
  payloads change too often to keep typed shapes in sync.
- **Errors are typed.** `errors.py` defines them; `cli._run` maps them to
  exit codes with specific user-facing messages.
- **Logs.** `structlog` with `ConsoleRenderer` to stderr on TTY. Use
  `log.info("event_name", key=value, …)` — events are searchable.
- **No `await` inside `with transaction(conn): ...`.** SQLite connections
  don't multiplex transactions per coroutine. If you genuinely need an
  `await` inside a transactional unit, wrap the connection in an
  `asyncio.Lock` at the call site — `transaction()` itself stays
  synchronous-body-only.

## Build / test

```bash
uv sync --all-extras
uv run pytest
uv run mypy claude_migrate
uv run ruff check
```

Tests live in `tests/`. The exhaustive auth-paste verification matrix is in
`test_auth.py`. The pacer's cooldown behaviour and parallel-safety properties
live in `test_runner.py`. The transcript renderer's edge cases (thinking
summaries, tool placeholders, citations, files, project context) live in
`test_render.py`.
