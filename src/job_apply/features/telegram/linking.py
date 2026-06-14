"""Account-linking service for the Telegram slice.

:class:`TelegramLinkingService` owns the one-time-code lifecycle:
generation, validation, consumption, and reverse lookup.  The token
store is an in-memory dict (no Redis for M1); the ORM write path lives
in a separate :class:`TelegramAccountRepository` so the service stays
testable with a fake.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field


class InvalidLinkingTokenError(Exception):
    """The supplied one-time code is unknown, already consumed, or expired."""


class TelegramAccountAlreadyLinkedError(Exception):
    """The Telegram user id is already linked to a different local user."""


@dataclass
class _TokenRecord:
    user_id: str
    consumed: bool = False
    expires_at: float = field(default_factory=lambda: time.monotonic() + 600)


class TelegramLinkingService:
    """Generates one-time linking codes and validates them.

    The service is intentionally side-effect free at the ORM level:
    ``link_account`` updates the in-memory bookkeeping and returns the
    resolved user id; the caller (the bot dispatcher or a route handler)
    is responsible for persisting the :class:`TelegramAccount` row via
    a :class:`TelegramAccountRepository`.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens: dict[str, _TokenRecord] = {}
        # Forward index: user_id (str) → telegram_user_id (int)
        self._links: dict[str, int] = {}
        # Reverse index: telegram_user_id (int) → user_id (str)
        self._telegram_to_user: dict[int, str] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate_token(self, *, user_id: str, ttl_seconds: float = 600) -> str:
        """Issue a new one-time linking token for *user_id*.

        The previous token for this user (if any) is silently replaced so a
        user can request a fresh code at any time.  *ttl_seconds* controls
        how long the token remains valid (default 600 s = 10 min).
        """
        token = secrets.token_hex(8)  # 16 hex chars → 64 bits of entropy
        expires_at = time.monotonic() + ttl_seconds
        with self._lock:
            # Invalidate any previous token for this user
            stale = [k for k, v in self._tokens.items() if v.user_id == user_id]
            for k in stale:
                del self._tokens[k]
            self._tokens[token] = _TokenRecord(user_id=user_id, expires_at=expires_at)
        return token

    def link_account(self, *, token: str, telegram_user_id: int) -> str:
        """Consume *token* and record the Telegram user id.

        Returns the *user_id* the token was issued for on success.

        Raises:
            InvalidLinkingTokenError: if the token is unknown,
                already consumed, or expired.
            TelegramAccountAlreadyLinkedError: if *telegram_user_id* is
                already linked to a different local user.
        """
        with self._lock:
            # --- clean up expired entries first --------------------------------
            now = time.monotonic()
            expired_keys = [k for k, v in self._tokens.items() if v.expires_at <= now]
            for k in expired_keys:
                del self._tokens[k]

            record = self._tokens.get(token)
            if record is None:
                raise InvalidLinkingTokenError("unknown or expired linking token")
            if record.expires_at <= now:
                del self._tokens[token]
                raise InvalidLinkingTokenError("linking token has expired")
            if record.consumed:
                raise InvalidLinkingTokenError("linking token already used")

            # --- duplicate guard: a Telegram account can only link once --------
            existing_user = self._telegram_to_user.get(telegram_user_id)
            if existing_user is not None and existing_user != record.user_id:
                raise TelegramAccountAlreadyLinkedError(
                    f"Telegram user {telegram_user_id} is already linked "
                    f"to local user {existing_user}"
                )

            record.consumed = True
            self._links[record.user_id] = telegram_user_id
            self._telegram_to_user[telegram_user_id] = record.user_id
        return record.user_id

    def find_telegram_user_id(self, *, user_id: str) -> int | None:
        """Return the Telegram user id linked to *user_id*, or ``None``."""
        with self._lock:
            return self._links.get(user_id)

    def find_user_id(self, *, telegram_user_id: int) -> str | None:
        """Return the local user id linked to *telegram_user_id*, or ``None``."""
        with self._lock:
            return self._telegram_to_user.get(telegram_user_id)

    # Expose the internal mapping for test introspection and for the
    # API endpoint that needs to know which user owns a token.
    def get_user_id_for_token(self, token: str) -> str | None:
        """Return the user_id that *token* was issued for, or ``None``."""
        with self._lock:
            record = self._tokens.get(token)
            if record is None:
                return None
            return record.user_id


# --------------------------------------------------------------------------
# Module-level default instance
# --------------------------------------------------------------------------

# A single shared instance so the FastAPI endpoint and the bot
# dispatcher share the same token/link state.  Production wiring (and
# tests) can inject their own instance via ``get_linking_service``.
_default_linking_service: TelegramLinkingService = TelegramLinkingService()


def get_linking_service() -> TelegramLinkingService:
    """Return the process-wide default linking service.

    Use this as a FastAPI dependency or inject it into the bot.
    Tests can override it by passing their own instance.
    """
    return _default_linking_service


__all__ = [
    "InvalidLinkingTokenError",
    "TelegramAccountAlreadyLinkedError",
    "TelegramLinkingService",
    "get_linking_service",
]
