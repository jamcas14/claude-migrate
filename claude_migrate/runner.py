"""Per-row migration runner.

`migrate_row` is the single home of "what does it mean to migrate one row":
check the migration_log for prior success, run the worker, write the outcome
back. `Pacer` owns the inter-call sleep and the capped exponential cooldown
that the serial conversation phase needs.

Phases (styles, projects, conversations) reduce to: query rows, build a
worker per row, call `migrate_row`. The shape used to be triplicated.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import structlog

from .state import RestoreState

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class WorkerOutcome:
    """What a per-row worker returns.

    Exactly one of `target_uuid` and `error` is set. `rate_limited=True` flags
    transient 429s so a `Pacer` can apply cooldown — this carries the type
    signal that used to be reconstructed by string-matching `"429" in err`.
    """

    target_uuid: str | None
    error: str | None
    rate_limited: bool = False

    @classmethod
    def ok(cls, target_uuid: str) -> WorkerOutcome:
        return cls(target_uuid=target_uuid, error=None)

    @classmethod
    def failed(cls, error: str, *, rate_limited: bool = False) -> WorkerOutcome:
        return cls(target_uuid=None, error=error, rate_limited=rate_limited)


async def migrate_row(
    *,
    state: RestoreState,
    object_type: str,
    source_uuid: str,
    work: Callable[[], Awaitable[WorkerOutcome]],
) -> WorkerOutcome | None:
    """Run one row through the migration_log lifecycle.

    Returns None if the row was already migrated `ok` (skipped). Otherwise
    returns the worker's outcome — already recorded to migration_log.

    Workers should let session-fatal exceptions (AuthExpired,
    CloudflareChallenge) propagate; every other failure should be turned into
    `WorkerOutcome.failed(...)`.
    """
    if state.already_migrated(source_uuid):
        return None
    outcome = await work()
    if outcome.target_uuid is not None:
        state.mark_ok(
            source_uuid=source_uuid,
            object_type=object_type,
            target_uuid=outcome.target_uuid,
        )
    else:
        state.mark_error(
            source_uuid=source_uuid,
            object_type=object_type,
            error=outcome.error or "unknown",
        )
    return outcome


@dataclass
class Pacer:
    """Pacing + 429 cooldown for a restore loop, serial or parallel.

    Two methods:
      * `before()` — call BEFORE issuing a request. Blocks if a 429 cooldown
        is active. Cheap when not paused (one lock acquisition).
      * `after(outcome)` — call AFTER the request returns. On rate-limited
        outcome: bumps the pause window so future `before()` calls wait, and
        returns immediately so the failed worker frees its slot fast. On
        success: sleeps the base inter-call interval. On `outcome=None`
        (skipped row): no-op.

    The pause window is a barrier shared across workers, not a sleep held by
    the failed worker. That avoids the lock-held-during-cooldown deadlock
    where every worker stalled behind a single 429.
    """

    base_sleep_sec: float
    rate_limit_sleep_sec: float
    max_cooldown_sec: float = 600.0
    _consecutive_rate_limits: int = field(default=0, init=False)
    _pause_until: float = field(default=0.0, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    async def before(self) -> None:
        """Block while a rate-limit pause is active. Cheap when not paused."""
        while True:
            async with self._lock:
                wait = self._pause_until - asyncio.get_event_loop().time()
            if wait <= 0:
                return
            log.info("rate_limit_wait", sleep_sec=round(wait, 1))
            await asyncio.sleep(wait)

    async def after(self, outcome: WorkerOutcome | None) -> None:
        """Record outcome state and sleep the base interval on success."""
        if outcome is None:
            return
        if outcome.rate_limited:
            async with self._lock:
                self._consecutive_rate_limits += 1
                multiplier = min(2 ** (self._consecutive_rate_limits - 1), 4)
                cooldown = min(
                    self.rate_limit_sleep_sec * multiplier, self.max_cooldown_sec
                )
                pause_until = asyncio.get_event_loop().time() + cooldown
                if pause_until > self._pause_until:
                    self._pause_until = pause_until
                log.warning(
                    "rate_limit_cooldown",
                    consecutive=self._consecutive_rate_limits,
                    sleep_sec=cooldown,
                )
            return
        async with self._lock:
            self._consecutive_rate_limits = 0
        if self.base_sleep_sec > 0:
            await asyncio.sleep(self.base_sleep_sec)
