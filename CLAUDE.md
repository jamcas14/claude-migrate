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
   429 backoff: capped exponential `2 → 4 → 8 → 16 → 32 → 60`.
5. **Encrypt secrets at rest.** Cookies live in OS keychain via `keyring`,
   with AES-256-GCM file fallback. Plaintext credentials on disk = critical bug.
6. **Raw-first storage.** Every API response is gzipped to
   `data/raw/{date}/{slug}-{uuid}.json.gz` *before* parsing — schema breakage
   must never lose data.
7. **Idempotency.** `migration_log(source_uuid, target_profile)` is the
   primary key. Re-running any command must be safe and ~instant when there's
   nothing to do.
8. **Dry-run default** for any command that mutates a remote account.
   `--execute` opt-in.
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
| `state.py` | `RestoreState` — owns `migration_log` for one (conn, target_profile). Methods: `mark_ok`, `mark_error`, `already_migrated`, `project_map`, `pending_count`, `recent_failures`, `confirmed_conversations`, `drop`. |
| `runner.py` | `WorkerOutcome` (typed result), `migrate_row(state, work)` (the per-row idempotency lifecycle), `Pacer` (rate-limit barrier with `before()` / `after()`). |
| `restore.py` | Per-object-type restore loops on top of `runner` + `state`. Each loop is a small worker + `migrate_row`; the runner handles already-migrated checks, log writes, and pacing. |
| `migrate.py` | Top-level orchestrator — `run_restore`, `dry_run_plan`, `migration_status`, `verify_target_conversations`. Uses `open_session`. |
| `scheduler.py` | Per-OS daily timer install/uninstall (systemd/launchd/Task Scheduler/cron). |
| `cli.py` | Click commands. Verb-first, positional args. Dry-run default. |
| `errors.py` | Typed exception hierarchy. Catch specifically; never `except Exception`. |

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
  `await` inside a transactional unit, use `async with async_transaction(
  conn): ...` instead — it holds a per-connection `asyncio.Lock` so the
  scheduler can't interleave two BEGIN/COMMIT pairs on one connection.

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
