"""Cookie paste flow, normalization, validation, probe, secure storage.

Section 7 of CLAUDE.md is the authoritative spec. Every error message here is
intentionally specific so users can self-recover without external help.
"""

from __future__ import annotations

import base64
import contextlib
import getpass
import json
import os
import re
import secrets
import sys
import urllib.parse
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import keyring
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from keyring.errors import KeyringError, NoKeyringError, PasswordDeleteError

from .client import ClaudeClient, Credentials
from .config import KEYRING_SERVICE, Settings, config_dir, load_settings
from .discover import discover_org
from .errors import (
    AuthExpired,
    AuthInvalid,
    AuthMissing,
    CloudflareChallenge,
    KeyringUnavailable,
    NetworkError,
    TLSReject,
)

SESSION_KEY_PREFIX: Final = "sk-ant-sid"
"""Stem prefix. Anthropic increments the trailing digits over time (sid01, sid02, ...)."""

SESSION_KEY_PREFIX_RE: Final = re.compile(r"^sk-ant-sid\d{2,}-")
SESSION_KEY_RE: Final = re.compile(r"^sk-ant-sid\d{2,}-[A-Za-z0-9_-]+$")
CF_CLEARANCE_RE: Final = re.compile(r"^[A-Za-z0-9_.-]+$")
SESSION_KEY_MIN_LEN: Final = 80
CF_CLEARANCE_MIN_LEN: Final = 40
MAX_PASTE_RETRIES: Final = 3

KNOWN_PREFIXES: Final = (
    "sessionKey:",
    "sessionKey =",
    "sessionKey=",
    "cf_clearance:",
    "cf_clearance =",
    "cf_clearance=",
    "Cookie:",
    "cookie:",
    "Bearer ",
)


# ---------------------------------------------------------------------------
# Normalization (silently fix common mistakes)
# ---------------------------------------------------------------------------


def normalize(raw: str) -> str:
    """Coerce common paste shapes (quoted, prefixed, URL-encoded) into a bare token."""
    s = raw.strip()
    # Strip surrounding quotes
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        s = s[1:-1].strip()
    # Strip "name: value" or "name=value" form, case-insensitive
    for prefix in KNOWN_PREFIXES:
        if s.lower().startswith(prefix.lower()):
            s = s[len(prefix):].strip()
            break
    # Trailing semicolon (common when copied from Cookie header)
    s = s.rstrip(";").strip()
    # Strip surrounding quotes again (common after prefix strip)
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        s = s[1:-1]
    # URL-decode percent-encoding (e.g. %2B → +)
    if "%" in s and re.search(r"%[0-9A-Fa-f]{2}", s):
        with contextlib.suppress(UnicodeDecodeError, ValueError):
            s = urllib.parse.unquote(s)
    return s


# ---------------------------------------------------------------------------
# Format validation — produces specific errors so the CLI can re-prompt
# ---------------------------------------------------------------------------


def validate_session_key(value: str) -> None:
    if not value:
        raise AuthInvalid(
            "Nothing was pasted. Please copy the full cookie value and try again."
        )
    if value.lower() in {"sessionkey", "session_key", "session-key"}:
        raise AuthInvalid(
            "That's the cookie name, not the value. Look for a long string starting with "
            "`sk-ant-sid<NN>-` (e.g. sk-ant-sid01-, sk-ant-sid02-) in the same row."
        )
    if " " in value or "\t" in value or "\n" in value:
        raise AuthInvalid(
            "That value contains whitespace, which means it might be multiple cookies "
            'pasted together. Please copy only the value of the single cookie named "sessionKey".'
        )
    if not SESSION_KEY_PREFIX_RE.match(value):
        raise AuthInvalid(
            "sessionKey should start with `sk-ant-sid<NN>-` "
            "(e.g. `sk-ant-sid01-`, `sk-ant-sid02-` — Anthropic rotates the digits over time). "
            f"Got something starting with {value[:16]!r}. "
            "Make sure you're copying the value of the cookie named 'sessionKey', not another."
        )
    if len(value) < SESSION_KEY_MIN_LEN:
        raise AuthInvalid(
            f"That looks too short — sessionKey is usually 100+ characters, got {len(value)}. "
            "The Value column may have been truncated. Click the row in DevTools and "
            "find the full value in the details panel below."
        )
    if not SESSION_KEY_RE.match(value):
        raise AuthInvalid(
            "sessionKey contains characters that don't belong (only letters, digits, "
            "underscore, and dash are valid). Re-copy the full value carefully."
        )


