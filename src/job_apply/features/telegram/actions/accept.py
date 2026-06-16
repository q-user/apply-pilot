"""``/accept <match_id>`` Telegram action handler (M4, issue #37).

This module owns the use-case for marking a :class:`VacancyMatch` as
accepted by the user from a Telegram chat. The handler is intentionally
thin: it resolves the local user from the Telegram account link, asks
the :class:`MatchService` to perform the state change (which enforces
ownership and status validation), records a ``MATCH_ACCEPTED`` audit
event, and returns a :class:`SendMessageRequest` for the chat.

The handler never talks to the network or to the SQLAlchemy session
directly — every collaborator is collaborator-injected so the vertical
slice can be exercised end-to-end with the in-memory fakes.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from job_apply.features.audit.models import AuditEventType
from job_apply.features.audit.service import AuditService
from job_apply.features.matches.models import MatchStatus
from job_apply.features.matches.service import (
    MatchNotFoundError,
    MatchOwnershipError,
    MatchService,
)
from job_apply.features.telegram.dto import SendMessageRequest
from job_apply.features.telegram.repository import TelegramAccountRepository

_LOGGER = logging.getLogger("job_apply.features.telegram.actions.accept")


# ---------------------------------------------------------------------------
# Allowed source statuses
# ---------------------------------------------------------------------------
#
# A match can only be accepted from these source states. Accepting a
# match that is already accepted, rejected, applied, or dismissed is
# refused with a friendly "cannot accept from status X" message instead
# of a silent no-op.
_ALLOWED_ACCEPT_SOURCES: frozenset[str] = frozenset(
    {
        MatchStatus.NEW.value,
        MatchStatus.SCORED.value,
        MatchStatus.REVIEW.value,
    }
)


# ---------------------------------------------------------------------------
# Command DTO and parser
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AcceptCommand:
    """The parsed ``/accept`` command.

    * ``match_id`` — the target :class:`VacancyMatch` UUID, parsed from
      the first positional argument. The parser rejects non-UUID input
      so the caller can show a usage hint instead of crashing.
    * ``raw_args`` — the raw trailing text after the ``/accept`` token,
      kept so the dispatcher can echo the user's input back when
      showing help or an error message.
    """

    match_id: uuid.UUID
    raw_args: str


def parse_accept_command(text: str) -> AcceptCommand | None:
    """Parse a ``/accept ...`` text message into an :class:`AcceptCommand`.

    Returns ``None`` for any of:

    * the command has no positional argument (caller shows usage);
    * the first positional argument is not a valid UUID (caller shows
      usage);
    * the text does not start with ``/accept``.

    The trailing text after the UUID is preserved in ``raw_args`` so
    the dispatcher can show the user's input back to them. The body is
    stripped; whitespace-only input is treated as absent.
    """
    stripped = text.strip()
    if not stripped.startswith("/accept"):
        return None

    # Strip the command token and the leading whitespace.
    body = stripped[len("/accept") :].strip()
    if not body:
        return None

    parts = body.split(maxsplit=1)
    raw_id = parts[0]
    try:
        match_id = uuid.UUID(raw_id)
    except ValueError:
        return None

    return AcceptCommand(match_id=match_id, raw_args=body)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class AcceptActionHandler:
    """Handle the ``/accept <match_id>`` Telegram command.

    Collaborators are injected through the constructor. ``handle`` is
    a regular method (not ``async``) because the current implementation
    is fully in-process: ``MatchService``, ``AuditService`` and
    ``TelegramAccountRepository`` are all synchronous. When a future
    slice needs to do I/O (call the Telegram API, push to Redis), the
    method can be promoted to ``async`` and the dispatcher updated
    accordingly — the action interface is small and the change stays
    local.

    The dispatcher (``TelegramBot``) is responsible for extracting
    ``chat_id`` and ``telegram_user_id`` from the incoming update and
    passing them in. The handler does not look at the raw update
    payload, which keeps the action slice-independent from the
    Telegram transport.
    """

    def __init__(
        self,
        *,
        match_service: MatchService,
        telegram_account_repo: TelegramAccountRepository,
        audit_service: AuditService,
    ) -> None:
        self._match_service = match_service
        self._telegram_account_repo = telegram_account_repo
        self._audit_service = audit_service

    def handle(
        self,
        *,
        chat_id: int,
        telegram_user_id: int,
        command: AcceptCommand,
    ) -> SendMessageRequest:
        """Execute the use-case and return the single chat reply."""
        account = self._telegram_account_repo.find_by_telegram_user_id(telegram_user_id)
        if account is None:
            return SendMessageRequest(
                chat_id=chat_id,
                text=(
                    "❌ This Telegram account is not linked to apply-pilot. "
                    "Use /link to connect it first."
                ),
            )

        user_id = account.user_id

        # Validate the source status before delegating to MatchService:
        # we want a dedicated "cannot accept from status X" message that
        # is friendlier than the generic ValidationError the service
        # surfaces for unknown statuses. The pre-check also avoids
        # recording an audit event for a no-op.
        existing = self._match_service.repo.get_by_id(command.match_id)
        if existing is None:
            return SendMessageRequest(
                chat_id=chat_id,
                text=(
                    f"❌ Match {command.match_id} not found. Use /list to see your current matches."
                ),
            )
        if existing.status not in _ALLOWED_ACCEPT_SOURCES:
            return SendMessageRequest(
                chat_id=chat_id,
                text=(
                    f"❌ Cannot accept match {command.match_id} — it is already "
                    f"in status '{existing.status}'."
                ),
            )

        try:
            self._match_service.update_status(
                match_id=command.match_id,
                status=MatchStatus.ACCEPTED.value,
                user_id=user_id,
            )
        except MatchOwnershipError:
            return SendMessageRequest(
                chat_id=chat_id,
                text=(
                    f"❌ Match {command.match_id} does not belong to you. "
                    "You can only accept your own matches."
                ),
            )
        except MatchNotFoundError:
            # Race: the match was deleted between the pre-check and the
            # update. Surface the same not-found message as above.
            return SendMessageRequest(
                chat_id=chat_id,
                text=f"❌ Match {command.match_id} not found.",
            )

        self._audit_service.log_event(
            AuditEventType.MATCH_ACCEPTED,
            user_id=user_id,
            details={"match_id": str(command.match_id)},
        )

        _LOGGER.info(
            "telegram.accept.success",
            extra={
                "event": "telegram.accept.success",
                "match_id": str(command.match_id),
                "user_id": str(user_id),
            },
        )
        return SendMessageRequest(
            chat_id=chat_id,
            text=(f"✅ Match {command.match_id} accepted. It is now eligible for applying."),
        )


# Help text for ``/accept``. Kept as a module constant so tests and the
# dispatcher share a single source of truth.
ACCEPT_HELP_TEXT = (
    "Usage: /accept <match_id>\n\n"
    "Mark one of your vacancy matches as accepted so it becomes eligible "
    "for the apply pipeline."
)


__all__ = [
    "ACCEPT_HELP_TEXT",
    "AcceptActionHandler",
    "AcceptCommand",
    "parse_accept_command",
]
