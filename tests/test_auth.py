"""Exhaustive tests for the auth normalizer and format validators (Section 7.2/7.3)."""

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
# Normalizer — every row in Section 7.2 plus a few extras
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


def test_list_profiles_continues_through_keyring_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A keyring error on one guess must not skip the rest of the loop nor
    drop the file-fallback profiles."""
    from keyring.errors import NoKeyringError

    from claude_migrate import auth as auth_mod

    seen: list[str] = []

    def flaky_get(service: str, name: str) -> str | None:
        seen.append(name)
        if name == "source":
            raise NoKeyringError("simulated transient")
        if name == "target":
            return '{"session_key": "x", "cf_clearance": "y"}'
        return None

    monkeypatch.setattr(auth_mod.keyring, "get_password", flaky_get)
    monkeypatch.setattr(auth_mod, "_fallback_blob_load_or_empty", lambda: {})
    names = auth_mod.list_profiles()
    # Must have probed past `source` despite the error.
    assert "target" in names
    assert "source" in seen
    assert "target" in seen
