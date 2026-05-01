"""Tests for the HTTP layer's cookie/header construction + status mapping."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from claude_migrate.client import ClaudeClient, Credentials, _extract_api_error
from claude_migrate.config import load_settings
from claude_migrate.errors import (
    AuthExpired,
    ClientVersionStale,
    CloudflareChallenge,
    EndpointChanged,
    NetworkError,
    TLSReject,
)


def _client() -> ClaudeClient:
    return ClaudeClient(
        Credentials(session_key="sk-ant-sid01-AAA", cf_clearance="cfABCDEF"),
        load_settings(),
    )


# ---------------------------------------------------------------------------
# Credentials repr — must NOT leak secrets.
# ---------------------------------------------------------------------------


def test_credentials_repr_redacts_session_key_and_cf_clearance() -> None:
    """An accidental log.info(creds=...) would otherwise leak the cookies."""
    creds = Credentials(
        session_key="sk-ant-sid01-VERY-SECRET-DO-NOT-LOG",
        cf_clearance="cf-clearance-also-secret-12345",
        email="user@example.com",
    )
    text = repr(creds)
    # Length fingerprints are OK; raw values must not appear.
    assert "VERY-SECRET" not in text
    assert "cf-clearance-also-secret" not in text
    # Sanity: the redacted shape is debuggable.
    assert "session_key=<" in text
    assert "chars>" in text
    assert "cf_clearance=<" in text
    # Non-secret fields still render.
    assert "user@example.com" in text


def test_credentials_str_also_redacted() -> None:
    """str() falls through to __repr__; same protection."""
    creds = Credentials(
        session_key="sk-ant-sid01-LEAKY",
        cf_clearance="cf-LEAKY",
    )
    assert "LEAKY" not in str(creds)


def test_cookie_header_basic() -> None:
    c = _client()
    h = c._cookie_header()
    assert "sessionKey=sk-ant-sid01-AAA" in h
    assert "cf_clearance=cfABCDEF" in h
    assert "lastActiveOrg" not in h


def test_cookie_header_includes_org_when_known() -> None:
    c = ClaudeClient(
        Credentials(
            session_key="sk-ant-sid01-AAA",
            cf_clearance="cfABCDEF",
            org_uuid="org-1",
        ),
        load_settings(),
    )
    assert "lastActiveOrg=org-1" in c._cookie_header()


def test_headers_contain_required_origin_and_ua() -> None:
    h = _client()._headers()
    assert h["Origin"] == "https://claude.ai"
    assert h["Referer"] == "https://claude.ai/"
    assert h["User-Agent"].startswith("Mozilla/5.0")
    assert "anthropic-client-version" in h


def test_headers_extra_overrides_merge() -> None:
    h = _client()._headers({"Accept": "text/event-stream"})
    assert h["Accept"] == "text/event-stream"


def test_optional_fingerprint_headers_omitted_when_unset() -> None:
    settings = load_settings()
    settings.client_sha = None
    settings.anonymous_id = None
    settings.device_id = None
    c = ClaudeClient(
        Credentials(session_key="sk-ant-sid01-AAA", cf_clearance="cfABCDEF"),
        settings,
    )
    h = c._headers()
    assert "anthropic-client-sha" not in h
    assert "anthropic-anonymous-id" not in h
    assert "anthropic-device-id" not in h


def test_optional_fingerprint_headers_emitted_when_set() -> None:
    settings = load_settings()
    settings.client_sha = "efac08e6600202fce1b38c7c5b5bcb27e8b917c5"
    settings.anonymous_id = "claudeai.v1.abc-uuid"
    settings.device_id = "device-uuid"
    c = ClaudeClient(
        Credentials(session_key="sk-ant-sid01-AAA", cf_clearance="cfABCDEF"),
        settings,
    )
    h = c._headers()
    assert h["anthropic-client-sha"] == "efac08e6600202fce1b38c7c5b5bcb27e8b917c5"
    assert h["anthropic-anonymous-id"] == "claudeai.v1.abc-uuid"
    assert h["anthropic-device-id"] == "device-uuid"


# ---------------------------------------------------------------------------
# Status code → typed-error mapping (no real network — fake the session)
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status: int, body: bytes = b"") -> None:
        self.status_code = status
        self.content = body


class _FakeSession:
    def __init__(self, *responses: _FakeResp) -> None:
        self._responses = list(responses)
        self.calls: list[Any] = []

    async def request(self, method: Any, url: Any, **kw: Any) -> _FakeResp:
        self.calls.append((method, url, kw))
        return self._responses.pop(0)

    async def close(self) -> None:
        return None


def _with_fake(*responses: _FakeResp) -> ClaudeClient:
    c = _client()
    c._session = _FakeSession(*responses)
    return c


@pytest.mark.parametrize("status", [400, 422])
async def test_400_or_422_with_html_body_raises_client_version_stale(status: int) -> None:
    """Non-JSON body suggests a fingerprint rejection at Cloudflare or similar."""
    c = _with_fake(_FakeResp(status, b"<html>blocked</html>"))
    with pytest.raises(ClientVersionStale):
        await c.get_json("/api/bootstrap")


@pytest.mark.parametrize("status", [400, 422])
async def test_400_or_422_with_json_error_raises_network_error(status: int) -> None:
    """API validation error → surface the message, don't blame client_version."""
    body = b'{"error": {"message": "field full_name is required"}}'
    c = _with_fake(_FakeResp(status, body))
    with pytest.raises(NetworkError, match="full_name is required"):
        await c.get_json("/api/account")


