"""Single HTTP layer wrapping curl_cffi.AsyncSession with auth, retry, rate cap."""

from __future__ import annotations

import asyncio
import email.utils
import json
import random
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
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

# Retry-After header may be either integer seconds ("120") or HTTP-date.
# Clamp the parsed value so a misconfigured edge sending Retry-After:86400
# can't wedge the run for a day. Lower bound matches BACKOFF_SCHEDULE[0].
_RETRY_AFTER_MIN = 2.0
_RETRY_AFTER_MAX = 600.0


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a Retry-After header. Returns None if absent or unparseable.

    Accepts integer-seconds (RFC 7231 form 1) or HTTP-date (form 2). Clamps
    to [_RETRY_AFTER_MIN, _RETRY_AFTER_MAX] so a malformed/extreme value
    can't push the cooldown into "wait a day" territory.
    """
    if not value:
        return None
    s = value.strip()
    try:
        secs = float(int(s))  # form 1: integer seconds
    except ValueError:
        try:
            dt = email.utils.parsedate_to_datetime(s)
        except (TypeError, ValueError):
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        secs = (dt - datetime.now(UTC)).total_seconds()
    return max(_RETRY_AFTER_MIN, min(_RETRY_AFTER_MAX, secs))


@dataclass(frozen=True)
class Credentials:
    """Cookies + the discovered org context, materialized just before requests.

    `__repr__` redacts the secret fields so an accidental `log.info(creds=...)`
    or exception that includes the dataclass can't leak credentials to stderr,
    log files, or crash reports.
    """

    session_key: str = field(repr=False)
    cf_clearance: str = field(repr=False)
    org_uuid: str | None = None
    email: str | None = None

    def __repr__(self) -> str:
        # Show fingerprints, not full values, so debugging is still possible.
        return (
            f"Credentials(session_key=<{len(self.session_key)} chars>, "
            f"cf_clearance=<{len(self.cf_clearance)} chars>, "
            f"org_uuid={self.org_uuid!r}, email={self.email!r})"
        )


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
                            f"a Cloudflare interstitial leaked through with status {status}"
                        ) from e
                    raise
            if status == 401:
                raise AuthExpired(
                    f"claude.ai rejected the session cookie for {method} {path} (401)"
                )
            if status == 403:
                if (
                    "Just a moment" in text_preview
                    or "cf-mitigated" in text_preview
                    or "challenge-platform" in text_preview
                ):
                    raise CloudflareChallenge(
                        f"Cloudflare blocked {method} {path} (cf_clearance is "
                        "stale or the IP is being challenged)"
                    )
                raise TLSReject(
                    f"{method} {path} returned 403 without a Cloudflare challenge "
                    "(usually a stale session cookie; sometimes an outdated TLS "
                    "fingerprint)"
                )
            if status == 404:
                raise EndpointChanged(
                    f"claude.ai's {method} {path} endpoint returned 404 — moved or removed"
                )
            if status in (400, 422):
                # Distinguish API validation errors (JSON body with a message)
                # from fingerprint rejections (HTML / generic Cloudflare body).
                api_msg = _extract_api_error(body)
                if api_msg:
                    raise NetworkError(
                        f"claude.ai rejected {method} {path} with HTTP {status}: {api_msg}"
                    )
                raise ClientVersionStale(
                    f"{method} {path} returned {status}. Most common cause: a "
                    "stale or missing `anthropic-client-version` / "
                    "`anthropic-client-sha` request header — Anthropic rotates "
                    "these every few weeks."
                )
            if status == 429:
                # Empirical instrumentation: capture rate-limit signaling so
                # the orchestrator's Pacer (and any future tuning) can see
                # what claude.ai actually sends. Cheap; one log line.
                retry_after_hdr = _resp_header(resp, "Retry-After")
                retry_after_sec = _parse_retry_after(retry_after_hdr)
                log.info(
                    "rate_limit_observed",
                    path=path, attempt=attempt,
                    retry_after_header=retry_after_hdr,
                    retry_after_parsed_sec=retry_after_sec,
                    ratelimit_remaining=_resp_header(
                        resp, "anthropic-ratelimit-requests-remaining"
                    ),
                    ratelimit_reset=_resp_header(
                        resp, "anthropic-ratelimit-tokens-reset"
                    ),
                    body_preview=text_preview[:200],
                )
                if attempt > MAX_RETRIES:
                    raise RateLimited(
                        f"claude.ai returned 429 on {method} {path} after "
                        f"{MAX_RETRIES} retries",
                        retry_after_sec=retry_after_sec,
                    )
                # Prefer the server's Retry-After when present; fall back to
                # the capped exponential schedule otherwise.
                if retry_after_sec is not None:
                    delay = retry_after_sec + random.uniform(0, 0.5)
                else:
                    base = BACKOFF_SCHEDULE[min(attempt - 1, len(BACKOFF_SCHEDULE) - 1)]
                    delay = base + random.uniform(0, 0.5)
                log.warning("rate_limited", path=path, retry_in_sec=delay)
                await asyncio.sleep(delay)  # outside _sem — see docstring
                continue
            if 500 <= status < 600:
                if attempt > MAX_RETRIES:
                    raise NetworkError(
                        f"claude.ai returned {status} on {method} {path} after "
                        f"{MAX_RETRIES} retries (server error; usually transient)"
                    )
                base = BACKOFF_SCHEDULE[min(attempt - 1, len(BACKOFF_SCHEDULE) - 1)]
                delay = base + random.uniform(0, 0.5)
                log.warning("server_error_retry",
                            path=path, status=status, retry_in_sec=delay)
                await asyncio.sleep(delay)  # outside _sem
                continue
            raise NetworkError(
                f"claude.ai returned an unexpected HTTP {status} for "
                f"{method} {path}: {text_preview[:200]}"
            )

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
                    raise NetworkError(
                        f"could not open stream to {url}: {e}"
                    ) from e
                async with stream_ctx as resp:
                    status = resp.status_code
                    if status == 401:
                        raise AuthExpired(
                            f"claude.ai rejected the session cookie when opening "
                            f"the stream for {path} (401)"
                        )
                    if status == 403:
                        preview = await _read_body_preview(resp, max_bytes=400)
                        if (
                            "Just a moment" in preview
                            or "cf-mitigated" in preview
                            or "challenge-platform" in preview
                        ):
                            raise CloudflareChallenge(
                                f"Cloudflare blocked the stream for {path} "
                                "(cf_clearance is stale or the IP is being challenged)"
                            )
                        raise TLSReject(
                            f"stream {path} returned 403 without a Cloudflare "
                            "challenge (usually a stale session cookie; sometimes "
                            "an outdated TLS fingerprint)"
                        )
                    if status == 404:
                        raise EndpointChanged(
                            f"claude.ai's stream endpoint {path} returned 404 "
                            "— moved or removed"
                        )
                    if status in (400, 422):
                        preview = await _read_body_preview(resp, max_bytes=4096)
                        api_msg = _extract_api_error(preview.encode("utf-8"))
                        if api_msg:
                            raise NetworkError(
                                f"claude.ai rejected the stream for {path} with "
                                f"HTTP {status}: {api_msg}"
                            )
                        raise ClientVersionStale(
                            f"stream {path} returned {status}. Most common cause: "
                            "a stale or missing `anthropic-client-version` / "
                            "`anthropic-client-sha` request header — Anthropic "
                            "rotates these every few weeks."
                        )
                    if status == 429:
                        # Drop the inner-retry on 429: /completion is the only
                        # caller of stream(), and the conversation-restore
                        # loop already retries via its outer Pacer with proper
                        # cooldown. Inner retries here just burn 14s of sleep
                        # before the real cooldown kicks in. Surface the
                        # server's Retry-After so the Pacer can use it.
                        retry_after_hdr = _resp_header(resp, "Retry-After")
                        retry_after_sec = _parse_retry_after(retry_after_hdr)
                        log.info(
                            "rate_limit_observed",
                            path=path, attempt=attempt,
                            retry_after_header=retry_after_hdr,
                            retry_after_parsed_sec=retry_after_sec,
                            ratelimit_remaining=_resp_header(
                                resp, "anthropic-ratelimit-requests-remaining"
                            ),
                            ratelimit_reset=_resp_header(
                                resp, "anthropic-ratelimit-tokens-reset"
                            ),
                        )
                        raise RateLimited(
                            f"claude.ai rate-limited the stream for {path} (429)",
                            retry_after_sec=retry_after_sec,
                        )
                    if 500 <= status < 600:
                        if attempt > MAX_RETRIES:
                            raise NetworkError(
                                f"claude.ai returned {status} on the stream for "
                                f"{path} after {MAX_RETRIES} retries (server "
                                "error; usually transient)"
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
                            f"claude.ai returned an unexpected HTTP {status} on "
                            f"the stream for {path}: {preview[:400]}"
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
                            raise NetworkError(
                                f"stream {url} disconnected mid-response: {e}"
                            ) from e
                        return  # stream completed
            # Both contexts have exited; _sem and the session ctx are released.
            # `retry_after` is set iff we took a retry branch above; the other
            # branches either returned (happy) or raised.
            assert retry_after is not None
            await asyncio.sleep(retry_after)


def _resp_header(resp: Any, name: str) -> str | None:
    """Look up a response header by name, case-insensitive, defensively.

    curl_cffi exposes headers via `resp.headers` (a dict-like). Callers like
    `request()` and `stream()` need it; tests mock `resp` with a minimal
    interface that may not have `headers`. Return None if it's missing.
    """
    headers = getattr(resp, "headers", None)
    if headers is None:
        return None
    try:
        # Most dict-like header containers are case-insensitive.
        val = headers.get(name)
    except (AttributeError, TypeError):
        return None
    if isinstance(val, str):
        return val
    return None


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


def map_status_to_typed_error(
    status: int, body: bytes, *, method: str, path: str,
) -> None:
    """Translate a non-2xx HTTP status into the right typed exception.

    Centralizes the 401/403/404/400/422/429/5xx mapping so callers other
    than `request()` (e.g., the multipart upload in `transport`, future
    streaming fall-back paths) raise the same typed errors that the rest
    of the codebase's `except` clauses expect.

    Returns None for 2xx (the caller should keep going); raises one of the
    typed exception subclasses otherwise. Does NOT retry — that's the
    caller's responsibility, since retry semantics differ per call site.
    """
    if 200 <= status < 300:
        return
    text_preview = body[:4096].decode("utf-8", errors="replace")
    if status == 401:
        raise AuthExpired(
            f"claude.ai rejected the session cookie for {method} {path} (401)"
        )
    if status == 403:
        if (
            "Just a moment" in text_preview
            or "cf-mitigated" in text_preview
            or "challenge-platform" in text_preview
        ):
            raise CloudflareChallenge(
                f"Cloudflare blocked {method} {path} (cf_clearance is "
                "stale or the IP is being challenged)"
            )
        raise TLSReject(
            f"{method} {path} returned 403 without a Cloudflare challenge "
            "(usually a stale session cookie; sometimes an outdated TLS "
            "fingerprint)"
        )
    if status == 404:
        raise EndpointChanged(
            f"claude.ai's {method} {path} endpoint returned 404 — moved or removed"
        )
    if status in (400, 422):
        api_msg = _extract_api_error(body)
        if api_msg:
            raise NetworkError(
                f"claude.ai rejected {method} {path} with HTTP {status}: {api_msg}"
            )
        raise ClientVersionStale(
            f"{method} {path} returned {status}. Most common cause: a stale "
            "or missing `anthropic-client-version` / `anthropic-client-sha` "
            "request header — Anthropic rotates these every few weeks."
        )
    if status == 429:
        raise RateLimited(
            f"claude.ai rate-limited {method} {path} (429)"
        )
    if 500 <= status < 600:
        raise NetworkError(
            f"claude.ai returned {status} on {method} {path} (server error; "
            "usually transient)"
        )
    raise NetworkError(
        f"claude.ai returned an unexpected HTTP {status} for {method} {path}: "
        f"{text_preview[:200]}"
    )


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
