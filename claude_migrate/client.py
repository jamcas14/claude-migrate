"""Single HTTP layer wrapping curl_cffi.AsyncSession with auth, retry, rate cap."""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast

import structlog
from curl_cffi.requests import AsyncSession
from curl_cffi.requests.errors import RequestsError

from .config import BASE_URL, IMPERSONATE, Settings
from .errors import (
    AuthExpired,
    ClientVersionStale,
    CloudflareChallenge,
    EndpointChanged,
    NetworkError,
    RateLimited,
    TLSReject,
)

log = structlog.get_logger(__name__)

# 429 backoff schedule: 2 → 4 → 8 → 16 → 32 → 60s (cap), max 3 retries.
BACKOFF_SCHEDULE = (2, 4, 8, 16, 32, 60)
MAX_RETRIES = 3


@dataclass(frozen=True)
class Credentials:
    """Cookies + the discovered org context, materialized just before requests."""

    session_key: str
    cf_clearance: str
    org_uuid: str | None = None
    email: str | None = None


class ClaudeClient:
    """Thin async wrapper around curl_cffi. Single chokepoint for all HTTP."""

    def __init__(
        self,
        creds: Credentials,
        settings: Settings,
        *,
        concurrency: int | None = None,
    ) -> None:
        self.creds = creds
        self.settings = settings
        # `concurrency` parameter wins if explicitly passed; otherwise read
        # from settings so users can tune it via env var or config.toml.
        self._sem = asyncio.Semaphore(concurrency or settings.concurrency)
        self._session: Any = None
        # Init lock — without this, two concurrent first calls to session()
        # both see _session is None, both create AsyncSession, the second
        # clobbers the first and the first's connection pool is leaked.
        self._init_lock = asyncio.Lock()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[Any]:
        if self._session is None:
            async with self._init_lock:
                if self._session is None:  # double-checked under lock
                    self._session = AsyncSession(impersonate=cast(Any, IMPERSONATE))
        yield self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _cookie_header(self) -> str:
        parts = [f"sessionKey={self.creds.session_key}", f"cf_clearance={self.creds.cf_clearance}"]
        if self.creds.org_uuid:
            parts.append(f"lastActiveOrg={self.creds.org_uuid}")
        return "; ".join(parts)

    def _headers(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        h = {
            "User-Agent": self.settings.user_agent,
            "anthropic-client-version": self.settings.client_version,
            "anthropic-client-platform": "web_claude_ai",
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/",
            "Cookie": self._cookie_header(),
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
        }
        # Optional fingerprint headers — send only when configured. Sending bogus
        # values would be worse than omitting these.
        if self.settings.client_sha:
            h["anthropic-client-sha"] = self.settings.client_sha
        if self.settings.anonymous_id:
            h["anthropic-anonymous-id"] = self.settings.anonymous_id
        if self.settings.device_id:
            h["anthropic-device-id"] = self.settings.device_id
        if extra:
            h.update(extra)
        return h

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = 30.0,
        expect_json: bool = True,
    ) -> Any:
        """Issue one HTTP call with retry, backoff, and typed-error mapping.

        The semaphore (`self._sem`) is acquired only for the actual HTTP send,
        not the retry-backoff sleep. Holding it across `await asyncio.sleep`
        would let one rate-limited worker freeze every other in-flight request
        for up to 60s.
        """
        url = path if path.startswith("http") else f"{self.settings.base_url}{path}"
        attempt = 0
        while True:
            attempt += 1
            # Acquire only for the network roundtrip, then drop before any
            # backoff sleep. `_sem` caps concurrent connections; nothing more.
            async with self._sem, self.session() as sess:
                try:
                    resp = await sess.request(
                        cast(Any, method),
                        url,
                        params=cast(Any, params),
                        json=json_body,
                        headers=self._headers(headers),
                        timeout=timeout,
                    )
                except RequestsError as e:
                    raise NetworkError(f"{method} {url}: {e}") from e

            status = resp.status_code
            body = resp.content or b""
            text_preview = body[:4096].decode("utf-8", errors="replace")
            log.debug(
                "http", method=method, path=path, status=status, attempt=attempt
            )

            if 200 <= status < 300:
                if status == 204 or not body:
                    return None if expect_json else b""
                if not expect_json:
                    return body
                try:
                    return json.loads(body)
                except json.JSONDecodeError as e:
                    if "Just a moment" in text_preview or "cf-mitigated" in text_preview:
                        raise CloudflareChallenge(
                            f"Cloudflare interstitial leaked through {status}"
                        ) from e
                    raise
            if status == 401:
                raise AuthExpired(f"{method} {path} returned 401")
            if status == 403:
                if (
                    "Just a moment" in text_preview
                    or "cf-mitigated" in text_preview
                    or "challenge-platform" in text_preview
                ):
                    raise CloudflareChallenge(
                        "Cloudflare challenged the request — refresh cf_clearance"
                    )
                raise TLSReject("403 with no Cloudflare body — TLS fingerprint reject")
            if status == 404:
                raise EndpointChanged(f"{method} {path} returned 404")
            if status in (400, 422):
                # Distinguish API validation errors (JSON body with a message)
                # from fingerprint rejections (HTML / generic Cloudflare body).
                api_msg = _extract_api_error(body)
                if api_msg:
                    raise NetworkError(
                        f"{method} {path} → {status}: {api_msg}"
                    )
                raise ClientVersionStale(
                    f"{method} {path} returned {status}. The most likely cause is a "
                    "stale or missing `anthropic-client-version` / `anthropic-client-sha` header."
                )
            if status == 429:
                if attempt > MAX_RETRIES:
                    raise RateLimited(f"429 after {MAX_RETRIES} retries")
                base = BACKOFF_SCHEDULE[min(attempt - 1, len(BACKOFF_SCHEDULE) - 1)]
                delay = base + random.uniform(0, 0.5)
                log.warning("rate_limited", path=path, retry_in_sec=delay)
                await asyncio.sleep(delay)  # outside _sem — see docstring
                continue
            if 500 <= status < 600:
                if attempt > MAX_RETRIES:
                    raise NetworkError(f"{method} {path} → {status} after retries")
                base = BACKOFF_SCHEDULE[min(attempt - 1, len(BACKOFF_SCHEDULE) - 1)]
                delay = base + random.uniform(0, 0.5)
                log.warning("server_error_retry",
                            path=path, status=status, retry_in_sec=delay)
                await asyncio.sleep(delay)  # outside _sem
                continue
            raise NetworkError(f"unexpected status {status} for {method} {path}: "
                               f"{text_preview[:200]}")

    async def get_json(self, path: str, **kw: Any) -> Any:
        return await self.request("GET", path, **kw)

    async def post_json(self, path: str, body: Any | None = None, **kw: Any) -> Any:
        return await self.request("POST", path, json_body=body, **kw)

    async def put_json(self, path: str, body: Any | None = None, **kw: Any) -> Any:
        return await self.request("PUT", path, json_body=body, **kw)

    async def patch_json(self, path: str, body: Any | None = None, **kw: Any) -> Any:
        return await self.request("PATCH", path, json_body=body, **kw)

    async def stream(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = 120.0,
    ) -> AsyncIterator[str]:
        """SSE-style streaming for /completion. Yields raw event lines.

        Concurrency: holds the client semaphore for the duration of the
        stream, matching the cap that `request()` enforces. Without this,
        `/completion` (the most expensive endpoint) would silently bypass
        the 5-way concurrency cap.

        Retry: on a 429/5xx *handshake* response (before any body bytes have
        been yielded), follows the same backoff schedule as `request()`.
        Mid-stream failures are not retried — partial SSE events can't be
        replayed safely.
        """
        url = path if path.startswith("http") else f"{self.settings.base_url}{path}"
        merged = dict(headers or {})
        merged.setdefault("Accept", "text/event-stream")
        attempt = 0
        while True:
            attempt += 1
            retry_after: float | None = None
            # Acquire _sem only for this attempt. On 429/5xx retry, we drop
            # the sem before sleeping — otherwise one rate-limited stream
            # would freeze 1/N of the concurrency budget for up to 60s.
            async with self._sem, self.session() as sess:
                try:
                    stream_ctx = sess.stream(
                        cast(Any, method),
                        url,
                        json=json_body,
                        headers=self._headers(merged),
                        timeout=timeout,
                    )
                except RequestsError as e:
                    raise NetworkError(f"stream {url}: {e}") from e
                async with stream_ctx as resp:
                    status = resp.status_code
                    if status == 401:
                        raise AuthExpired(f"{path} 401")
                    if status == 403:
                        preview = await _read_body_preview(resp, max_bytes=400)
                        if (
                            "Just a moment" in preview
                            or "cf-mitigated" in preview
                            or "challenge-platform" in preview
                        ):
                            raise CloudflareChallenge(f"{path} 403 (Cloudflare)")
                        raise TLSReject(f"{path} 403 (TLS fingerprint)")
                    if status == 429:
                        if attempt > MAX_RETRIES:
                            raise RateLimited(
                                f"stream {path} 429 after {MAX_RETRIES} retries"
                            )
                        base = BACKOFF_SCHEDULE[
                            min(attempt - 1, len(BACKOFF_SCHEDULE) - 1)
                        ]
                        retry_after = base + random.uniform(0, 0.5)
                        log.warning(
                            "stream_rate_limited", path=path,
                            retry_in_sec=retry_after,
                        )
                    elif 500 <= status < 600:
                        if attempt > MAX_RETRIES:
                            raise NetworkError(
                                f"stream {path} {status} after retries"
                            )
                        base = BACKOFF_SCHEDULE[
                            min(attempt - 1, len(BACKOFF_SCHEDULE) - 1)
                        ]
                        retry_after = base + random.uniform(0, 0.5)
                        log.warning(
                            "stream_5xx_retry", path=path, status=status,
                            retry_in_sec=retry_after,
                        )
                    elif not (200 <= status < 300):
                        preview = await _read_body_preview(resp, max_bytes=400)
                        raise NetworkError(
                            f"stream {path} status {status}: {preview[:400]}"
                        )
                    else:
                        # Happy path: yield each line. We yield while still
                        # inside _sem and the response context — that's
                        # intentional, the cap applies to the full lifetime
                        # of an in-flight stream.
                        try:
                            async for line in resp.aiter_lines():
                                if not line:
                                    continue
                                if isinstance(line, bytes):
                                    line = line.decode("utf-8", errors="replace")
                                yield line
                        except RequestsError as e:
                            raise NetworkError(f"stream {url}: {e}") from e
                        return  # stream completed
            # Both contexts have exited; _sem and the session ctx are released.
            # `retry_after` is set iff we took a retry branch above; the other
            # branches either returned (happy) or raised.
            assert retry_after is not None
            await asyncio.sleep(retry_after)


async def _read_body_preview(resp: Any, *, max_bytes: int = 400) -> str:
    """Best-effort read of an in-flight error response for diagnostic preview."""
    parts: list[str] = []
    total = 0
    try:
        async for blob in resp.aiter_content():
            chunk = blob.decode("utf-8", errors="replace")
            parts.append(chunk)
            total += len(chunk)
            if total >= max_bytes:
                break
    except (RequestsError, UnicodeDecodeError):
        pass
    return "".join(parts)[:max_bytes]


def _extract_api_error(body: bytes) -> str | None:
    """Return a human-readable API error message if `body` is JSON-shaped.

    claude.ai's API returns JSON like {"error": {"message": "..."}} or
    {"detail": "..."} for validation errors. Cloudflare/bot-detection
    rejections return HTML or a non-JSON body — we don't extract from those.
    """
    if not body:
        return None
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    err = data.get("error")
    if isinstance(err, dict):
        msg = err.get("message") or err.get("type") or err.get("code")
        if isinstance(msg, str):
            return msg
    if isinstance(err, str):
        return err
    detail = data.get("detail") or data.get("message")
    if isinstance(detail, str):
        return detail
    return None
