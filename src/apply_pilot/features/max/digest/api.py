"""FastAPI router for the MAX daily digest slice.

Single endpoint — ``POST /digest/max/send`` — triggers a broadcast
for manual testing and operations. Auth is intentionally not enforced
yet (mirrors the Telegram digest API): M1 did not yet ship role-based
access control, and the endpoint is meant for an internal trigger
(cron / on-call) rather than a user-facing button. A future slice will
guard it behind an admin role.
"""

from __future__ import annotations

import logging
from datetime import date

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from apply_pilot.config import MaxSettings, get_max_settings
from apply_pilot.db import get_db
from apply_pilot.features.matches.repository import SqlVacancyMatchRepository
from apply_pilot.features.max.bot import MaxBot
from apply_pilot.features.max.digest import MaxDigestSender, MaxStatsService
from apply_pilot.features.max.linking import MaxLinkingService
from apply_pilot.features.max.repository import (
    InMemoryMaxAccountRepository,
    SqlAlchemyMaxAccountRepository,
)
from apply_pilot.features.messaging.actions.accept import AcceptActionHandler
from apply_pilot.features.messaging.actions.defer import DeferActionHandler
from apply_pilot.features.messaging.actions.regenerate import RegenerateActionHandler
from apply_pilot.features.messaging.actions.reject import RejectActionHandler
from apply_pilot.features.messaging.actions.review import ReviewActionHandler
from apply_pilot.features.search_profiles.repository import SqlSearchProfileRepository
from apply_pilot.features.users.repository import SqlAlchemyUsersRepository

_LOGGER = logging.getLogger("apply_pilot.features.max.digest.api")

router = APIRouter(prefix="/digest/max", tags=["digest-max"])


class MaxDigestSendResponse(BaseModel):
    """The result of a manual ``POST /digest/max/send`` trigger."""

    sent: int = Field(..., description="Number of digests successfully dispatched.")
    on_date: date = Field(..., description="The UTC date the digests cover.")


def _bypass_action_handlers() -> tuple[
    AcceptActionHandler,
    DeferActionHandler,
    RejectActionHandler,
    ReviewActionHandler,
    RegenerateActionHandler,
]:
    """Build lightweight handler instances for the digest bot.

    Each handler is instantiated through its real ``__init__`` with
    :class:`_NoopDependency` sentinels. The MAX digest only forwards
    :meth:`MaxBot.send_message` and never invokes any action handler.
    """
    noop = _NoopDependency()
    return (
        AcceptActionHandler(match_service=noop, account_repo=noop, audit_service=noop),
        DeferActionHandler(match_service=noop, account_repo=noop, audit_service=noop),
        RejectActionHandler(match_service=noop, account_repo=noop, audit_service=noop),
        ReviewActionHandler(
            match_service=noop,
            vacancy_repo=noop,
            cover_letter_repo=noop,
            account_repo=noop,
        ),
        RegenerateActionHandler(
            match_service=noop,
            vacancy_repo=noop,
            cover_letter_repo=noop,
            account_repo=noop,
        ),
    )


class _NoopDependency:
    """Sentinel stub satisfying any Protocol via ``__getattr__``.

    Returns its own instance from every attribute access, so accidental
    invocation short-circuits without raising ``AttributeError`` on
    ``None``-typed attributes. Safe under structural duck-typing: every
    Protocol method call returns another ``_NoopDependency``.
    """

    def __getattr__(self, _attr: str) -> type[_NoopDependency]:
        return _NoopDependency


def build_max_digest_sender(
    session: Session,
    *,
    max_settings: MaxSettings | None = None,
) -> MaxDigestSender:
    """Build a :class:`MaxDigestSender` bound to the request-scoped session.

    Factored out so the API route handler and (eventually) the
    background ``MaxDigestRunner`` entry point share the same wiring.

    The :class:`MaxBot` is built with bare handler instances because
    the digest sender never invokes any handler method — it only
    forwards rendered text via :meth:`MaxBot.send_message`. The
    in-memory account repository and a fresh linking service cover
    the constructor's other required arguments without dragging in
    the full action-handler dependency graph.
    """
    match_repo = SqlVacancyMatchRepository(session_factory=lambda: session)
    max_repo = SqlAlchemyMaxAccountRepository(session=session)
    user_repo = SqlAlchemyUsersRepository(session=session)
    profile_repo = SqlSearchProfileRepository(session_factory=lambda: session)
    stats_service = MaxStatsService(
        match_repo=match_repo,
        max_account_repo=max_repo,
        user_repo=user_repo,
        profile_repo=profile_repo,
    )
    settings = max_settings or get_max_settings()
    accept, defer, reject, review, regenerate = _bypass_action_handlers()
    bot = MaxBot(
        settings=settings,
        account_repo=InMemoryMaxAccountRepository(),
        linking_service=MaxLinkingService(),
        accept_handler=accept,  # type: ignore[arg-type]
        defer_handler=defer,  # type: ignore[arg-type]
        reject_handler=reject,  # type: ignore[arg-type]
        review_handler=review,  # type: ignore[arg-type]
        regenerate_handler=regenerate,  # type: ignore[arg-type]
    )
    return MaxDigestSender(
        stats_service=stats_service,
        max_bot=bot,
        max_account_repo=max_repo,
    )


def get_max_digest_sender(  # type: ignore[assignment]
    session: Session = Depends(get_db),  # noqa: B008
) -> MaxDigestSender:
    """FastAPI dependency: build a :class:`MaxDigestSender` for the current request."""
    return build_max_digest_sender(session=session)


@router.post(
    "/send",
    response_model=MaxDigestSendResponse,
    status_code=status.HTTP_200_OK,
    responses={500: {"description": "Internal error during digest dispatch"}},
)
def send_max_digest_now(
    sender: MaxDigestSender = Depends(get_max_digest_sender),  # type: ignore[valid-type]  # noqa: B008
) -> MaxDigestSendResponse:
    """Trigger a one-shot MAX digest broadcast.

    Returns the number of users the digest was sent to. The
    ``on_date`` field echoes the date the digest covers (today in
    UTC).
    """
    from datetime import UTC, datetime

    target_date = datetime.now(UTC).date()
    sent = _run_send(sender, on_date=target_date)
    return MaxDigestSendResponse(sent=sent, on_date=target_date)


def _run_send(sender: MaxDigestSender, *, on_date: date) -> int:
    """Bridge the sync FastAPI handler to the async :class:`MaxDigestSender`.

    FastAPI's threadpool offloads sync handlers; we hand the coroutine
    to ``asyncio.run`` here so the route stays simple. Tests bypass
    this and call :meth:`MaxDigestSender.send_to_all_users` directly.
    """
    import asyncio

    _LOGGER.info(
        "max.digest.send.requested",
        extra={"event": "max.digest.send.requested", "on_date": on_date.isoformat()},
    )
    try:
        return asyncio.run(sender.send_to_all_users(on_date=on_date))
    except Exception:
        _LOGGER.exception(
            "max.digest.send.failed",
            extra={"event": "max.digest.send.failed", "on_date": on_date.isoformat()},
        )
        raise


__all__ = [
    "MaxDigestSendResponse",
    "build_max_digest_sender",
    "get_max_digest_sender",
    "router",
]
