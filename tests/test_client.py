"""Tests for the HTTP layer's cookie/header construction + status mapping."""

from __future__ import annotations

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
