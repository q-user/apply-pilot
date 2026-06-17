"""``/reject <match_id> [reason]`` Telegram action handler (M4, issue #38).

This module owns the use-case for marking a :class:`VacancyMatch` as
rejected by the user from a Telegram chat. The handler is
intentionally thin: it resolves the local user from the Telegram
account link, asks the :class:`MatchService` to perform the state
change (which enforces ownership and status validation), records a
:class:`VacancyMatchRejected` audit event, and returns a
:class:`SendMessageRequest` for the chat.

The handler never talks to the network or to the SQLAlchemy session
directly — every collaborator is collaborator-injected so the
vertical slice can be exercised end-to-end with the in-memory fakes.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from apply_pilot.features.audit.models import AuditEventType
from apply_pilot.features.audit.service import AuditService
from apply_pilot.features.learning.service import LearningSignalsService
from apply_pilot.features.matches.models import MatchStatus
from apply_pilot.features.matches.service import (
    MatchNotFoundError,
    MatchOwnershipError,
    MatchService,
)
from apply_pilot.features.telegram.dto import SendMessageRequest
from apply_pilot.features.telegram.repository import TelegramAccountRepository

_LOGGER = logging.getLogger("apply_pilot.features.telegram.actions.reject")


# ---------------------------------------------------------------------------
# Allowed source statuses
# ---------------------------------------------------------------------------
#
# A match can only be rejected from these source states. Rejecting a
# match that is already rejected, applied, or dismissed is rejected with
# an error so the user gets a clear hint instead of a silent no-op.
_ALLOWED_REJECT_SOURCES: frozenset[str] = frozenset(
    {
        MatchStatus.NEW.value,
        MatchStatus.SCORED.value,
        MatchStatus.REVIEW.value,
        MatchStatus.ACCEPTED.value,
    }
)


# ---------------------------------------------------------------------------
# Command DTO and parser
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RejectCommand:
    """The parsed ``/reject`` command.

    * ``match_id`` — the target :class:`VacancyMatch` UUID, parsed from
      the first positional argument. The parser rejects non-UUID input
      so the caller can show a usage hint instead of crashing.
    * ``reason`` — optional free-form reason supplied as the trailing
      args. Stored on the audit event only; the match row itself does
      not carry a rejection reason.
    """

    match_id: uuid.UUID
    reason: str | None = None


def parse_reject_command(text: str) -> RejectCommand | None:
    """Parse a ``/reject ...`` text message into a :class:`RejectCommand`.

    Returns ``None`` for any of:

    * the command has no positional argument (caller shows usage);
    * the first positional argument is not a valid UUID (caller shows
      usage);
    * the text does not start with ``/reject``.

    Trailing arguments after the UUID are joined with single spaces and
    returned as the optional ``reason``. The text is stripped and
    collapsed; a reason of only whitespace is treated as absent.
    """
    stripped = text.strip()
    if not stripped.startswith("/reject"):
        return None

    # Strip the command token and the leading whitespace.
    body = stripped[len("/reject") :].strip()
    if not body:
        return None

    parts = body.split(maxsplit=1)
    raw_id = parts[0]
    try:
        match_id = uuid.UUID(raw_id)
    except ValueError:
        return None

    reason: str | None = None
    if len(parts) > 1:
        reason_text = " ".join(parts[1].split())
        if reason_text:
            reason = reason_text

    return RejectCommand(match_id=match_id, reason=reason)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class RejectActionHandler:
    """Handle the ``/reject <match_id> [reason]`` Telegram command.

    Collaborators are injected through the constructor. ``handle`` is
    a regular method (not ``async``) because the current
    implementation is fully in-process: ``MatchService``,
    ``AuditService`` and ``TelegramAccountRepository`` are all
    synchronous. When a future slice needs to do I/O (call the
    Telegram API, push to Redis), the method can be promoted to
    ``async`` and the dispatcher updated accordingly — the action
    interface is small and the change stays local.

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
        learning_signals: LearningSignalsService | None = None,
    ) -> None:
        self._match_service = match_service
        self._telegram_account_repo = telegram_account_repo
        self._audit_service = audit_service
        # Optional so the slice can be exercised in isolation (the
        # public tests build the handler without a learning service).
        # When present, every successful reject records a structured
        # signal so future prompt tuning has data to learn from.
        self._learning_signals = learning_signals

    def handle(
        self,
        *,
        chat_id: int,
        telegram_user_id: int,
        command: RejectCommand,
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
        # we want a dedicated "cannot reject from status X" message that
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
        if existing.status not in _ALLOWED_REJECT_SOURCES:
            return SendMessageRequest(
                chat_id=chat_id,
                text=(
                    f"❌ Cannot reject match {command.match_id} — it is already "
                    f"in status '{existing.status}'."
                ),
            )

        try:
            self._match_service.update_status(
                match_id=command.match_id,
                status=MatchStatus.REJECTED.value,
                user_id=user_id,
            )
        except MatchOwnershipError:
            return SendMessageRequest(
                chat_id=chat_id,
                text=(
                    f"❌ Match {command.match_id} does not belong to you. "
                    "You can only reject your own matches."
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
            AuditEventType.VACANCY_MATCH_REJECTED,
            user_id=user_id,
            details={
                "match_id": str(command.match_id),
                "reason": command.reason,
            },
        )

        # Record a structured learning signal alongside the audit
        # event. ``score`` / ``prompt_version`` are ``None`` on a
        # freshly-ingested match; the service persists the
        # ``None``s as-is so future readers can tell the difference
        # between "no score yet" and "score was 0". ``score`` is
        # cast to float so the column type matches the schema
        # (``Float``, not ``Integer``).
        if self._learning_signals is not None:
            self._learning_signals.record_rejection(
                user_id=user_id,
                match_id=command.match_id,
                vacancy_id=existing.vacancy_id,
                search_profile_id=existing.search_profile_id,
                reason=command.reason,
                score=float(existing.score) if existing.score is not None else None,
                prompt_version=existing.prompt_version,
            )

        suffix = f" Reason: {command.reason}" if command.reason else ""
        _LOGGER.info(
            "telegram.reject.success",
            extra={
                "event": "telegram.reject.success",
                "match_id": str(command.match_id),
                "user_id": str(user_id),
            },
        )
        return SendMessageRequest(
            chat_id=chat_id,
            text=(f"❌ Match {command.match_id} rejected. Won't be shown again.{suffix}"),
        )


# Help text for ``/reject``. Kept as a module constant so tests and the
# dispatcher share a single source of truth.
REJECT_HELP_TEXT = (
    "Usage: /reject <match_id> [reason]\n\n"
    "Mark one of your vacancy matches as rejected so it stops appearing in "
    "your review queue. Optionally include a short reason — it is stored "
    "on the audit log only."
)


__all__ = [
    "REJECT_HELP_TEXT",
    "RejectActionHandler",
    "RejectCommand",
    "parse_reject_command",
]
