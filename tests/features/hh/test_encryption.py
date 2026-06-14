"""TDD tests for the HH credential encryption primitives.

These tests exercise :class:`CredentialEncryptor` against the contract:
encrypt/decrypt roundtrip, key rotation, and redaction in repr/str
so tokens never leak into logs.
"""

from __future__ import annotations

import base64

import pytest
from cryptography.fernet import Fernet, InvalidToken

from job_apply.features.hh.encryption import CredentialEncryptor


def _key_material_strs(encryptor: CredentialEncryptor) -> tuple[str, str, str]:
    """Return (raw-key-bytes, base64-key, hex-key) representations of the key material.

    Fernet's internal keys are raw 32-byte values; we expose them as bytes,
    base64, and hex so tests can check for the presence of *any* of these
    representations in repr/str output.
    """
    raw = encryptor._fernet._signing_key + encryptor._fernet._encryption_key
    return (
        raw.decode("latin-1"),
        base64.b64encode(raw).decode("ascii"),
        raw.hex(),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def key() -> bytes:
    """A fresh Fernet key for each test."""
    return Fernet.generate_key()


@pytest.fixture
def encryptor(key: bytes) -> CredentialEncryptor:
    """Return an encryptor bound to the fresh key."""
    return CredentialEncryptor(key=key)


# ---------------------------------------------------------------------------
# Encrypt / decrypt roundtrip
# ---------------------------------------------------------------------------


def test_encrypt_returns_nonempty_string(encryptor: CredentialEncryptor) -> None:
    """encrypt() must return a non-empty base64 string different from the input."""
    plain = "hh-ru-token-abc123"
    token = encryptor.encrypt(plain)

    assert isinstance(token, str)
    assert len(token) > 0
    assert token != plain


def test_encrypt_is_non_deterministic(encryptor: CredentialEncryptor) -> None:
    """Two encryptions of the same plaintext must produce different tokens (unique IV)."""
    plain = "same-plaintext-twice"

    first = encryptor.encrypt(plain)
    second = encryptor.encrypt(plain)

    assert first != second


def test_decrypt_roundtrip(encryptor: CredentialEncryptor) -> None:
    """decrypt(encrypt(plain)) must recover the original plaintext."""
    plain = "access-token-value-42"

    token = encryptor.encrypt(plain)
    recovered = encryptor.decrypt(token)

    assert recovered == plain


def test_decrypt_roundtrip_unicode(encryptor: CredentialEncryptor) -> None:
    """Unicode strings (including non-ASCII chars) must survive encryption."""
    plain = "токен-доступа-на-русском-языке"
    token = encryptor.encrypt(plain)
    recovered = encryptor.decrypt(token)
    assert recovered == plain


def test_decrypt_raises_on_corrupted_token(encryptor: CredentialEncryptor) -> None:
    """decrypt() must raise on a corrupted token."""
    with pytest.raises(InvalidToken):
        encryptor.decrypt("not-a-valid-fernet-token")


def test_decrypt_raises_on_wrong_key(encryptor: CredentialEncryptor) -> None:
    """A token encrypted with one key must NOT decrypt with a different key."""
    token = encryptor.encrypt("secret")

    other = CredentialEncryptor(key=Fernet.generate_key())
    with pytest.raises(InvalidToken):
        other.decrypt(token)


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------


def test_rotation_reencrypts_with_new_key(encryptor: CredentialEncryptor) -> None:
    """After rotate(), the same plaintext should decrypt successfully but with a
    different ciphertext (new key)."""
    plain = "rotate-me"

    token_old = encryptor.encrypt(plain)
    new_key = Fernet.generate_key()
    encryptor.rotate(new_key)

    # Old token encrypted with old key should raise
    with pytest.raises(InvalidToken):
        encryptor.decrypt(token_old)

    # New encryption should work
    token_new = encryptor.encrypt(plain)
    recovered = encryptor.decrypt(token_new)
    assert recovered == plain


# ---------------------------------------------------------------------------
# Redaction — sensitive fields must not appear in repr/str
# ---------------------------------------------------------------------------


def test_encryptor_repr_redacts_key(encryptor: CredentialEncryptor) -> None:
    """The repr of CredentialEncryptor must not expose the raw key bytes."""
    rep = repr(encryptor)
    assert "REDACTED" in rep
    raw, b64, hex_key = _key_material_strs(encryptor)
    assert raw not in rep
    assert b64 not in rep
    assert hex_key not in rep


def test_encryptor_str_redacts_key(encryptor: CredentialEncryptor) -> None:
    """The str() of CredentialEncryptor must not expose the raw key bytes."""
    s = str(encryptor)
    assert "REDACTED" in s
    raw, b64, hex_key = _key_material_strs(encryptor)
    assert raw not in s
    assert b64 not in s
    assert hex_key not in s


# ---------------------------------------------------------------------------
# Key from env var
# ---------------------------------------------------------------------------


def test_from_env_uses_app_hh_encryption_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """CredentialEncryptor.from_env() reads APP_HH_ENCRYPTION_KEY."""
    test_key = Fernet.generate_key().decode()
    monkeypatch.setenv("APP_HH_ENCRYPTION_KEY", test_key)

    enc = CredentialEncryptor.from_env()
    token = enc.encrypt("test")
    recovered = enc.decrypt(token)
    assert recovered == "test"


def test_from_env_raises_when_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """CredentialEncryptor.from_env() raises when APP_HH_ENCRYPTION_KEY is not set."""
    monkeypatch.delenv("APP_HH_ENCRYPTION_KEY", raising=False)
    with pytest.raises(ValueError, match="APP_HH_ENCRYPTION_KEY"):
        CredentialEncryptor.from_env()
