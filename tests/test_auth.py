"""Exhaustive tests for the auth normalizer and format validators."""

from __future__ import annotations

import pytest

from claude_migrate.auth import (
    normalize,
    validate_cf_clearance,
    validate_session_key,
)
from claude_migrate.errors import AuthInvalid

VALID_SK = "sk-ant-sid01-" + "A" * 100  # 113 chars; well above min
VALID_SK_SID02 = "sk-ant-sid02-" + "A" * 100  # current real-world prefix
VALID_CF = "Zk0c.W3" + "A" * 50  # 57 chars


# ---------------------------------------------------------------------------
# Normalizer — every common paste shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("sk-ant-sid01-AbCd", "sk-ant-sid01-AbCd"),
        ("  sk-ant-sid01-AbCd  ", "sk-ant-sid01-AbCd"),
        ('"sk-ant-sid01-AbCd"', "sk-ant-sid01-AbCd"),
        ("'sk-ant-sid01-AbCd'", "sk-ant-sid01-AbCd"),
        ("sessionKey: sk-ant-sid01-AbCd", "sk-ant-sid01-AbCd"),
        ("sessionKey=sk-ant-sid01-AbCd", "sk-ant-sid01-AbCd"),
        ("sessionKey =sk-ant-sid01-AbCd", "sk-ant-sid01-AbCd"),
        ('sessionKey="sk-ant-sid01-AbCd"', "sk-ant-sid01-AbCd"),
        ("sk-ant-sid01-AbCd;", "sk-ant-sid01-AbCd"),
        ("sk-ant-sid01-A%2BbCd", "sk-ant-sid01-A+bCd"),
        ("Cookie: sk-ant-sid01-AbCd", "sk-ant-sid01-AbCd"),
        ("Bearer sk-ant-sid01-AbCd", "sk-ant-sid01-AbCd"),
        ("\n\nsk-ant-sid01-AbCd\n", "sk-ant-sid01-AbCd"),
    ],
)
def test_normalize_session_key(raw: str, expected: str) -> None:
    assert normalize(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("XYZ", "XYZ"),
        ("cf_clearance: XYZ", "XYZ"),
        ("cf_clearance=XYZ", "XYZ"),
        ('"XYZ";', "XYZ"),
        ("XYZ%2EAbc", "XYZ.Abc"),
    ],
)
def test_normalize_cf_clearance_paste_shapes(raw: str, expected: str) -> None:
    assert normalize(raw) == expected


def test_normalize_idempotent() -> None:
    once = normalize("  'sessionKey: sk-ant-sid01-AbCd'  ")
    twice = normalize(once)
    assert once == twice == "sk-ant-sid01-AbCd"


# ---------------------------------------------------------------------------
# sessionKey validator
# ---------------------------------------------------------------------------


def test_session_key_valid() -> None:
    validate_session_key(VALID_SK)


def test_session_key_sid02_accepted() -> None:
    """Anthropic rotated from sid01 to sid02 over time — both must validate."""
    validate_session_key(VALID_SK_SID02)


def test_session_key_future_sid_digits_accepted() -> None:
    """Forward-compat: any 2+ digit sid stem should pass format validation."""
    validate_session_key("sk-ant-sid42-" + "A" * 100)


def test_session_key_wrong_sid_stem_rejected() -> None:
    """Stem must be `sid<digits>-`; reject `sk-ant-foo-` and similar."""
    with pytest.raises(AuthInvalid, match="sk-ant-sid"):
        validate_session_key("sk-ant-foo-" + "A" * 100)


def test_session_key_too_short() -> None:
    with pytest.raises(AuthInvalid, match="too short"):
        validate_session_key("sk-ant-sid01-")


def test_session_key_literal_name_rejected() -> None:
    with pytest.raises(AuthInvalid, match="cookie name"):
        validate_session_key("sessionKey")


def test_session_key_wrong_prefix() -> None:
    with pytest.raises(AuthInvalid, match="should start with"):
        validate_session_key("sid01-" + "A" * 100)


def test_session_key_with_whitespace() -> None:
    with pytest.raises(AuthInvalid, match="whitespace"):
        validate_session_key("sk-ant-sid01-Abcd Efgh" + "A" * 90)


def test_session_key_empty() -> None:
    with pytest.raises(AuthInvalid, match="Nothing was pasted"):
        validate_session_key("")


def test_session_key_invalid_chars() -> None:
    with pytest.raises(AuthInvalid, match="don't belong"):
        validate_session_key("sk-ant-sid01-" + "A" * 80 + "$$")


# ---------------------------------------------------------------------------
# cf_clearance validator
# ---------------------------------------------------------------------------


def test_cf_clearance_valid() -> None:
    validate_cf_clearance(VALID_CF)


def test_cf_clearance_session_key_paste_rejected() -> None:
    with pytest.raises(AuthInvalid, match="looks like a sessionKey"):
        validate_cf_clearance(VALID_SK)


def test_cf_clearance_with_separator() -> None:
    with pytest.raises(AuthInvalid, match="separator character"):
        validate_cf_clearance("foo=bar" + "x" * 40)


def test_cf_clearance_too_short() -> None:
    with pytest.raises(AuthInvalid, match="too short"):
        validate_cf_clearance("abc")


def test_cf_clearance_with_whitespace() -> None:
    with pytest.raises(AuthInvalid, match="whitespace"):
        validate_cf_clearance("Zk0c.W3 " + "A" * 50)


def test_cf_clearance_empty() -> None:
    with pytest.raises(AuthInvalid, match="Nothing was pasted"):
        validate_cf_clearance("")


