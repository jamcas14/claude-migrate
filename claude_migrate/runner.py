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
    `retry_after_sec` carries the server's `Retry-After` hint when present
    so the Pacer can use it as the cooldown floor instead of a fixed schedule.
    """

    target_uuid: str | None
    error: str | None
    rate_limited: bool = False
    retry_after_sec: float | None = None

    @classmethod
    def ok(cls, target_uuid: str) -> WorkerOutcome:
        return cls(target_uuid=target_uuid, error=None)

    @classmethod
    def failed(
        cls, error: str, *,
        rate_limited: bool = False,
        retry_after_sec: float | None = None,
    ) -> WorkerOutcome:
        return cls(
            target_uuid=None, error=error,
            rate_limited=rate_limited, retry_after_sec=retry_after_sec,
        )


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
    """Adaptive pacing + 429 cooldown for a restore loop, serial or parallel.

    Two methods:
      * `before()` — call BEFORE issuing a request. Blocks if a 429 cooldown
        is active. Cheap when not paused (one lock acquisition).
      * `after(outcome)` — call AFTER the request returns. On rate-limited
        outcome: extends the shared pause-until barrier (using the server's
        `Retry-After` when present, the configured `rate_limit_sleep_sec`
        floor otherwise) and adapts `_current_base` upward. On success:
        adapts `_current_base` downward after a streak, then sleeps it.

    AIMD on `_current_base`: doubles on each rate-limit, divides by 1.5 after
    every 3 consecutive successes. Bounded `[base_min, base_max]`. Replaces
    the previous fixed-base + 2** cooldown-multiplier scheme — two layered
    AIMD systems overcorrect; pick one knob (the per-success base sleep) and
    let cooldown be deterministic, driven by the server's signal.

    The pause window is a barrier shared across workers, not a sleep held by
    the failed worker. Avoids the lock-held-during-cooldown deadlock where
    every worker stalls behind a single 429.
    """

    base_sleep_sec: float
    """Initial / maximum value of the adaptive `_current_base`. AIMD will
    decrease below this on success streaks (down to `base_min`) and grow
    back up to but not past this on rate-limits."""

    rate_limit_sleep_sec: float
    """Cooldown floor used when the server didn't send `Retry-After`."""

    base_min: float = 5.0
    """Lower bound on the adaptive base sleep. 5s avoids hot-looping the API."""

    max_cooldown_sec: float = 600.0
    """Upper clamp on a single cooldown window."""

    rate_limit_min_floor: float = 10.0
    """Hard minimum cooldown when ANY 429 hits. Anthropic occasionally sends
    `Retry-After: 0` (a spec-compliant placeholder, not a useful hint) which
    the parser surfaces as a tiny number; trusting it produces tight 429-retry
    loops that achieve nothing. We refuse to retry faster than this floor
    regardless of what the server says."""

    _current_base: float = field(default=0.0, init=False)
    _consecutive_successes: int = field(default=0, init=False)
    _consecutive_rate_limits: int = field(default=0, init=False)
    _pause_until: float = field(default=0.0, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    def __post_init__(self) -> None:
        # Start at base_min so a fresh window can burst-fire; AIMD ramps up
        # only when 429s appear. Caller's `base_sleep_sec` is the ceiling.
        self._current_base = self.base_min

    async def before(self) -> None:
        """Block while a rate-limit pause is active. Cheap when not paused."""
        while True:
            async with self._lock:
                wait = self._pause_until - asyncio.get_running_loop().time()
            if wait <= 0:
                return
            log.info("rate_limit_wait", sleep_sec=round(wait, 1))
            await asyncio.sleep(wait)

    @property
    def consecutive_rate_limits(self) -> int:
        """Number of rate-limited outcomes since the last success. Callers
        use this to detect rate-limit cascades (every chat 429ing) and abort
        instead of wasting more attempts."""
        return self._consecutive_rate_limits

    async def after(self, outcome: WorkerOutcome | None) -> None:
        """Record outcome state and sleep the adaptive base on success."""
        if outcome is None:
            return
        if outcome.rate_limited:
            async with self._lock:
                self._consecutive_successes = 0
                self._consecutive_rate_limits += 1
                # AIMD: multiplicative increase on the per-success sleep,
                # capped at the user's configured `base_sleep_sec` ceiling.
                self._current_base = min(self._current_base * 2, self.base_sleep_sec)
                if self._current_base < self.base_min:
                    self._current_base = self.base_min
                # Cooldown logic. Three signals, in order of trust:
                #   - Server's Retry-After header (if present and reasonable)
                #   - AIMD's _current_base (what we've learned the server tolerates)
                #   - rate_limit_min_floor (hard floor; defends against
                #     `Retry-After: 0` and similar nonsense values)
                # Take the maximum of all available signals, then clamp to
                # max_cooldown_sec. If the server gives no hint, default to
                # the configured fixed `rate_limit_sleep_sec`.
                if outcome.retry_after_sec is not None:
                    cooldown_raw = max(
                        outcome.retry_after_sec,
                        self._current_base,
                        self.rate_limit_min_floor,
                    )
                else:
                    cooldown_raw = max(
                        self.rate_limit_sleep_sec,
                        self._current_base,
                    )
                cooldown = min(cooldown_raw, self.max_cooldown_sec)
                pause_until = asyncio.get_running_loop().time() + cooldown
                if pause_until > self._pause_until:
                    self._pause_until = pause_until
                log.warning(
                    "rate_limit_cooldown",
                    cooldown_sec=cooldown,
                    retry_after_from_server=outcome.retry_after_sec,
                    next_base_sleep=self._current_base,
                    consecutive_rate_limits=self._consecutive_rate_limits,
                )
            return
        async with self._lock:
            self._consecutive_successes += 1
            self._consecutive_rate_limits = 0
            # AIMD: multiplicative decrease after a streak. The ÷1.5 lets the
            # controller probe for a faster steady state while still backing
            # off quickly when the server pushes back.
            if self._consecutive_successes >= 3 and self._current_base > self.base_min:
                self._current_base = max(self.base_min, self._current_base / 1.5)
                self._consecutive_successes = 0
                log.info("pacer_decreased", next_base_sleep=self._current_base)
            sleep_for = self._current_base
        if sleep_for > 0:
            await asyncio.sleep(sleep_for)