def test_extract_api_error_nested() -> None:
    body = b'{"error": {"message": "boom", "code": "x"}}'
    assert _extract_api_error(body) == "boom"


def test_extract_api_error_detail_field() -> None:
    body = b'{"detail": "missing"}'
    assert _extract_api_error(body) == "missing"


def test_extract_api_error_string_error() -> None:
    body = b'{"error": "kaboom"}'
    assert _extract_api_error(body) == "kaboom"


def test_extract_api_error_html_returns_none() -> None:
    assert _extract_api_error(b"<html>blocked</html>") is None


def test_extract_api_error_empty_returns_none() -> None:
    assert _extract_api_error(b"") is None


async def test_401_raises_auth_expired() -> None:
    c = _with_fake(_FakeResp(401))
    with pytest.raises(AuthExpired):
        await c.get_json("/api/bootstrap")


async def test_403_with_cf_marker_raises_cloudflare() -> None:
    c = _with_fake(_FakeResp(403, b"<title>Just a moment...</title>"))
    with pytest.raises(CloudflareChallenge):
        await c.get_json("/api/bootstrap")


async def test_403_without_cf_marker_raises_tls_reject() -> None:
    c = _with_fake(_FakeResp(403, b"forbidden"))
    with pytest.raises(TLSReject):
        await c.get_json("/api/bootstrap")


async def test_404_raises_endpoint_changed() -> None:
    c = _with_fake(_FakeResp(404))
    with pytest.raises(EndpointChanged):
        await c.get_json("/api/bootstrap")


@pytest.mark.parametrize("status", [200, 201, 202])
async def test_2xx_returns_decoded_json(status: int) -> None:
    """Regression: 201 from POST /chat_conversations was being treated as failure."""
    c = _with_fake(_FakeResp(status, b'{"uuid":"new-conv-uuid"}'))
    result = await c.post_json("/api/organizations/x/chat_conversations", body={})
    assert result == {"uuid": "new-conv-uuid"}


async def test_204_returns_none() -> None:
    c = _with_fake(_FakeResp(204))
    result = await c.request("DELETE", "/api/organizations/x/chat_conversations/y")
    assert result is None


async def test_201_with_empty_body_does_not_crash() -> None:
    c = _with_fake(_FakeResp(201, b""))
    result = await c.post_json("/api/organizations/x/projects", body={})
    assert result is None


async def test_5xx_uses_capped_backoff_schedule() -> None:
    """Regression: 5xx backoff must use BACKOFF_SCHEDULE (capped at 60s),
    not unbounded 2**attempt. Verify by simulating sleep durations."""
    import claude_migrate.client as cm

    sleep_calls: list[float] = []

    async def fake_sleep(t: float) -> None:
        sleep_calls.append(t)

    real_sleep = cm.asyncio.sleep
    cm.asyncio.sleep = fake_sleep  # type: ignore[assignment]
    try:
        c = _with_fake(
            _FakeResp(500, b""), _FakeResp(500, b""),
            _FakeResp(500, b""), _FakeResp(500, b""),
        )
        with pytest.raises(NetworkError):
            await c.get_json("/api/foo")
    finally:
        cm.asyncio.sleep = real_sleep  # type: ignore[assignment]
    # Should never sleep more than the schedule top (60s + jitter < 61).
    assert all(t < 61 for t in sleep_calls), f"saw uncapped sleep: {sleep_calls}"
    # Should be at least 2s (start of schedule).
    assert any(t >= 2 for t in sleep_calls)