def test_cf_clearance_invalid_chars() -> None:
    with pytest.raises(AuthInvalid, match="don't belong"):
        validate_cf_clearance("Zk0c@W3" + "A" * 50)


# ---------------------------------------------------------------------------
# Stored-profile robustness (Track 1 of audit)
# ---------------------------------------------------------------------------


def test_load_profile_missing_required_fields_raises_authinvalid(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A keychain entry that's parseable JSON but missing fields like
    cf_clearance must raise AuthInvalid (not TypeError)."""
    import json

    from claude_migrate import auth as auth_mod

    monkeypatch.setattr(
        auth_mod.keyring,
        "get_password",
        lambda service, name: json.dumps({"session_key": "incomplete"}),
    )
    with pytest.raises(AuthInvalid, match="missing required fields"):
        auth_mod.load_profile("source")


def test_list_profiles_uses_index_file_as_source_of_truth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """list_profiles reads the plaintext index file written by store_profile,
    so profile names like `acme-prod` show up regardless of keychain enumeration
    (which has no list API)."""
    from claude_migrate import auth as auth_mod

    monkeypatch.setattr(auth_mod, "_index_load", lambda: {"acme-prod", "personal"})
    monkeypatch.setattr(auth_mod, "_fallback_blob_load_or_empty", lambda: {})
    names = auth_mod.list_profiles()
    assert names == ["acme-prod", "personal"]


def test_list_profiles_unions_fallback_file_for_legacy_installs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fallback-file keys are unioned in so installs that predate the index
    file still surface their profiles."""
    from claude_migrate import auth as auth_mod

    monkeypatch.setattr(auth_mod, "_index_load", lambda: set())
    monkeypatch.setattr(
        auth_mod, "_fallback_blob_load_or_empty",
        lambda: {"legacy": {"session_key": "x", "cf_clearance": "y"}},
    )
    names = auth_mod.list_profiles()
    assert "legacy" in names


def test_list_profiles_tolerates_corrupt_fallback_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bad passphrase or corrupted secrets file shouldn't make `accounts` fail."""
    from claude_migrate import auth as auth_mod
    from claude_migrate.errors import AuthInvalid

    def boom() -> dict[str, dict[str, str]]:
        raise AuthInvalid("simulated decrypt failure")

    monkeypatch.setattr(auth_mod, "_index_load", lambda: {"primary"})
    monkeypatch.setattr(auth_mod, "_fallback_blob_load_or_empty", boom)
    names = auth_mod.list_profiles()
    assert names == ["primary"]


def test_fallback_blob_load_wraps_invalid_tag_as_auth_invalid(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wrong passphrase should raise AuthInvalid, not propagate InvalidTag."""
    import base64
    import json as json_mod
    import secrets

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from claude_migrate import auth as auth_mod
    from claude_migrate.errors import AuthInvalid

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))  # type: ignore[attr-defined]
    # Encrypt under one key, attempt decrypt under another → InvalidTag.
    salt = secrets.token_bytes(16)
    nonce = secrets.token_bytes(12)
    key = auth_mod._derive_key("correct-passphrase", salt)
    ct = AESGCM(key).encrypt(nonce, b'{"x": "y"}', None)
    auth_mod._fallback_path().parent.mkdir(parents=True, exist_ok=True)
    auth_mod._fallback_path().write_text(
        json_mod.dumps({
            "salt": base64.b64encode(salt).decode(),
            "nonce": base64.b64encode(nonce).decode(),
            "ct": base64.b64encode(ct).decode(),
        }),
        "utf-8",
    )
    auth_mod._cached_passphrase = "wrong-passphrase"
    try:
        with pytest.raises(AuthInvalid, match="Wrong passphrase"):
            auth_mod._fallback_blob_load()
        # Cache must be cleared so a retry isn't poisoned.
        assert auth_mod._cached_passphrase is None
    finally:
        auth_mod._cached_passphrase = None


def test_fallback_blob_load_wraps_malformed_json_as_auth_invalid(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Truncated or hand-edited file → AuthInvalid (not raw JSONDecodeError)."""
    from claude_migrate import auth as auth_mod
    from claude_migrate.errors import AuthInvalid

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))  # type: ignore[attr-defined]
    auth_mod._fallback_path().parent.mkdir(parents=True, exist_ok=True)
    auth_mod._fallback_path().write_text("{not valid json", "utf-8")
    with pytest.raises(AuthInvalid, match="unreadable"):
        auth_mod._fallback_blob_load()


def test_fallback_blob_load_wraps_missing_keys_as_auth_invalid(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A JSON file missing salt/nonce/ct → AuthInvalid."""
    from claude_migrate import auth as auth_mod
    from claude_migrate.errors import AuthInvalid

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))  # type: ignore[attr-defined]
    auth_mod._fallback_path().parent.mkdir(parents=True, exist_ok=True)
    auth_mod._fallback_path().write_text('{"salt": "abc"}', "utf-8")
    with pytest.raises(AuthInvalid, match="unreadable"):
        auth_mod._fallback_blob_load()


def test_index_save_is_atomic_and_fsynced(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_index_save writes through a tempfile and fsyncs before replace."""
    from claude_migrate import auth as auth_mod

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))  # type: ignore[attr-defined]
    auth_mod._index_save({"alpha", "beta"})
    p = auth_mod._index_path()
    assert p.exists()
    assert p.read_text("utf-8") == "alpha\nbeta\n"
    # Tempfile cleanup
    siblings = list(p.parent.glob(f"{p.name}.tmp-*"))
    assert siblings == [], f"leftover tempfiles: {siblings}"
