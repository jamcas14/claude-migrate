"""Tests for the per-row runner and the serial-loop pacer."""

from __future__ import annotations

import asyncio
import sqlite3

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


async def test_pacer_skips_sleep_when_base_is_zero() -> None:
    sleeps: list[float] = []

    async def fake_sleep(t: float) -> None:
        sleeps.append(t)

    real = asyncio.sleep
    asyncio.sleep = fake_sleep  # type: ignore[assignment]
    try:
        pacer = Pacer(base_sleep_sec=0.0, rate_limit_sleep_sec=300.0)
        await pacer.after(WorkerOutcome.ok("x"))
    finally:
        asyncio.sleep = real  # type: ignore[assignment]
    assert sleeps == []


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


async def test_pacer_cooldown_grows_with_consecutive_429s() -> None:
    """Consecutive 429s extend the pause window: 100 → 200 → 400 → 400 (cap)."""
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
        pacer = Pacer(base_sleep_sec=0.0, rate_limit_sleep_sec=100.0, max_cooldown_sec=400.0)
        for _ in range(5):
            await pacer.after(WorkerOutcome.failed("429", rate_limited=True))
            await pacer.before()
    finally:
        asyncio.sleep = real_sleep  # type: ignore[assignment]
        asyncio.get_running_loop = real_get_loop  # type: ignore[assignment]
    # Each before() sleeps for the *remaining* pause; with no real time passing
    # except via fake_sleep, after the 1st 429 pause_until = 1000+100 = 1100,
    # before() sleeps 100, fake_now=1100. After 2nd 429 (consecutive=2),
    # pause_until = 1100+200 = 1300, before() sleeps 200. Etc.
    assert pause_seen == [100.0, 200.0, 400.0, 400.0, 400.0]


async def test_pacer_success_resets_cooldown_counter() -> None:
    """A successful outcome resets the consecutive-429 counter."""
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
        pacer = Pacer(base_sleep_sec=0.0, rate_limit_sleep_sec=100.0)
        await pacer.after(WorkerOutcome.failed("429", rate_limited=True))
        await pacer.before()  # sleeps 100
        await pacer.after(WorkerOutcome.ok("x"))  # resets counter (no sleep — base=0)
        await pacer.after(WorkerOutcome.failed("429", rate_limited=True))
        await pacer.before()  # sleeps 100 again (fresh, not 200)
    finally:
        asyncio.sleep = real_sleep  # type: ignore[assignment]
        asyncio.get_running_loop = real_get_loop  # type: ignore[assignment]
    assert pause_seen == [100.0, 100.0]


async def test_pacer_skipped_row_is_no_op() -> None:
    """outcome=None means the row was already migrated; no API call hit the
    server, so the Pacer should neither sleep nor change its cooldown state."""
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
        pacer = Pacer(base_sleep_sec=2.0, rate_limit_sleep_sec=300.0)
        # Build up a cooldown counter, skip a row, then 429 again — the skip
        # should be invisible: the next 429 escalates as if the skip never happened.
        await pacer.after(WorkerOutcome.failed("429", rate_limited=True))  # consecutive=1
        await pacer.before()                                               # sleep 300
        await pacer.after(None)                                            # no-op
        await pacer.after(WorkerOutcome.failed("429", rate_limited=True))  # consecutive=2
        await pacer.before()                                               # sleep 600
    finally:
        asyncio.sleep = real_sleep  # type: ignore[assignment]
        asyncio.get_running_loop = real_get_loop  # type: ignore[assignment]
    assert pause_seen == [300.0, 600.0]