# ---------------------------------------------------------------------------
# stream() concurrency cap + retry — used by the conversation restore path.
# ---------------------------------------------------------------------------


class _FakeStreamResp:
    """Async-context-managed response for sess.stream(...). Yields one
    `data: ...` line so the happy-path consumer sees a terminating event."""

    def __init__(
        self, status: int, body_lines: list[bytes] | None = None,
    ) -> None:
        self.status_code = status
        self._body_lines = body_lines or [b'data: {"stop_reason": "end_turn"}']

    async def __aenter__(self) -> _FakeStreamResp:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def aiter_lines(self) -> Any:
        for ln in self._body_lines:
            yield ln

    async def aiter_content(self) -> Any:
        for ln in self._body_lines:
            yield ln


class _FakeStreamSession:
    """Records each stream call. Pops the next prepared response."""

    def __init__(self, *resps: _FakeStreamResp) -> None:
        self._resps = list(resps)
        self.stream_call_count = 0

    def stream(self, method: Any, url: Any, **kw: Any) -> _FakeStreamResp:
        self.stream_call_count += 1
        return self._resps.pop(0)

    async def close(self) -> None:
        return None


async def test_stream_429_raises_immediately_no_inner_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stream() raises RateLimited on the FIRST 429 without inner retry.

    The conversation-restore loop already retries via its outer Pacer with
    a proper cooldown. Inner retry here just burned ~14s of wasted sleep
    before the real cooldown — actively slowing the migration on every
    rate-limited chat. The fix dropped the inner retry on 429 (kept for 5xx).
    """
    import claude_migrate.client as cm
    from claude_migrate.errors import RateLimited

    sleeps: list[float] = []
    async def fake_sleep(t: float) -> None: sleeps.append(t)
    monkeypatch.setattr(cm.asyncio, "sleep", fake_sleep)
    c = _client()
    c._session = _FakeStreamSession(_FakeStreamResp(429))
    with pytest.raises(RateLimited):
        async for _ in c.stream("POST", "/api/x"):
            pass
    # No inner retry, so no sleep before the raise.
    assert sleeps == []
    # The session.stream call fired exactly once.
    assert c._session.stream_call_count == 1


async def test_stream_5xx_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """5xx handshake response must retry on the same schedule."""
    import claude_migrate.client as cm

    sleeps: list[float] = []
    async def fake_sleep(t: float) -> None: sleeps.append(t)
    monkeypatch.setattr(cm.asyncio, "sleep", fake_sleep)
    c = _client()
    c._session = _FakeStreamSession(
        _FakeStreamResp(503), _FakeStreamResp(200),
    )
    async for _ in c.stream("POST", "/api/x"):
        pass
    assert len(sleeps) == 1


async def test_stream_403_with_cf_marker_raises_cloudflare() -> None:
    """403 + Cloudflare body → CloudflareChallenge (not TLSReject)."""
    c = _client()
    c._session = _FakeStreamSession(
        _FakeStreamResp(403, body_lines=[b"<title>Just a moment...</title>"]),
    )
    with pytest.raises(CloudflareChallenge):
        async for _ in c.stream("POST", "/api/x"):
            pass


async def test_stream_403_without_cf_marker_raises_tls_reject() -> None:
    """403 with no CF marker → TLSReject (was always RaiseCloudflareChallenge)."""
    c = _client()
    c._session = _FakeStreamSession(
        _FakeStreamResp(403, body_lines=[b"forbidden"]),
    )
    with pytest.raises(TLSReject):
        async for _ in c.stream("POST", "/api/x"):
            pass


async def test_stream_holds_semaphore_for_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole stream lifetime is gated by self._sem so /completion can't
    silently bypass the concurrency cap."""
    c = _client()
    c._session = _FakeStreamSession(_FakeStreamResp(200))
    # Drain the semaphore so the stream blocks if it tries to acquire.
    for _ in range(c._sem._value):
        await c._sem.acquire()
    try:
        # Schedule the stream — it must wait, not proceed.
        async def consume() -> list[str]:
            return [ln async for ln in c.stream("POST", "/api/x")]

        task = asyncio.create_task(consume())
        # Give the event loop time to attempt the acquire.
        await asyncio.sleep(0)
        assert not task.done(), "stream proceeded without acquiring _sem"
    finally:
        c._sem.release()
        # Now the stream can complete.
        await task
        # Re-fill the semaphore for hygiene.
        for _ in range(c._sem._value, c._sem._value):
            pass


