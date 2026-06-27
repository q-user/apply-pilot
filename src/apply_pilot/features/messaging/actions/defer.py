"""``/defer <match_id>`` messaging action handler (M4, issue #39).

This module owns the use-case for marking a :class:`VacancyMatch` as
deferred by the user from a messaging chat. Deferred is a soft
"not now, maybe later" state: the match is shelved so it stops
appearing in the daily digest, but the row is left in place so the
user can resume it later (issue #39).

The handler is intentionally thin: it resolves the local user from
the Telegram account link, asks the :class:`MatchService` to perform
the state change (which enforces ownership and status validation),
records a :class:`AuditEventType.MATCH_DEFERRED` event, and returns a
:class:`SendMessageRequest` for the chat.

The handler never talks to the network or to the SQLAlchemy session
directly — every collaborator is constructor-injected so the vertical
slice can be exercised end-to-end with the in-memory fakes.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from apply_pilot.features.audit.models import AuditEventType
from apply_pilot.features.audit.service import AuditService
from apply_pilot.features.matches.models import MatchStatus
from apply_pilot.features.matches.service import (
    MatchNotFoundError,
    MatchOwnershipError,
    MatchService,
)
from apply_pilot.features.messaging.dto import SendMessageRequest
from apply_pilot.features.messaging.protocols import MessagingAccountRepository

_LOGGER = logging.getLogger("apply_pilot.features.messaging.actions.defer")


# ---------------------------------------------------------------------------
# Allowed source statuses
# ---------------------------------------------------------------------------
#
# A match can only be deferred from these source states. Deferring a
# match that is already accepted, rejected, applied, or dismissed is
# refused with a friendly "cannot defer from status X" message instead
# of a silent no-op. ``deferred`` is in the allowed set so the user
# gets an explicit confirmation when they re-defer a row they have
# already shelved (idempotent transition: status stays ``deferred``,
# an audit event is still recorded).
_ALLOWED_DEFER_SOURCES: frozenset[str] = frozenset(
    {
        MatchStatus.NEW.value,
        MatchStatus.SCORED.value,
        MatchStatus.REVIEW.value,
        MatchStatus.DEFERRED.value,
    }
)


# ---------------------------------------------------------------------------
# Command DTO and parser
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeferCommand:
    """The parsed ``/defer`` command.

    * ``match_id`` — the target :class:`VacancyMatch` UUID, parsed from
      the first positional argument. The parser rejects non-UUID input
      so the caller can show a usage hint instead of crashing.
    * ``raw_args`` — the raw trailing text after the ``/defer`` token,
      kept so the dispatcher can echo the user's input back when
      showing help or an error message.
    """

    match_id: uuid.UUID
    raw_args: str


def parse_defer_command(text: str) -> DeferCommand | None:
    """Parse a ``/defer ...`` text message into a :class:`DeferCommand`.

    Returns ``None`` for any of:

    * the command has no positional argument (caller shows usage);
    * the first positional argument is not a valid UUID (caller shows
      usage);
    * the text does not start with ``/defer``.

    The trailing text after the UUID is preserved in ``raw_args`` so
    the dispatcher can show the user's input back to them. The body is
    stripped; whitespace-only input is treated as absent.
    """
    stripped = text.strip()
    if not stripped.startswith("/defer"):
        return None

    # Strip the command token and the leading whitespace.
    body = stripped[len("/defer") :].strip()
    if not body:
        return None

    parts = body.split(maxsplit=1)
    raw_id = parts[0]
    try:
        match_id = uuid.UUID(raw_id)
    except ValueError:
        return None

    return DeferCommand(match_id=match_id, raw_args=body)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class DeferActionHandler:
    """Handle the ``/defer <match_id>`` Telegram command.

    Collaborators are injected through the constructor. ``handle`` is
    a regular method (not ``async``) because the current implementation
    is fully in-process: ``MatchService``, ``AuditService`` and
    ``MessagingAccountRepository`` are all synchronous. When a future
    slice needs to do I/O (call the messaging API, push to Redis), the
    method can be promoted to ``async`` and the dispatcher updated
    accordingly — the action interface is small and the change stays
    local.

    The messaging dispatcher (``TelegramBot`` or the future MAX bot)
    is responsible for extracting ``chat_id`` and
    ``messaging_user_id`` from the incoming update and passing them
    in. The handler does not look at the raw update payload, which
    keeps the action slice-independent from the transport.
    """

    def __init__(
        self,
        *,
        match_service: MatchService,
        account_repo: MessagingAccountRepository,
        audit_service: AuditService,
    ) -> None:
        self._match_service = match_service
        self._account_repo = account_repo
        self._audit_service = audit_service

    def handle(
        self,
        *,
        chat_id: int,
        messaging_user_id: int,
        command: DeferCommand,
    ) -> SendMessageRequest:
        """Execute the use-case and return the single chat reply."""
        account = self._account_repo.find_by_external_user_id(messaging_user_id)
        if account is None:
            return SendMessageRequest(
                chat_id=chat_id,
                text=(
                    "❌ This messaging account is not linked to apply-pilot. "
                    "Use /link to connect it first."
                ),
            )

        user_id = account.user_id

        # Validate the source status before delegating to MatchService:
        # we want a dedicated "cannot defer from status X" message that
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
        if existing.status not in _ALLOWED_DEFER_SOURCES:
            return SendMessageRequest(
                chat_id=chat_id,
                text=(
                    f"❌ Cannot defer match {command.match_id} — it is already "
                    f"in status '{existing.status}'."
                ),
            )

        try:
            self._match_service.update_status(
                match_id=command.match_id,
                status=MatchStatus.DEFERRED.value,
                user_id=user_id,
            )
        except MatchOwnershipError:
            return SendMessageRequest(
                chat_id=chat_id,
                text=(
                    f"❌ Match {command.match_id} does not belong to you. "
                    "You can only defer your own matches."
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
            AuditEventType.MATCH_DEFERRED,
            user_id=user_id,
            details={"match_id": str(command.match_id)},
        )

        _LOGGER.info(
            "messaging.defer.success",
            extra={
                "event": "messaging.defer.success",
                "match_id": str(command.match_id),
                "user_id": str(user_id),
            },
        )
        return SendMessageRequest(
            chat_id=chat_id,
            text=(
                f"⏸️ Match {command.match_id} deferred. "
                "It will not show up in your daily digest — use /defer again "
                "or /accept when you're ready to look at it."
            ),
        )


# Help text for ``/defer``. Kept as a module constant so tests and the
# dispatcher share a single source of truth.
DEFER_HELP_TEXT = (
    "Usage: /defer <match_id>\n\n"
    "Shelve one of your vacancy matches for later. Deferred matches "
    "are hidden from the daily digest but stay on the row so you "
    "can resume them whenever you want."
)


__all__ = [
    "DEFER_HELP_TEXT",
    "DeferActionHandler",
    "DeferCommand",
    "parse_defer_command",
]