def validate_cf_clearance(value: str) -> None:
    if not value:
        raise AuthInvalid(
            "Nothing was pasted. Please copy the full cookie value and try again."
        )
    if value.startswith(SESSION_KEY_PREFIX) or "sk-ant-" in value:
        raise AuthInvalid(
            "That looks like a sessionKey, not cf_clearance. cf_clearance is a different "
            'cookie — find the row named exactly "cf_clearance" in the same table.'
        )
    if any(c in value for c in (":", "=", ";")):
        raise AuthInvalid(
            "cf_clearance contains a separator character (':' or '=' or ';'), which means "
            "you may have copied a name=value pair. Copy only the value column."
        )
    if " " in value or "\t" in value or "\n" in value:
        raise AuthInvalid(
            "cf_clearance contains whitespace. Copy only the bare value, no name and no quotes."
        )
    if len(value) < CF_CLEARANCE_MIN_LEN:
        raise AuthInvalid(
            f"cf_clearance looks too short ({len(value)} chars). Real values are 40+ chars. "
            "It may have been truncated; check the details panel below the cookie table."
        )
    if not CF_CLEARANCE_RE.match(value):
        raise AuthInvalid(
            "cf_clearance contains characters that don't belong. Re-copy the value carefully."
        )


# ---------------------------------------------------------------------------
# Storage (keyring with file fallback)
# ---------------------------------------------------------------------------


@dataclass
class Profile:
    session_key: str
    cf_clearance: str
    org_uuid: str | None = None
    email: str | None = None
    stored_at: str | None = None
    last_probe_ok: str | None = None

    def as_credentials(self) -> Credentials:
        return Credentials(
            session_key=self.session_key,
            cf_clearance=self.cf_clearance,
            org_uuid=self.org_uuid,
            email=self.email,
        )


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _fallback_path() -> Path:
    return config_dir() / "secrets.enc.json"


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    kdf = Scrypt(salt=salt, length=32, n=2**14, r=8, p=1)
    return kdf.derive(passphrase.encode("utf-8"))


def _fallback_blob_load() -> dict[str, dict[str, str]]:
    p = _fallback_path()
    if not p.exists():
        return {}
    raw = json.loads(p.read_text("utf-8"))
    salt = base64.b64decode(raw["salt"])
    nonce = base64.b64decode(raw["nonce"])
    ct = base64.b64decode(raw["ct"])
    pp = getpass.getpass("Passphrase for claude-migrate secrets file: ")
    key = _derive_key(pp, salt)
    pt = AESGCM(key).decrypt(nonce, ct, None)
    parsed: Any = json.loads(pt.decode("utf-8"))
    if not isinstance(parsed, dict):
        return {}
    return parsed


def _fallback_blob_save(blob: dict[str, dict[str, str]]) -> None:
    p = _fallback_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    pp = getpass.getpass("Set a passphrase to encrypt the secrets file: ")
    if len(pp) < 8:
        raise AuthInvalid("Passphrase too short — use at least 8 characters.")
    pp2 = getpass.getpass("Confirm passphrase: ")
    if pp != pp2:
        raise AuthInvalid("Passphrases did not match.")
    salt = secrets.token_bytes(16)
    nonce = secrets.token_bytes(12)
    key = _derive_key(pp, salt)
    pt = json.dumps(blob).encode("utf-8")
    ct = AESGCM(key).encrypt(nonce, pt, None)
    p.write_text(
        json.dumps(
            {
                "salt": base64.b64encode(salt).decode("ascii"),
                "nonce": base64.b64encode(nonce).decode("ascii"),
                "ct": base64.b64encode(ct).decode("ascii"),
            }
        ),
        "utf-8",
    )
    with contextlib.suppress(OSError):
        os.chmod(p, 0o600)


