"""Org and account discovery via /api/bootstrap, with /api/organizations fallback.

Pure read: returns the (org_uuid, org_name, email) tuple. Callers that want
the discovered org_uuid bound into the client's credentials should use
`session.open_session(...)`, which does that wiring once.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from .errors import EndpointChanged, NetworkError, SchemaDrift

if TYPE_CHECKING:
    from .client import ClaudeClient

log = structlog.get_logger(__name__)


def _extract_email(payload: Any) -> str | None:
    """Tolerate the multiple shapes /api/bootstrap has shipped over time."""
    if not isinstance(payload, dict):
        return None
    for path in (
        ("account", "email_address"),
        ("account", "email"),
        ("account", "primaryEmail"),
        ("user", "email_address"),
        ("user", "email"),
        ("email_address",),
        ("email",),
    ):
        v: Any = payload
        for key in path:
            if not isinstance(v, dict):
                v = None
                break
            v = v.get(key)
        if isinstance(v, str) and "@" in v:
            return v
    return None


def _extract_org(payload: Any) -> tuple[str, str | None] | None:
    if not isinstance(payload, dict):
        return None
    candidates: list[Any] = []
    for key in ("organizations", "active_organization", "memberships", "orgs"):
        v = payload.get(key)
        if isinstance(v, list):
            candidates.extend(v)
        elif isinstance(v, dict):
            candidates.append(v)
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        nested = cand.get("organization")
        org: dict[str, Any] = nested if isinstance(nested, dict) else cand
        uuid = org.get("uuid") or org.get("organization_uuid") or org.get("id")
        if isinstance(uuid, str):
            name_field = org.get("name")
            name = name_field if isinstance(name_field, str) else None
            return uuid, name
    return None


async def discover_org(client: ClaudeClient) -> tuple[str, str | None, str | None]:
    """Return (org_uuid, org_name, email). Tries /api/bootstrap first."""
    try:
        payload = await client.get_json("/api/bootstrap")
    except EndpointChanged:
        payload = None

    if payload is not None:
        email = _extract_email(payload)
        org = _extract_org(payload)
        if org is not None:
            uuid, name = org
            return uuid, name, email

    # Fallback to /api/organizations
    try:
        orgs = await client.get_json("/api/organizations")
    except EndpointChanged as e:
        raise NetworkError(
            "claude.ai returned 404 on both /api/bootstrap and /api/organizations "
            "— org discovery is impossible. Either Anthropic moved both endpoints "
            "(tool may need an update) or your network is intercepting requests."
        ) from e

    if not isinstance(orgs, list) or not orgs:
        raise SchemaDrift(
            "claude.ai's /api/organizations didn't return a non-empty list "
            "of orgs (got {type(orgs).__name__}). Schema drift on Anthropic's "
            "side, or your account has no organizations attached."
        )

    first = orgs[0]
    if not isinstance(first, dict):
        raise SchemaDrift(
            "claude.ai's /api/organizations[0] isn't an object — schema drift."
        )
    uuid_field = first.get("uuid") or first.get("id")
    if not isinstance(uuid_field, str):
        raise SchemaDrift(
            "claude.ai's /api/organizations[0] is missing the expected `uuid` "
            "(or `id`) field — schema drift."
        )
    name_field = first.get("name")
    name = name_field if isinstance(name_field, str) else None
    email_only: str | None = None
    try:
        acct = await client.get_json("/api/account")
        email_only = _extract_email(acct) or _extract_email({"account": acct})
    except (EndpointChanged, NetworkError):
        pass
    return uuid_field, name, email_only
