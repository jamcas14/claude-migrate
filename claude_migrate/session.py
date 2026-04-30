"""Authenticated, org-bound client lifecycle.

`open_session(profile_name)` is the one entry point every command should use:
it loads the stored profile, instantiates the HTTP client, discovers the org,
binds the org_uuid into the client's credentials so subsequent /api/* calls
carry the right `lastActiveOrg` cookie, yields a `BoundSession`, and closes
the client on exit.

This collapses the six-line `load_profile → ClaudeClient → discover_org →
finally close` incantation that used to live at every call site, and removes
the surprising side effect that `discover_org` had of mutating the client.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace

from .auth import load_profile
from .client import ClaudeClient
from .config import Settings, load_settings
from .discover import discover_org


@dataclass(frozen=True)
class BoundSession:
    """A claude-migrate session bound to one profile + one org.

    `client` already carries the discovered org_uuid in its credentials, so
    callers can issue /api/organizations/{org_uuid}/... requests directly.
    """

    client: ClaudeClient
    org_uuid: str
    org_name: str | None
    email: str | None


@asynccontextmanager
async def open_session(
    profile_name: str,
    *,
    settings: Settings | None = None,
) -> AsyncIterator[BoundSession]:
    """Yield a BoundSession for `profile_name`. Closes the client on exit."""
    settings = settings or load_settings()
    profile = load_profile(profile_name)
    client = ClaudeClient(profile.as_credentials(), settings)
    try:
        org_uuid, org_name, email = await discover_org(client)
        client.creds = replace(client.creds, org_uuid=org_uuid, email=email)
        yield BoundSession(
            client=client,
            org_uuid=org_uuid,
            org_name=org_name,
            email=email,
        )
    finally:
        await client.close()