def store_profile(name: str, profile: Profile) -> None:
    payload = json.dumps(asdict(profile))
    try:
        keyring.set_password(KEYRING_SERVICE, name, payload)
        return
    except (NoKeyringError, KeyringError):
        pass
    print(
        "OS keychain unavailable — falling back to encrypted file at "
        f"{_fallback_path()}. Install gnome-keyring or kwallet for native storage.",
        file=sys.stderr,
    )
    blob = _fallback_blob_load_or_empty()
    blob[name] = json.loads(payload)
    _fallback_blob_save(blob)


def _fallback_blob_load_or_empty() -> dict[str, dict[str, str]]:
    try:
        return _fallback_blob_load()
    except FileNotFoundError:
        return {}


def load_profile(name: str) -> Profile:
    raw: str | None
    try:
        raw = keyring.get_password(KEYRING_SERVICE, name)
    except (NoKeyringError, KeyringError):
        raw = None
    if raw is None:
        blob = _fallback_blob_load_or_empty()
        if name in blob:
            data = blob[name]
        else:
            raise AuthMissing(
                f"No profile named {name!r} found. Run `claude-migrate auth {name}` first."
            )
    else:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise AuthInvalid(
                f"Stored profile {name!r} is malformed in the keychain. "
                f"Run `claude-migrate auth refresh {name}` to recreate it."
            ) from e
    try:
        return Profile(**data)
    except TypeError as e:
        raise AuthInvalid(
            f"Stored profile {name!r} is missing required fields ({e}). "
            f"Run `claude-migrate auth refresh {name}` to recreate it."
        ) from e


def remove_profile(name: str) -> None:
    removed = False
    try:
        keyring.delete_password(KEYRING_SERVICE, name)
        removed = True
    except PasswordDeleteError:
        pass
    except (NoKeyringError, KeyringError) as e:
        raise KeyringUnavailable(str(e)) from e
    blob = _fallback_blob_load_or_empty()
    if name in blob:
        del blob[name]
        if blob:
            _fallback_blob_save(blob)
        else:
            with contextlib.suppress(FileNotFoundError):
                _fallback_path().unlink()
        removed = True
    if not removed:
        raise AuthMissing(f"No profile named {name!r} to remove.")


def list_profiles() -> list[str]:
    """Best-effort enumeration. keyring has no list API — we read the fallback file
    plus probe a small set of conventional names from the keychain. A keyring
    error on one guess does not skip the rest; we always merge in fallback-file
    profiles even if every keyring probe fails."""
    names: set[str] = set(_fallback_blob_load_or_empty().keys())
    for guess in ("source", "target", "personal", "work"):
        try:
            if keyring.get_password(KEYRING_SERVICE, guess):
                names.add(guess)
        except (NoKeyringError, KeyringError):
            continue
    return sorted(names)


# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------


@dataclass
class ProbeResult:
    org_uuid: str
    email: str | None
    org_name: str | None


async def probe(creds: Credentials, settings: Settings | None = None) -> ProbeResult:
    """Issue /api/bootstrap with these credentials. Map response to typed errors."""
    settings = settings or load_settings()
    client = ClaudeClient(creds, settings)
    try:
        org_uuid, org_name, email = await discover_org(client)
        return ProbeResult(org_uuid=org_uuid, org_name=org_name, email=email)
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# Interactive prompts
# ---------------------------------------------------------------------------


def _prompt_with_retries(
    label: str,
    validator: Callable[[str], None],
    *,
    extra_help: str = "",
) -> str:
    """Prompt for a paste and re-prompt up to MAX_PASTE_RETRIES on validation failure."""
    for attempt in range(1, MAX_PASTE_RETRIES + 1):
        raw = input(f"{label}> ").strip()
        value = normalize(raw)
        try:
            validator(value)
            return value
        except AuthInvalid as e:
            print(f"\n  ✗ {e}\n", file=sys.stderr)
            if attempt == MAX_PASTE_RETRIES:
                msg = (
                    "Three consecutive paste failures. Common causes:\n"
                    "  • The Value column in DevTools is truncated — click the "
                    "row and copy from the details panel.\n"
                    "  • You're copying the cookie *name* instead of its value.\n"
                    "  • The cookie has expired — sign in to claude.ai again "
                    "and re-copy.\n"
                    "Run `claude-migrate doctor` to confirm your environment, "
                    "or `claude-migrate auth <profile>` to retry."
                )
                if extra_help:
                    msg += f"\n\n{extra_help}"
                raise AuthInvalid(msg) from e
    raise AssertionError("unreachable")


