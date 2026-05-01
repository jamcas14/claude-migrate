"""Tests for the per-row runner and the serial-loop pacer."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from claude_migrate.runner import Pacer, WorkerOutcome, migrate_row
from claude_migrate.state import RestoreState

# ---------------------------------------------------------------------------
# WorkerOutcome
# ---------------------------------------------------------------------------


def test_outcome_ok_has_no_error() -> None:
    o = WorkerOutcome.ok("tgt-1")
    assert o.target_uuid == "tgt-1"
    assert o.error is None
    assert o.rate_limited is False


def test_outcome_failed_has_no_target() -> None:
    o = WorkerOutcome.failed("boom")
    assert o.target_uuid is None
    assert o.error == "boom"
    assert o.rate_limited is False


def test_outcome_failed_can_flag_rate_limited() -> None:
    o = WorkerOutcome.failed("RateLimited: 429", rate_limited=True)
    assert o.rate_limited is True


# ---------------------------------------------------------------------------
# migrate_row
# ---------------------------------------------------------------------------


async def test_migrate_row_skips_already_migrated(db_conn: sqlite3.Connection) -> None:
    state = RestoreState(db_conn, "t")
    state.mark_ok(source_uuid="s1", object_type="conversation", target_uuid="prior")

    called = False

    async def work() -> WorkerOutcome:
        nonlocal called
        called = True
        return WorkerOutcome.ok("new")

    result = await migrate_row(
        state=state, object_type="conversation", source_uuid="s1", work=work,
    )
    assert result is None
    assert called is False, "worker must not run for already-migrated rows"


async def test_migrate_row_marks_ok_on_success(db_conn: sqlite3.Connection) -> None:
    state = RestoreState(db_conn, "t")

    async def work() -> WorkerOutcome:
        return WorkerOutcome.ok("tgt-1")

    result = await migrate_row(
        state=state, object_type="conversation", source_uuid="s1", work=work,
    )
    assert result is not None
    assert result.target_uuid == "tgt-1"
    assert state.already_migrated("s1") == "tgt-1"


async def test_migrate_row_marks_error_on_failure(db_conn: sqlite3.Connection) -> None:
    state = RestoreState(db_conn, "t")

    async def work() -> WorkerOutcome:
        return WorkerOutcome.failed("boom")

    result = await migrate_row(
        state=state, object_type="conversation", source_uuid="s1", work=work,
    )
    assert result is not None
    assert result.error == "boom"
    assert state.already_migrated("s1") is None  # error rows don't count
    failures = state.recent_failures()
    assert failures[0]["error"] == "boom"


async def test_migrate_row_propagates_session_fatal(db_conn: sqlite3.Connection) -> None:
    """Workers can let session-fatal exceptions bubble through migrate_row."""
    from claude_migrate.errors import AuthExpired

    state = RestoreState(db_conn, "t")

    async def work() -> WorkerOutcome:
        raise AuthExpired("session ended")

    import pytest

    with pytest.raises(AuthExpired):
        await migrate_row(
            state=state, object_type="conversation", source_uuid="s1", work=work,
        )
    # No row should have been written for s1 — the exception bubbled before the write.
    assert state.already_migrated("s1") is None


# ---------------------------------------------------------------------------
# Pacer
# ---------------------------------------------------------------------------


async def test_pacer_sleeps_base_after_success(monkeypatch: object) -> None:
    sleeps: list[float] = []

    async def fake_sleep(t: float) -> None:
        sleeps.append(t)

    asyncio_mod = asyncio  # local handle so type checker sees a non-imported name
    real = asyncio_mod.sleep
    asyncio_mod.sleep = fake_sleep  # type: ignore[assignment]
    try:
        pacer = Pacer(base_sleep_sec=5.0, rate_limit_sleep_sec=300.0)
        await pacer.after(WorkerOutcome.ok("x"))
    finally:
        asyncio_mod.sleep = real  # type: ignore[assignment]
    assert sleeps == [5.0]


async def test_pacer_starts_at_base_min_not_base_sleep_sec() -> None:
    """AIMD-Pacer initializes at base_min so a fresh window can burst-fire.
    `base_sleep_sec` is the *ceiling* the controller can reach, not the
    starting value."""
    sleeps: list[float] = []

    async def fake_sleep(t: float) -> None:
        sleeps.append(t)

    real = asyncio.sleep
    asyncio.sleep = fake_sleep  # type: ignore[assignment]
    try:
        pacer = Pacer(base_sleep_sec=90.0, rate_limit_sleep_sec=300.0, base_min=5.0)
        await pacer.after(WorkerOutcome.ok("x"))
    finally:
        asyncio.sleep = real  # type: ignore[assignment]
    # First success → sleep base_min (5s), not the 90s ceiling.
    assert sleeps == [5.0]


async def test_pacer_after_does_not_sleep_on_rate_limited() -> None:
    """The new design: a rate-limited outcome only updates pause_until — the
    failed worker itself returns immediately so it doesn't hold its slot."""
    sleeps: list[float] = []

    async def fake_sleep(t: float) -> None:
        sleeps.append(t)

    real = asyncio.sleep
    asyncio.sleep = fake_sleep  # type: ignore[assignment]
    try:
        pacer = Pacer(base_sleep_sec=10.0, rate_limit_sleep_sec=100.0)
        await pacer.after(WorkerOutcome.failed("429", rate_limited=True))
        await pacer.after(WorkerOutcome.failed("429", rate_limited=True))
    finally:
        asyncio.sleep = real  # type: ignore[assignment]
    # No sleeps in `after` — pause is registered for `before()` to respect.
    assert sleeps == []


