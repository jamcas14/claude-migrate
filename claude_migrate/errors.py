"""Typed exception hierarchy. All non-error catches must catch one of these."""

from __future__ import annotations


class ClaudeMigrateError(Exception):
    """Base class. Subclasses carry a stable string code for programmatic checks."""

    code: str = "claude_migrate_error"


class AuthError(ClaudeMigrateError):
    code = "auth_error"


class AuthInvalid(AuthError):
    """Cookie format failed validation before any network call."""

    code = "auth_invalid"


class AuthExpired(AuthError):
    """sessionKey was rejected with 401 by claude.ai."""

    code = "auth_expired"


class AuthMissing(AuthError):
    """No stored profile by that name."""

    code = "auth_missing"


class CloudflareChallenge(AuthError):
    """403 + Cloudflare interstitial; cf_clearance is stale or missing."""

    code = "cloudflare_challenge"


class TLSReject(AuthError):
    """403 with no Cloudflare body — TLS / JA3 fingerprint rejection."""

    code = "tls_reject"


class NetworkError(ClaudeMigrateError):
    """Connection / DNS / timeout."""

    code = "network_error"


class EndpointChanged(ClaudeMigrateError):
    """A documented endpoint returned 404."""

    code = "endpoint_changed"


class ClientVersionStale(ClaudeMigrateError):
    """A request returned 400/422 — the most likely cause is a stale or missing
    `anthropic-client-version` / `anthropic-client-sha` header. Refresh via
    `claude-migrate config edit`."""

    code = "client_version_stale"


class RateLimited(ClaudeMigrateError):
    """429 after exhausted backoff.

    `retry_after_sec` carries the server's `Retry-After` hint when present,
    so the orchestrator's Pacer can use it as the cooldown floor instead of
    a fixed schedule. Anthropic's documented API surface always sends this
    header on 429; the consumer claude.ai surface is undocumented but uses
    the same backend stack — capture it opportunistically.
    """

    code = "rate_limited"

    def __init__(self, message: str, *, retry_after_sec: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_sec = retry_after_sec


class SchemaDrift(ClaudeMigrateError):
    """Pydantic validation against an API response failed."""

    code = "schema_drift"


class KeyringUnavailable(ClaudeMigrateError):
    """OS secret store is not usable; fallback path needed."""

    code = "keyring_unavailable"