async def test_stream_releases_sem_during_5xx_retry_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirroring request(): the 5xx retry-backoff sleep must NOT hold the
    semaphore. Otherwise one transient 503 freezes 1/N of the cap for up
    to 60s. (429 no longer retries inside stream — that's the conversation
    worker's outer-Pacer responsibility.)"""
    import claude_migrate.client as cm

    sem_levels: list[int] = []

    async def fake_sleep(t: float) -> None:
        # Snapshot _sem._value mid-sleep — should equal initial (released).
        sem_levels.append(c._sem._value)

    monkeypatch.setattr(cm.asyncio, "sleep", fake_sleep)
    c = _client()
    initial = c._sem._value
    c._session = _FakeStreamSession(
        _FakeStreamResp(503), _FakeStreamResp(200),
    )
    async for _ in c.stream("POST", "/api/x"):
        pass
    # During the one retry sleep, _sem was fully released.
    assert sem_levels == [initial], (
        f"_sem was held during retry sleep (saw level {sem_levels})"
    )


# ---------------------------------------------------------------------------
# Retry-After header parsing.
# ---------------------------------------------------------------------------


def test_parse_retry_after_integer_seconds() -> None:
    from claude_migrate.client import _parse_retry_after
    assert _parse_retry_after("120") == 120.0


def test_parse_retry_after_clamps_too_large() -> None:
    """Misconfigured edge sending Retry-After: 86400 must not wedge the run."""
    from claude_migrate.client import _RETRY_AFTER_MAX, _parse_retry_after
    assert _parse_retry_after("86400") == _RETRY_AFTER_MAX


def test_parse_retry_after_clamps_too_small() -> None:
    """Server replying 0 or 1 should still floor to a sane minimum."""
    from claude_migrate.client import _RETRY_AFTER_MIN, _parse_retry_after
    assert _parse_retry_after("0") == _RETRY_AFTER_MIN
    assert _parse_retry_after("1") == _RETRY_AFTER_MIN


def test_parse_retry_after_http_date() -> None:
    """Form 2 of RFC 7231 — HTTP-date in the future."""
    # Use a date ~5 minutes in the future. The exact seconds will vary by
    # clock skew but should be in the 100-600s clamp.
    import email.utils
    from datetime import UTC, datetime, timedelta

    from claude_migrate.client import _parse_retry_after
    future = datetime.now(UTC) + timedelta(minutes=5)
    header = email.utils.format_datetime(future)
    parsed = _parse_retry_after(header)
    assert parsed is not None
    assert 100 <= parsed <= 600


def test_parse_retry_after_garbage() -> None:
    from claude_migrate.client import _parse_retry_after
    assert _parse_retry_after(None) is None
    assert _parse_retry_after("") is None
    assert _parse_retry_after("not-a-thing") is None


async def test_429_carries_retry_after_to_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 429 response with Retry-After populates RateLimited.retry_after_sec
    so the orchestrator's Pacer can use it as the cooldown floor."""
    import claude_migrate.client as cm
    from claude_migrate.errors import RateLimited

    async def fake_sleep(t: float) -> None: ...
    monkeypatch.setattr(cm.asyncio, "sleep", fake_sleep)

    class _RespWithHeaders:
        def __init__(self, status: int, retry_after: str) -> None:
            self.status_code = status
            self.content = b""
            self.headers = {"Retry-After": retry_after}

    class _SessWithHeaders:
        def __init__(self) -> None:
            self.calls = 0

        async def request(self, method: Any, url: Any, **kw: Any) -> Any:
            self.calls += 1
            return _RespWithHeaders(429, "45")

        async def close(self) -> None: ...

    c = _client()
    c._session = _SessWithHeaders()
    with pytest.raises(RateLimited) as exc_info:
        await c.get_json("/api/x")
    assert exc_info.value.retry_after_sec == 45.0


async def test_session_init_is_lock_protected_under_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two concurrent first calls to client.session() must NOT both create
    a fresh AsyncSession (the second would clobber the first, leaking it)."""
    creation_count = 0

    class _Sentinel:
        async def close(self) -> None: ...

    def fake_async_session(impersonate: object) -> _Sentinel:
        nonlocal creation_count
        creation_count += 1
        return _Sentinel()

    import claude_migrate.client as cm

    monkeypatch.setattr(cm, "AsyncSession", fake_async_session)
    c = _client()

    async def acquire_session() -> object:
        async with c.session() as s:
            return s

    s1, s2 = await asyncio.gather(acquire_session(), acquire_session())
    assert s1 is s2, "concurrent first-calls produced two different sessions"
    assert creation_count == 1, f"AsyncSession instantiated {creation_count} times (expected 1)"