async def test_pacer_before_blocks_until_pause_window_passes() -> None:
    """before() should sleep for the remaining pause time set by a 429."""
    fake_now = [1000.0]
    sleeps: list[float] = []

    async def fake_sleep(t: float) -> None:
        sleeps.append(t)
        fake_now[0] += t  # pretend time advanced by t

    class FakeLoop:
        def time(self) -> float:
            return fake_now[0]

    real_sleep = asyncio.sleep
    real_get_loop = asyncio.get_running_loop
    asyncio.sleep = fake_sleep  # type: ignore[assignment]
    asyncio.get_running_loop = lambda: FakeLoop()  # type: ignore[assignment,return-value]
    try:
        pacer = Pacer(base_sleep_sec=0.0, rate_limit_sleep_sec=100.0)
        await pacer.after(WorkerOutcome.failed("429", rate_limited=True))
        # pause_until is now fake_now + 100.
        await pacer.before()
    finally:
        asyncio.sleep = real_sleep  # type: ignore[assignment]
        asyncio.get_running_loop = real_get_loop  # type: ignore[assignment]
    assert sleeps == [100.0]


async def test_pacer_cooldown_uses_fixed_floor_when_no_retry_after() -> None:
    """Without a server-side Retry-After hint, every 429 cooldown uses the
    same `rate_limit_sleep_sec` floor — no per-failure multiplier (the old
    2** scheme is replaced by AIMD on base_sleep instead, plus deterministic
    cooldown driven by Retry-After)."""
    fake_now = [1000.0]
    pause_seen: list[float] = []

    async def fake_sleep(t: float) -> None:
        pause_seen.append(t)
        fake_now[0] += t

    class FakeLoop:
        def time(self) -> float:
            return fake_now[0]

    real_sleep = asyncio.sleep
    real_get_loop = asyncio.get_running_loop
    asyncio.sleep = fake_sleep  # type: ignore[assignment]
    asyncio.get_running_loop = lambda: FakeLoop()  # type: ignore[assignment,return-value]
    try:
        pacer = Pacer(
            base_sleep_sec=30.0, rate_limit_sleep_sec=100.0, max_cooldown_sec=400.0,
        )
        for _ in range(4):
            await pacer.after(WorkerOutcome.failed("429", rate_limited=True))
            await pacer.before()
    finally:
        asyncio.sleep = real_sleep  # type: ignore[assignment]
        asyncio.get_running_loop = real_get_loop  # type: ignore[assignment]
    # Every cooldown is the fixed 100s floor — no exponential growth.
    assert pause_seen == [100.0, 100.0, 100.0, 100.0]


async def test_pacer_cooldown_honors_retry_after_when_present() -> None:
    """Server-sent Retry-After overrides the configured `rate_limit_sleep_sec`
    floor — usually shorter (30-90s) than our 300s default."""
    fake_now = [1000.0]
    pause_seen: list[float] = []

    async def fake_sleep(t: float) -> None:
        pause_seen.append(t)
        fake_now[0] += t

    class FakeLoop:
        def time(self) -> float:
            return fake_now[0]

    real_sleep = asyncio.sleep
    real_get_loop = asyncio.get_running_loop
    asyncio.sleep = fake_sleep  # type: ignore[assignment]
    asyncio.get_running_loop = lambda: FakeLoop()  # type: ignore[assignment,return-value]
    try:
        pacer = Pacer(base_sleep_sec=30.0, rate_limit_sleep_sec=300.0)
        # Server sends Retry-After: 45 — Pacer should respect it.
        await pacer.after(WorkerOutcome.failed(
            "429", rate_limited=True, retry_after_sec=45.0,
        ))
        await pacer.before()
    finally:
        asyncio.sleep = real_sleep  # type: ignore[assignment]
        asyncio.get_running_loop = real_get_loop  # type: ignore[assignment]
    assert pause_seen == [45.0]


