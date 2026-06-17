"""Daily-digest sender.

:class:`DigestSender` is the only collaborator that talks to the
Telegram Bot API in this slice. It pulls stats from
:class:`StatsService`, renders the message with
:func:`render_digest_message` and forwards it to the bot's
``send_message`` method. The bot is duck-typed (anything with an
``async send_message(chat_id, text)`` method works), which keeps the
production ``TelegramBot`` and the test fake interchangeable.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import date, datetime
from typing import Any, Protocol

from apply_pilot.features.telegram.digest.models import UserStats
from apply_pilot.features.telegram.digest.render import render_digest_message
from apply_pilot.features.users.models import User

_LOGGER = logging.getLogger("apply_pilot.features.telegram.digest.sender")


class _StatsServiceLike(Protocol):
    async def get_user_stats(
        self,
        user_id: uuid.UUID,
        *,
        on_date: date | None = None,
    ) -> UserStats: ...

    async def get_all_users_with_telegram(self) -> list[User]: ...


class _TelegramBotLike(Protocol):
    async def send_message(self, chat_id: int, text: str) -> dict[str, Any]: ...


class _TelegramAccountRepo(Protocol):
    def list_all(self) -> list[object]: ...


class DigestSender:
    """Send digests to one user or to every linked user.

    The sender keeps its collaborators on simple attributes so the
    FastAPI dependency in :mod:`api` and the scheduled
    :class:`DigestRunner` can both reuse the same instance.
    """

    def __init__(
        self,
        stats_service: _StatsServiceLike,
        telegram_bot: _TelegramBotLike,
        telegram_account_repo: _TelegramAccountRepo,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._stats_service = stats_service
        self._telegram_bot = telegram_bot
        self._telegram_account_repo = telegram_account_repo
        self._now: Callable[[], datetime] = now or _default_now

    # ------------------------------------------------------------------
    # Public attributes (exposed for tests and the FastAPI dependency)
    # ------------------------------------------------------------------
    @property
    def stats_service(self) -> _StatsServiceLike:
        return self._stats_service

    @property
    def telegram_bot(self) -> _TelegramBotLike:
        return self._telegram_bot

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

        A user without a linked Telegram account is silently skipped
        (the digest broadcast always operates on the
        ``telegram_accounts`` join, but a manual trigger could point
        at a user whose link was revoked).
        """
        chat_id = self._find_chat_id(user_id)
        if chat_id is None:
            return False
        return await self._dispatch(user_id, chat_id, on_date=on_date)

    async def send_to_all_users(
        self,
        users: list[uuid.UUID] | None = None,
        *,
        on_date: date | None = None,
    ) -> int:
        """Send a digest to every linked user; return the count sent.

        ``users`` overrides the default enumeration when supplied
        (used by tests and by the manual ``POST /digest/send``
        endpoint when the caller wants to scope the broadcast).
        """
        target_users = (
            users
            if users is not None
            else [u.id for u in await self._stats_service.get_all_users_with_telegram()]
        )
        sent = 0
        for user_id in target_users:
            if await self.send_to_user(user_id, on_date=on_date):
                sent += 1
        return sent

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _find_chat_id(self, user_id: uuid.UUID) -> int | None:
        for account in self._telegram_account_repo.list_all():
            # ``list_all`` returns :class:`TelegramAccount` rows; keep
            # the access duck-typed to avoid coupling the slice to the
            # ORM model when a different repo implementation lands.
            if getattr(account, "user_id", None) == user_id:
                return getattr(account, "telegram_user_id", None)
        return None

    async def _dispatch(
        self,
        user_id: uuid.UUID,
        chat_id: int,
        *,
        on_date: date | None,
    ) -> bool:
        target_date = on_date or self._now().date()
        stats = await self._stats_service.get_user_stats(user_id, on_date=target_date)
        text = render_digest_message(stats)
        try:
            await self._telegram_bot.send_message(chat_id, text)
        except Exception:
            _LOGGER.exception(
                "digest.send_message.failed",
                extra={
                    "event": "digest.send_message.failed",
                    "user_id": str(user_id),
                    "chat_id": chat_id,
                },
            )
            return False
        return True


def _default_now() -> datetime:
    from datetime import UTC

    return datetime.now(UTC)


__all__ = ["DigestSender"]
