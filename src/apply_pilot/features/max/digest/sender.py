"""MAX-side daily-digest sender.

:class:`MaxDigestSender` is the only collaborator that talks to the
MAX Bot API in this slice. It pulls stats from
:class:`MaxStatsService`, renders the message with
:func:`apply_pilot.features.messaging.digest.render.render_digest_message`
and forwards it to the bot's ``send_message`` method. The bot is
duck-typed (anything with an ``async send_message(chat_id, text)``
method works), which keeps the production :class:`MaxBot` and the test
fake interchangeable.

The class mirrors :class:`apply_pilot.features.telegram.digest.sender.DigestSender`
field-for-field; the only differences are the chat identifier type
(int — ``max_user_id``) and the account-repo attribute names
(``user_id`` + ``max_user_id``).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import date, datetime
from typing import Any, Protocol

from apply_pilot.features.messaging.digest.render import render_digest_message
from apply_pilot.features.telegram.digest.models import UserStats
from apply_pilot.features.users.models import User

_LOGGER = logging.getLogger("apply_pilot.features.max.digest.sender")


class _StatsServiceLike(Protocol):
    def get_user_stats(
        self,
        user_id: uuid.UUID,
        *,
        on_date: date | None = None,
    ) -> UserStats: ...

    async def get_all_users_with_max(self) -> list[User]: ...


class _MaxBotLike(Protocol):
    async def send_message(self, chat_id: int, text: str) -> dict[str, Any]: ...


class _MaxAccountRepo(Protocol):
    def list_all(self) -> list[object]: ...


class MaxDigestSender:
    """Send digests to one user or to every linked user.

    The sender keeps its collaborators on simple attributes so the
    FastAPI dependency in :mod:`api` and the scheduled
    :class:`MaxDigestRunner` can both reuse the same instance.
    """

    def __init__(
        self,
        stats_service: _StatsServiceLike,
        max_bot: _MaxBotLike,
        max_account_repo: _MaxAccountRepo,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._stats_service = stats_service
        self._max_bot = max_bot
        self._max_account_repo = max_account_repo
        self._now: Callable[[], datetime] = now or _default_now

    # ------------------------------------------------------------------
    # Public attributes (exposed for tests and the FastAPI dependency)
    # ------------------------------------------------------------------
    @property
    def stats_service(self) -> _StatsServiceLike:
        return self._stats_service

    @property
    def max_bot(self) -> _MaxBotLike:
        return self._max_bot

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def send_to_user(
        self,
        user_id: uuid.UUID,
        *,
        on_date: date | None = None,
    ) -> bool:
        """Send a digest to *user_id*; return True if sent, False if no link.

        A user without a linked MAX account is silently skipped (the
        digest broadcast always operates on the ``max_accounts`` join,
        but a manual trigger could point at a user whose link was
        revoked).
        """
        max_user_id = self._find_max_user_id(user_id)
        if max_user_id is None:
            return False
        return await self._dispatch(user_id, max_user_id, on_date=on_date)

    async def send_to_all_users(
        self,
        users: list[uuid.UUID] | None = None,
        *,
        on_date: date | None = None,
    ) -> int:
        """Send a digest to every linked user; return the count sent.

        ``users`` overrides the default enumeration when supplied
        (used by tests and by the manual ``POST /digest/max/send``
        endpoint when the caller wants to scope the broadcast).
        """
        target_users = (
            users
            if users is not None
            else [u.id for u in await self._stats_service.get_all_users_with_max()]
        )
        sent = 0
        for user_id in target_users:
            if await self.send_to_user(user_id, on_date=on_date):
                sent += 1
        return sent

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _find_max_user_id(self, user_id: uuid.UUID) -> int | None:
        """Look up the MAX user id linked to *user_id*.

        ``list_all`` returns :class:`MaxAccount` rows; the access is
        duck-typed so the slice does not couple to the ORM model when
        a different repo implementation lands.
        """
        for account in self._max_account_repo.list_all():
            if getattr(account, "user_id", None) == user_id:
                return getattr(account, "max_user_id", None)
        return None

    async def _dispatch(
        self,
        user_id: uuid.UUID,
        max_user_id: int,
        *,
        on_date: date | None,
    ) -> bool:
        """Render the digest for *user_id* and forward it to MAX."""
        target_date = on_date or self._now().date()
        stats = self._stats_service.get_user_stats(user_id, on_date=target_date)
        text = render_digest_message(stats)
        try:
            await self._max_bot.send_message(max_user_id, text)
        except Exception:
            _LOGGER.exception(
                "max.digest.send_message.failed",
                extra={
                    "event": "max.digest.send_message.failed",
                    "user_id": str(user_id),
                    "max_user_id": max_user_id,
                },
            )
            return False
        return True


def _default_now() -> datetime:
    from datetime import UTC

    return datetime.now(UTC)


__all__ = ["MaxDigestSender"]
