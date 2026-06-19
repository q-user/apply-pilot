"""``/accept <match_id>`` messaging action handler (M4, issue #37 + issue #41).

This module owns the use-case for marking a :class:`VacancyMatch` as
accepted by the user from a messaging chat. The handler is intentionally
thin: it resolves the local user from the Telegram account link, asks
the :class:`MatchService` to perform the state change (which enforces
ownership and status validation), records a ``MATCH_ACCEPTED`` audit
event, optionally enqueues an :class:`ApplyJob` through the injected
:class:`ApplyJobEnqueuer` (issue #41), and returns a
:class:`SendMessageRequest` for the chat.

The handler never talks to the network or to the SQLAlchemy session
directly — every collaborator is collaborator-injected so the vertical
slice can be exercised end-to-end with the in-memory fakes.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Protocol

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
from apply_pilot.features.writing_style_memory.service import StyleMemoryService

_LOGGER = logging.getLogger("apply_pilot.features.messaging.actions.accept")


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
# ApplyJob enqueue dependency (issue #41)
# ---------------------------------------------------------------------------


class ApplyJobEnqueuer(Protocol):
    """Minimal Protocol for the apply-queue dependency of :class:`AcceptActionHandler`.

    The actual :class:`apply_worker.ApplyJobService` (landed in #43) will
    satisfy this protocol structurally — it exposes
    ``enqueue_for_match(match_id)``. The Protocol keeps the accept action
    decoupled from the apply worker package so the two slices can ship
    independently and tests can wire a tiny recording fake.
    """

    def enqueue_for_match(self, match_id: uuid.UUID) -> object:
        """Enqueue an apply job for ``match_id`` and return the new job."""
        ...


# ---------------------------------------------------------------------------
# Cover-letter draft dependency (M8, issue #66)
# ---------------------------------------------------------------------------


class CoverLetterDraftSource(Protocol):
    """Minimal protocol for the cover-letter draft dependency.

    The handler only needs ``get_by_match`` to look up the body of the
    accepted letter; the protocol is duck-typed so the handler stays
    decoupled from the ``cover_letter`` package and avoids the import
    cycle through ``matches.service`` / ``telegram``.
    """

    def get_by_match(self, match_id: uuid.UUID) -> object | None:
        """Return the draft for ``match_id`` or ``None`` if absent.

        The returned object is duck-typed; the handler only reads
        ``.content`` and ``.id`` when present.
        """
        ...


class _NoopDraftSource:
    """Sentinel for "no cover-letter source available"."""

    def get_by_match(self, match_id: uuid.UUID) -> object | None:
        return None


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

    ``apply_job_enqueuer`` is optional: when it is ``None`` the accept
    still works but no :class:`ApplyJob` is scheduled. This lets the
    slice ship before issue #43 (the apply queue model) lands — once
    :class:`apply_worker.ApplyJobService` is available, the wiring code
    in :mod:`apply_pilot.features.messaging.process` will inject it
    structurally (it satisfies :class:`ApplyJobEnqueuer`).

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
        apply_job_enqueuer: ApplyJobEnqueuer | None = None,
        style_memory_service: StyleMemoryService | None = None,
        draft_repository: CoverLetterDraftSource | None = None,
    ) -> None:
        self._match_service = match_service
        self._account_repo = account_repo
        self._audit_service = audit_service
        self._apply_job_enqueuer = apply_job_enqueuer
        # The style memory layer (M8, issue #66) is optional: when
        # ``None`` the accept still works but no style memory entry is
        # recorded. The ``draft_repository`` is the cover-letter
        # gateway the service uses to fetch the accepted letter's
        # body; ``None`` falls back to the no-op source.
        self._style_memory_service = style_memory_service
        self._draft_repository: CoverLetterDraftSource = (
            draft_repository if draft_repository is not None else _NoopDraftSource()
        )

    def handle(
        self,
        *,
        chat_id: int,
        messaging_user_id: int,
        command: AcceptCommand,
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

        # Best-effort enqueue of an ApplyJob for the apply worker (issue #41).
        # The accept must not fail if the queue is down: the user has
        # already accepted the match and the worker can be re-driven by a
        # periodic reconciler (out of scope for this slice). The audit
        # event below records whether the enqueue succeeded so operators
        # can spot patterns of failures.
        audit_details: dict[str, object] = {"match_id": str(command.match_id)}
        if self._apply_job_enqueuer is not None:
            try:
                self._apply_job_enqueuer.enqueue_for_match(command.match_id)
            except Exception as exc:
                _LOGGER.exception(
                    "messaging.accept.enqueue_failed",
                    extra={
                        "event": "messaging.accept.enqueue_failed",
                        "match_id": str(command.match_id),
                        "user_id": str(user_id),
                    },
                )
                audit_details["apply_job_enqueued"] = False
                audit_details["apply_job_enqueue_failed"] = True
                audit_details["apply_job_enqueue_error"] = str(exc)
            else:
                audit_details["apply_job_enqueued"] = True

        self._audit_service.log_event(
            AuditEventType.MATCH_ACCEPTED,
            user_id=user_id,
            details=audit_details,
        )

        # Best-effort style-memory recording (M8, issue #66). The
        # accept must not fail if the style memory layer is down: the
        # match is already in the accepted state, the audit event
        # is recorded, and a future reconciler (out of scope for the
        # slice) can backfill the entry. We do not surface the
        # failure to the chat reply.
        if self._style_memory_service is not None:
            self._record_style_memory(user_id=user_id, match_id=command.match_id)

        _LOGGER.info(
            "messaging.accept.success",
            extra={
                "event": "messaging.accept.success",
                "match_id": str(command.match_id),
                "user_id": str(user_id),
            },
        )
        return SendMessageRequest(
            chat_id=chat_id,
            text=(f"✅ Match {command.match_id} accepted. It is now eligible for applying."),
        )

    def _record_style_memory(
        self,
        *,
        user_id: uuid.UUID,
        match_id: uuid.UUID,
    ) -> None:
        """Best-effort style-memory recording for an accepted match.

        The handler resolves the cover letter's body via the
        injected :class:`CoverLetterDraftSource`. When the draft
        cannot be found (no generation yet) the recording is
        skipped silently — the ``/regenerate`` flow can re-trigger
        the memory when the user accepts again with a draft in
        place.

        Any failure inside the style-memory layer is logged and
        swallowed so the accept command can still succeed; the
        audit event above is the source of truth for "match
        accepted" state.
        """
        try:
            draft = self._draft_repository.get_by_match(match_id)
        except Exception:
            _LOGGER.exception(
                "messaging.accept.style_memory_draft_lookup_failed",
                extra={
                    "event": "messaging.accept.style_memory_draft_lookup_failed",
                    "match_id": str(match_id),
                    "user_id": str(user_id),
                },
            )
            return
        if draft is None:
            return
        body = str(getattr(draft, "content", "") or "").strip()
        if not body:
            return
        # ``cover_letter_id`` must be the FK target — the draft's own
        # id — so the ``style_memory_entries.cover_letter_id`` row is
        # consistent with the ``cover_letter_drafts`` table. Use a
        # strict ``is None`` check: a falsy but non-None ``id`` (e.g.
        # the zero UUID ``00000000-0000-0000-0000-000000000000``) is
        # a real value and must not silently fall back to ``match_id``,
        # which would break the FK constraint. The zero UUID is
        # also rejected as a sentinel "missing" value — it cannot
        # satisfy the ``cover_letter_drafts.id`` FK in production.
        cover_letter_id = getattr(draft, "id", None)
        if cover_letter_id is None or cover_letter_id == uuid.UUID(int=0):
            return
        try:
            self._style_memory_service.record_accepted_letter(  # type: ignore[union-attr]
                user_id=user_id,
                cover_letter_id=cover_letter_id,
                letter_text=body,
            )
        except Exception:
            _LOGGER.exception(
                "messaging.accept.style_memory_failed",
                extra={
                    "event": "messaging.accept.style_memory_failed",
                    "match_id": str(match_id),
                    "user_id": str(user_id),
                },
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
    "ApplyJobEnqueuer",
    "parse_accept_command",
]