SOURCE_INSTRUCTIONS = """\
Authenticating profile {target}. You'll paste two cookies from your browser
(~30 seconds, once per account).

  1. Open https://claude.ai signed in to the {target} account.
  2. Press F12, then go to:
       Chromium browsers:  Application tab → Cookies → claude.ai
       Firefox:            Storage tab → Cookies → claude.ai
       Safari:             Develop → Show Web Inspector → Storage → Cookies → claude.ai
                           (enable Develop menu first in Safari → Settings → Advanced)
  3. Copy the full Value for `sessionKey` and `cf_clearance`.
     The displayed Value column truncates — click the row to see the full
     value in the details panel and copy from there.

(See the README for screenshots and per-browser troubleshooting.)
"""


async def run_auth_flow(profile_name: str, *, refreshing: bool = False) -> Profile:
    """Interactive cookie paste flow."""
    settings = load_settings()
    print(SOURCE_INSTRUCTIONS.format(target=profile_name))
    print("sessionKey (starts with sk-ant-sid01- or sk-ant-sid02-, ~120 chars):")
    sk = _prompt_with_retries(
        "",
        validate_session_key,
        extra_help=(
            "If the Value column is truncated, click the row in DevTools and copy "
            "from the details panel below the table."
        ),
    )
    print("  ✓ sessionKey format OK")
    print("\ncf_clearance:")
    cf = _prompt_with_retries(
        "",
        validate_cf_clearance,
        extra_help=(
            "cf_clearance is a separate row in the same cookie table. If you don't "
            "see it, refresh https://claude.ai once in your browser to provoke a "
            "fresh challenge."
        ),
    )
    print("  ✓ cf_clearance format OK\n")

    print("Confirming credentials with claude.ai...")
    creds = Credentials(session_key=sk, cf_clearance=cf)
    try:
        result = await probe(creds, settings)
    except AuthExpired as e:
        raise AuthInvalid(
            "Your sessionKey was not accepted (HTTP 401). It was probably truncated when "
            "copied or it has expired. Re-copy the full value carefully — it should start "
            "with `sk-ant-sid<NN>-` (e.g. sk-ant-sid02-) and be over 100 characters long."
        ) from e
    except CloudflareChallenge as e:
        raise AuthInvalid(
            "Cloudflare is challenging the request. Refresh https://claude.ai once in your "
            "browser to get a fresh challenge clearance, then re-paste cf_clearance. "
            "Your sessionKey is fine."
        ) from e
    except TLSReject as e:
        raise AuthInvalid(
            "Cloudflare blocked the TLS fingerprint. Try `pip install -U curl_cffi`. "
            "If that doesn't help, file a GitHub issue — Anthropic may have updated bot "
            "detection."
        ) from e
    except NetworkError as e:
        raise AuthInvalid(
            f"Network error while probing claude.ai: {e}. Check your internet connection, "
            "VPN, or proxy settings."
        ) from e

    profile = Profile(
        session_key=sk,
        cf_clearance=cf,
        org_uuid=result.org_uuid,
        email=result.email,
        stored_at=_now_iso(),
        last_probe_ok=_now_iso(),
    )
    store_profile(profile_name, profile)
    verb = "Refreshed" if refreshing else "Authenticated"
    print(f"  ✓ {verb} as {result.email}", end="")
    if result.org_name:
        print(f" ({result.org_name})")
    else:
        print()
    print(f"    Stored as profile {profile_name!r} in the OS keychain.")
    print()
    print(f"  → `claude-migrate whoami {profile_name}`   live-probe this profile later")
    print(f"  → `claude-migrate login {profile_name}`    re-paste cookies after expiry")
    return profile


async def verify_profile(profile_name: str) -> ProbeResult:
    """Re-runs the probe with stored cookies and updates last_probe_ok."""
    profile = load_profile(profile_name)
    result = await probe(profile.as_credentials())
    profile.last_probe_ok = _now_iso()
    profile.org_uuid = result.org_uuid
    profile.email = result.email
    store_profile(profile_name, profile)
    return result
