"""Password hashing and bearer-token primitives for the auth slice.

Design notes
------------

We deliberately avoid pulling in ``argon2-cffi`` / ``passlib`` for M1 to
keep the dependency surface small. Passwords are hashed with PBKDF2-HMAC
(SHA-256, 200_000 iterations, 16-byte random salt) using only the
standard library. The stored format is self-describing so we can later
swap in Argon2 without breaking existing hashes::

    pbkdf2_sha256$<iterations>$<hex(salt)>$<hex(derived_key)>

Tokens are random 32-byte urlsafe strings stored in a process-local
in-memory ``TokenStore``. The store is a class-level singleton, but the
``AuthService`` accepts an injected instance so tests can use a fresh
one and so that a future slice can swap the store for a Redis-backed
implementation without changing the service.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Protocol

# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

# PBKDF2 work factor. 200k iterations of SHA-256 is the OWASP 2023
# recommendation for PBKDF2-SHA256 and is the default used by Werkzeug's
# security helper. Tune downward only for test speed.
_PBKDF2_ITERATIONS = 200_000
_PBKDF2_ALG = "sha256"
_SALT_BYTES = 16
_KEY_BYTES = 32

_HASH_PREFIX = "pbkdf2_sha256"


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def hash_password(plain: str) -> str:
    """Return a self-describing hash string for ``plain``.

    The result is safe to persist; the original ``plain`` is never
    recoverable from it.
    """
    if not isinstance(plain, str) or not plain:
        raise ValueError("password must be a non-empty string")
    salt = secrets.token_bytes(_SALT_BYTES)
    derived = hashlib.pbkdf2_hmac(
        _PBKDF2_ALG, plain.encode("utf-8"), salt, _PBKDF2_ITERATIONS, dklen=_KEY_BYTES
    )
    return f"{_HASH_PREFIX}${_PBKDF2_ITERATIONS}${_encode(salt)}${_encode(derived)}"


def verify_password(plain: str, stored_hash: str) -> bool:
    """Return ``True`` iff ``plain`` matches the previously-stored ``stored_hash``.

    Uses a constant-time comparison on the derived key bytes to avoid
    timing side channels.
    """
    if not isinstance(plain, str) or not isinstance(stored_hash, str):
        return False
    try:
        scheme, iter_str, salt_b64, key_b64 = stored_hash.split("$", 3)
    except ValueError:
        return False
    if scheme != _HASH_PREFIX:
        return False
    try:
        iterations = int(iter_str)
    except ValueError:
        return False
    try:
        salt = _decode(salt_b64)
        expected = _decode(key_b64)
    except (ValueError, TypeError):
        return False
    derived = hashlib.pbkdf2_hmac(
        _PBKDF2_ALG, plain.encode("utf-8"), salt, iterations, dklen=len(expected)
    )
    return hmac.compare_digest(derived, expected)


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


class InvalidTokenError(Exception):
    """Raised when a bearer token is unknown, malformed, or expired."""


@dataclass(frozen=True)
class IssuedToken:
    """A bearer token plus its metadata."""

    access_token: str
    token_type: str = "bearer"


class _TokenRecord:
    __slots__ = ("user_id", "expires_at")

    def __init__(self, user_id: str, expires_at: float) -> None:
        self.user_id = user_id
        self.expires_at = expires_at


class TokenStore(Protocol):
    """Minimal interface the AuthService relies on.

    A protocol keeps tests honest: any object that satisfies it can be
    injected, including in-memory fakes and (in a later slice) a
    Redis-backed implementation.
    """

    def issue(self, user_id: str, ttl_seconds: int) -> str: ...
    def resolve(self, token: str) -> str: ...
    def revoke(self, token: str) -> None: ...


class InMemoryTokenStore:
    """Thread-safe in-process token store.

    Tokens auto-expire lazily: ``resolve`` checks the deadline on
    every call and removes expired entries it encounters.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, _TokenRecord] = {}

    def issue(self, user_id: str, ttl_seconds: int) -> str:
        token = secrets.token_urlsafe(32)
        expires_at = time.monotonic() + ttl_seconds
        with self._lock:
            self._records[token] = _TokenRecord(user_id=user_id, expires_at=expires_at)
        return token

    def resolve(self, token: str) -> str:
        with self._lock:
            record = self._records.get(token)
            if record is None:
                raise InvalidTokenError("unknown token")
            if record.expires_at <= time.monotonic():
                # Lazy expiry: clean up so the dict does not grow forever.
                self._records.pop(token, None)
                raise InvalidTokenError("token expired")
            return record.user_id

    def revoke(self, token: str) -> None:
        with self._lock:
            self._records.pop(token, None)


# ---------------------------------------------------------------------------
# Token hashing (for session persistence, issue #12)
# ---------------------------------------------------------------------------


def hash_token(token: str) -> str:
    """Return the SHA-256 hex digest of a raw bearer token.

    The raw token is a 32-byte random urlsafe string, so a fast
    cryptographic hash is sufficient — we do not need a password-grade
    KDF here because the input is already high-entropy.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Module-level default token store
# ---------------------------------------------------------------------------

# A single shared instance keeps the slice drop-in usable for callers
# that don't want to wire DI themselves. ``AuthService`` accepts its own
# store, so production wiring (and tests) stay in control of lifetime.
_default_token_store: InMemoryTokenStore = InMemoryTokenStore()


def issue_token(user_id: str, ttl_seconds: int) -> str:
    """Issue a bearer token bound to ``user_id`` using the default store."""
    return _default_token_store.issue(user_id, ttl_seconds=ttl_seconds)


def verify_token(token: str) -> str:
    """Resolve ``token`` to a user id using the default store."""
    return _default_token_store.resolve(token)


def default_token_store() -> InMemoryTokenStore:
    """Return the module-level default token store (for tests and DI wiring)."""
    return _default_token_store


__all__ = [
    "InvalidTokenError",
    "IssuedToken",
    "InMemoryTokenStore",
    "TokenStore",
    "default_token_store",
    "hash_password",
    "hash_token",
    "issue_token",
    "verify_password",
    "verify_token",
]
