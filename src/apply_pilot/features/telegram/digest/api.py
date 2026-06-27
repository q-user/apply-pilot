"""FastAPI router for the daily digest slice.

Single endpoint — ``POST /digest/send`` — triggers a broadcast for
manual testing and operations. Auth is intentionally not enforced
yet: M1 did not yet ship role-based access control, and the
endpoint is meant for an internal trigger (cron / on-call) rather
than a user-facing button. A future slice will guard it behind an
admin role.
"""

from __future__ import annotations

import logging
from datetime import date

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from apply_pilot.config import TelegramSettings, get_telegram_settings
from apply_pilot.db import get_db
from apply_pilot.features.matches.repository import SqlVacancyMatchRepository
from apply_pilot.features.search_profiles.repository import SqlSearchProfileRepository
from apply_pilot.features.telegram.bot import TelegramBot
from apply_pilot.features.telegram.digest import DigestSender, StatsService
from apply_pilot.features.telegram.repository import SqlAlchemyTelegramAccountRepository
from apply_pilot.features.users.repository import SqlAlchemyUsersRepository

_LOGGER = logging.getLogger("apply_pilot.features.telegram.digest.api")

router = APIRouter(prefix="/digest", tags=["digest"])


class DigestSendResponse(BaseModel):
    """The result of a manual ``POST /digest/send`` trigger."""

    sent: int = Field(..., description="Number of digests successfully dispatched.")
    on_date: date = Field(..., description="The UTC date the digests cover.")


def build_digest_sender(
    session: Session,
    *,
    telegram_settings: TelegramSettings | None = None,
) -> DigestSender:
    """Build a :class:`DigestSender` bound to the request-scoped session.

    Factored out so the API route handler and (eventually) the
    background ``DigestRunner`` entry point share the same wiring.
    """
    match_repo = SqlVacancyMatchRepository(session=session)
    telegram_repo = SqlAlchemyTelegramAccountRepository(session=session)
    user_repo = SqlAlchemyUsersRepository(session=session)
    profile_repo = SqlSearchProfileRepository(session=session)
    stats_service = StatsService(
        match_repo=match_repo,
        telegram_account_repo=telegram_repo,
        user_repo=user_repo,
        profile_repo=profile_repo,
    )
    settings = telegram_settings or get_telegram_settings()
    bot = TelegramBot(settings=settings)
    return DigestSender(
        stats_service=stats_service,
        telegram_bot=bot,
        telegram_account_repo=telegram_repo,  # type: ignore[invalid-argument-type]
    )


def get_digest_sender(session: Session = Depends(get_db)) -> DigestSender:  # type: ignore[assignment]  # noqa: B008
    """FastAPI dependency: build a :class:`DigestSender` for the current request."""
    return build_digest_sender(session=session)


@router.post(
    "/send",
    response_model=DigestSendResponse,
    status_code=status.HTTP_200_OK,
    responses={500: {"description": "Internal error during digest dispatch"}},
)
def send_digest_now(
    sender: DigestSender = Depends(get_digest_sender),  # type: ignore[valid-type]  # noqa: B008
) -> DigestSendResponse:
    """Trigger a one-shot digest broadcast.

    Returns the number of users the digest was sent to. The
    ``on_date`` field echoes the date the digest covers (today in
    UTC).
    """
    from datetime import UTC, datetime

    target_date = datetime.now(UTC).date()
    sent = _run_send(sender, on_date=target_date)
    return DigestSendResponse(sent=sent, on_date=target_date)


def _run_send(sender: DigestSender, *, on_date: date) -> int:
    """Bridge the sync FastAPI handler to the async :class:`DigestSender`.

    FastAPI's threadpool offloads sync handlers; we hand the coroutine
    to ``asyncio.run`` here so the route stays simple. Tests bypass
    this and call ``DigestSender.send_to_all_users`` directly.
    """
    import asyncio

    _LOGGER.info(
        "digest.send.requested",
        extra={"event": "digest.send.requested", "on_date": on_date.isoformat()},
    )
    try:
        return asyncio.run(sender.send_to_all_users(on_date=on_date))
    except Exception:
        _LOGGER.exception(
            "digest.send.failed",
            extra={"event": "digest.send.failed", "on_date": on_date.isoformat()},
        )
        raise


__all__ = ["DigestSendResponse", "build_digest_sender", "get_digest_sender", "router"]
