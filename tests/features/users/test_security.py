"""TDD tests for the password hashing + token primitives.

These tests define the public contract of the security helpers used by
the auth slice. We pin the hashing algorithm and the token format so
that downstream code can rely on the stable surface.
"""

from __future__ import annotations

import time

import pytest

from apply_pilot.features.users.security import (
    InvalidTokenError,
    hash_password,
    issue_token,
    verify_password,
    verify_token,
)


def test_hash_password_returns_string_different_from_plaintext() -> None:
    """A hashed password must not be the original plaintext."""
    plain = "super-secret-pw"
    hashed = hash_password(plain)

    assert isinstance(hashed, str)
    assert hashed != plain
    assert len(hashed) > 0


def test_hash_password_is_non_deterministic() -> None:
    """Two hashes of the same password must differ (random salt)."""
    plain = "super-secret-pw"

    first = hash_password(plain)
    second = hash_password(plain)

    assert first != second


def test_verify_password_round_trip() -> None:
    """verify_password should return True for a matching password and False otherwise."""
    hashed = hash_password("hunter2")

    assert verify_password("hunter2", hashed) is True
    assert verify_password("wrong", hashed) is False


def test_issue_token_returns_nonempty_string() -> None:
    """A freshly-issued token is a non-empty string identifier."""
    token = issue_token("user-1", ttl_seconds=60)

    assert isinstance(token, str)
    assert len(token) > 0


def test_verify_token_round_trip() -> None:
    """A token issued for a user should verify back to that same user id."""
    token = issue_token("user-1", ttl_seconds=60)

    assert verify_token(token) == "user-1"


def test_verify_token_rejects_unknown_token() -> None:
    """An unknown token must raise InvalidTokenError."""
    with pytest.raises(InvalidTokenError):
        verify_token("not-a-real-token")


def test_verify_token_rejects_expired_token() -> None:
    """A token whose TTL has elapsed must raise InvalidTokenError."""
    token = issue_token("user-1", ttl_seconds=0)

    # ttl_seconds=0 means it is already expired at the next clock tick.
    time.sleep(0.01)

    with pytest.raises(InvalidTokenError):
        verify_token(token)