async def test_pacer_aimd_doubles_base_on_rate_limit() -> None:
    """AIMD: on rate-limited outcome, current_base doubles toward the
    configured ceiling. Fresh pacer starts at base_min=5 with ceiling=60."""
    sleeps: list[float] = []

    async def fake_sleep(t: float) -> None:
        sleeps.append(t)

    real = asyncio.sleep
    asyncio.sleep = fake_sleep  # type: ignore[assignment]
    try:
        pacer = Pacer(base_sleep_sec=60.0, rate_limit_sleep_sec=300.0, base_min=5.0)
        # Sequence: fail (5→10), success → sleep 10
        await pacer.after(WorkerOutcome.failed("429", rate_limited=True))
        await pacer.after(WorkerOutcome.ok("x"))
        # fail (10→20), success → sleep 20
        await pacer.after(WorkerOutcome.failed("429", rate_limited=True))
        await pacer.after(WorkerOutcome.ok("y"))
        # fail (20→40), success → sleep 40
        await pacer.after(WorkerOutcome.failed("429", rate_limited=True))
        await pacer.after(WorkerOutcome.ok("z"))
        # fail (40→60, capped at ceiling), success → sleep 60
        await pacer.after(WorkerOutcome.failed("429", rate_limited=True))
        await pacer.after(WorkerOutcome.ok("w"))
        # fail (still 60, ceiling), success → still 60
        await pacer.after(WorkerOutcome.failed("429", rate_limited=True))
        await pacer.after(WorkerOutcome.ok("v"))
    finally:
        asyncio.sleep = real  # type: ignore[assignment]
    assert sleeps == [10.0, 20.0, 40.0, 60.0, 60.0]


async def test_pacer_aimd_decreases_after_three_successes() -> None:
    """After 3 consecutive successes, current_base divides by 1.5
    (multiplicative-decrease). Floors at base_min."""
    sleeps: list[float] = []

    async def fake_sleep(t: float) -> None:
        sleeps.append(t)

    real = asyncio.sleep
    asyncio.sleep = fake_sleep  # type: ignore[assignment]
    try:
        pacer = Pacer(base_sleep_sec=60.0, rate_limit_sleep_sec=300.0, base_min=5.0)
        # Pump current_base up via three rate-limits: 5 → 10 → 20 → 40
        await pacer.after(WorkerOutcome.failed("x", rate_limited=True))
        await pacer.after(WorkerOutcome.failed("x", rate_limited=True))
        await pacer.after(WorkerOutcome.failed("x", rate_limited=True))
        sleeps.clear()
        await pacer.after(WorkerOutcome.ok("a"))   # success 1: sleep 40
        await pacer.after(WorkerOutcome.ok("b"))   # success 2: sleep 40
        await pacer.after(WorkerOutcome.ok("c"))   # success 3: decrease THEN sleep 40/1.5
    finally:
        asyncio.sleep = real  # type: ignore[assignment]
    assert sleeps[0] == 40.0
    assert sleeps[1] == 40.0
    assert sleeps[2] == pytest.approx(40 / 1.5, rel=0.001)


async def test_pacer_aimd_floors_at_base_min() -> None:
    """Many successes shouldn't push current_base below base_min."""
    sleeps: list[float] = []

    async def fake_sleep(t: float) -> None:
        sleeps.append(t)

    real = asyncio.sleep
    asyncio.sleep = fake_sleep  # type: ignore[assignment]
    try:
        pacer = Pacer(base_sleep_sec=60.0, rate_limit_sleep_sec=300.0, base_min=5.0)
        for _ in range(30):  # plenty of successes to drive decrease cycles
            await pacer.after(WorkerOutcome.ok("x"))
    finally:
        asyncio.sleep = real  # type: ignore[assignment]
    # current_base started at base_min=5 and only decreases trigger AFTER an
    # increase from a 429 — so all sleeps stay at 5.
    assert all(s == 5.0 for s in sleeps), f"saw a sleep below base_min: {sleeps}"


async def test_pacer_skipped_row_is_no_op() -> None:
    """outcome=None means the row was already migrated; no API call hit the
    server, so the Pacer should neither sleep nor mutate state."""
    sleeps: list[float] = []

    async def fake_sleep(t: float) -> None:
        sleeps.append(t)

    real = asyncio.sleep
    asyncio.sleep = fake_sleep  # type: ignore[assignment]
    try:
        pacer = Pacer(base_sleep_sec=30.0, rate_limit_sleep_sec=300.0)
        await pacer.after(None)
        await pacer.after(None)
    finally:
        asyncio.sleep = real  # type: ignore[assignment]
    assert sleeps == []
