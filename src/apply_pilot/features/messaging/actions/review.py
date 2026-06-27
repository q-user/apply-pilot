"""``/review <match_id>`` messaging action handler (M4, issue #36).

This module owns the use-case for rendering a vacancy review card in a
messaging chat. The handler is intentionally thin: it resolves the
local user from the Telegram account link, loads the target match
(which ``MatchService`` uses to enforce ownership), loads the
underlying :class:`Vacancy` and the latest :class:`CoverLetterDraft`
(if any), asks the pure :func:`render_review_card` function to format
the card, and returns a :class:`SendMessageRequest` for the chat.

The handler never talks to the network or to the SQLAlchemy session
directly — every collaborator is collaborator-injected so the vertical
slice can be exercised end-to-end with the in-memory fakes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from apply_pilot.features.cover_letter.models import CoverLetterDraft
from apply_pilot.features.cover_letter.repository import CoverLetterDraftRepository
from apply_pilot.features.matches.service import (
    MatchNotFoundError,
    MatchOwnershipError,
    MatchService,
)
from apply_pilot.features.messaging.dto import SendMessageRequest
from apply_pilot.features.messaging.protocols import MessagingAccountRepository
from apply_pilot.features.sources.models import Vacancy
from apply_pilot.features.sources.repository import VacancyRepository

# ---------------------------------------------------------------------------
# MarkdownV2 escaping
# ---------------------------------------------------------------------------
#
# The renderer formats the card as Telegram MarkdownV2, which treats a
# handful of characters as syntax. A stray asterisk or underscore in a
# job title would otherwise be interpreted as emphasis by the parser
# and break the layout. ``_escape_markdownv2`` prefixes each special
# character with a backslash so the literal text survives the round
# trip through the parser.
_MARKDOWNV2_SPECIAL_CHARS: frozenset[str] = frozenset("_*[]()~`>#+-=|{}.!")


def _escape_markdownv2(text: str | None) -> str:
    """Return ``text`` with MarkdownV2 special characters backslash-escaped.

    ``None`` is rendered as the empty string so the caller can use a
    single ``_escape_markdownv2(value)`` for every field regardless of
    whether it was populated by the source.
    """
    if text is None:
        return ""
    out: list[str] = []
    for ch in text:
        if ch in _MARKDOWNV2_SPECIAL_CHARS:
            out.append("\\")
        out.append(ch)
    return "".join(out)


# Truncation marker appended to explanations longer than
# :data:`_MAX_EXPLANATION_CHARS`. The three ASCII dots are kept
# literal (no Unicode ellipsis) so the truncation is visible
# regardless of the user's font / locale and is also easy to
# assert in tests without dealing with character classes.
_TRUNCATION_MARKER: str = "..."
_MAX_EXPLANATION_CHARS: int = 200


# ---------------------------------------------------------------------------
# Card rendering
# ---------------------------------------------------------------------------
#
# The card is rendered as a single MarkdownV2 string. Width is kept
# below ~30 columns so the message reads well on a phone screen. The
# sections are stable so a future reformat stays a deliberate,
# reviewable change.


@runtime_checkable
class _ReviewableMatch(Protocol):
    """The minimal match surface the renderer depends on.

    The Protocol keeps the renderer independent of the SQLAlchemy
    model — tests can pass any object that exposes the four
    attributes the card needs.
    """

    id: uuid.UUID
    status: str
    score: int | None
    explanation: str | None


def _format_salary(vacancy: Vacancy) -> str:
    """Render the salary range as a single human-readable line.

    Both bounds missing → ``"(unspecified)"``. Only one bound → that
    bound alone with a "from" / "up to" prefix. Both → ``"<min> - <max>
    <currency>"``. The currency is always appended when at least one
    bound is known so the reader never has to guess.
    """
    if vacancy.salary_from is None and vacancy.salary_to is None:
        return "(unspecified)"
    currency = vacancy.salary_currency or ""
    if vacancy.salary_from is None:
        return f"up to {vacancy.salary_to} {currency}".strip()
    if vacancy.salary_to is None:
        return f"from {vacancy.salary_from} {currency}".strip()
    return f"{vacancy.salary_from}-{vacancy.salary_to} {currency}".strip()


def _format_skills(skills: list[str] | None) -> str:
    """Render the ``skills`` list as a comma-separated line.

    Empty / ``None`` → ``"(none)"`` so the card always shows the
    section even when the source vacancy has no skills.
    """
    if not skills:
        return "(none)"
    return ", ".join(skills)


def _format_score(score: int | None) -> str:
    """Render the LLM score as ``"<n>/100"`` or ``"N/A"``.

    The ``/100`` denominator is a deliberate convention — the scoring
    service stores raw integers, and the user-facing card makes the
    scale explicit so the score is interpretable on its own.
    """
    if score is None:
        return "N/A"
    return f"{score}/100"


def _format_explanation(text: str | None) -> str:
    """Render the LLM explanation, truncating to a stable line length.

    Explanations over :data:`_MAX_EXPLANATION_CHARS` characters are
    clipped and a trailing :data:`_TRUNCATION_MARKER` is appended so
    the user knows the full text was longer. ``None`` renders as
    ``"(no explanation)"`` so the section is always present.
    """
    if not text:
        return "(no explanation)"
    if len(text) <= _MAX_EXPLANATION_CHARS:
        return text
    return text[: _MAX_EXPLANATION_CHARS - len(_TRUNCATION_MARKER)].rstrip() + _TRUNCATION_MARKER


def render_review_card(
    match: _ReviewableMatch,
    vacancy: Vacancy,
    cover_letter: CoverLetterDraft | None = None,
) -> str:
    """Render a MarkdownV2 vacancy review card.

    The card is a single Telegram message. It lists the vacancy's
    title, employer, location, salary, the LLM score, the (possibly
    truncated) explanation, the vacancy's skills, the cover-letter
    status, and a footer with the next-step commands (``/accept``,
    ``/reject``, ``/defer``, ``/regenerate``). All user-supplied
    text is MarkdownV2-escaped so the message is safe to send with
    ``parse_mode="MarkdownV2"`` and also renders cleanly as plain
    text.

    The function is pure: it has no I/O, no clock, and no
    configuration. Tests pin the exact output structure so a future
    reformat is a deliberate, reviewable change.
    """
    title = _escape_markdownv2(vacancy.title)
    employer = _escape_markdownv2(vacancy.employer_name) or "(unspecified)"
    location = _escape_markdownv2(vacancy.location) or "(unspecified)"
    salary = _format_salary(vacancy)
    score = _format_score(match.score)
    explanation = _format_explanation(match.explanation)
    skills = _format_skills(vacancy.skills)

    cover_letter_status = "ready" if cover_letter is not None else "not generated"

    match_id = _escape_markdownv2(str(match.id))

    return (
        f"📄 *Vacancy review*\n"
        f"\n"
        f"*Title:* {title}\n"
        f"*Employer:* {employer}\n"
        f"*Location:* {location}\n"
        f"*Salary:* {salary}\n"
        f"\n"
        f"*Match score:* {score}\n"
        f"*Why:* {explanation}\n"
        f"*Skills:* {skills}\n"
        f"\n"
        f"*Cover letter:* {cover_letter_status}\n"
        f"\n"
        f"*Actions:*\n"
        f"/accept {match_id}\n"
        f"/reject {match_id} \\[reason\\]\n"
        f"/defer {match_id}\n"
        f"/regenerate {match_id}\n"
    )


# ---------------------------------------------------------------------------
# Command DTO and parser
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewCommand:
    """The parsed ``/review`` command.

    * ``match_id`` — the target :class:`VacancyMatch` UUID, parsed
      from the first positional argument. The parser rejects
      non-UUID input so the caller can show a usage hint instead of
      crashing.
    """

    match_id: uuid.UUID


def parse_review_command(text: str) -> ReviewCommand | None:
    """Parse a ``/review ...`` text message into a :class:`ReviewCommand`.

    Returns ``None`` for any of:

    * the command has no positional argument (caller shows usage);
    * the first positional argument is not a valid UUID (caller
      shows usage);
    * the text does not start with ``/review``.
    """
    stripped = text.strip()
    if not stripped.startswith("/review"):
        return None

    body = stripped[len("/review") :].strip()
    if not body:
        return None

    parts = body.split(maxsplit=1)
    raw_id = parts[0]
    try:
        match_id = uuid.UUID(raw_id)
    except ValueError:
        return None

    return ReviewCommand(match_id=match_id)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class ReviewActionHandler:
    """Handle the ``/review <match_id>`` Telegram command.

    Collaborators are injected through the constructor. ``handle`` is
    a regular method (not ``async``) because the current
    implementation is fully in-process: ``MatchService``,
    :class:`VacancyRepository`, :class:`CoverLetterDraftRepository`
    and :class:`MessagingAccountRepository` are all synchronous. When
    a future slice needs to do I/O (call the messaging API, push to
    Redis), the method can be promoted to ``async`` and the
    dispatcher updated accordingly — the action interface is small
    and the change stays local.

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
        vacancy_repo: VacancyRepository,
        cover_letter_repo: CoverLetterDraftRepository,
        account_repo: MessagingAccountRepository,
    ) -> None:
        self._match_service = match_service
        self._vacancy_repo = vacancy_repo
        self._cover_letter_repo = cover_letter_repo
        self._account_repo = account_repo

    def handle(
        self,
        *,
        chat_id: int,
        messaging_user_id: int,
        match_id: uuid.UUID,
    ) -> SendMessageRequest:
        """Execute the use-case and return the single chat reply.

        The handler performs a friendly pre-check on the match
        before delegating to :class:`MatchService` so the user gets
        a clear "match not found" / "does not belong to you"
        message rather than a generic exception. Read-only actions
        do not record an audit event — the user can browse their
        queue as much as they want.
        """
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

        # MatchService.get enforces ownership and raises a domain
        # error when the row is missing or belongs to another user.
        try:
            self._match_service.get(match_id, user_id=user_id)
        except MatchNotFoundError:
            return SendMessageRequest(
                chat_id=chat_id,
                text=(f"❌ Match {match_id} not found. Use /list to see your current matches."),
            )
        except MatchOwnershipError:
            return SendMessageRequest(
                chat_id=chat_id,
                text=(
                    f"❌ Match {match_id} does not belong to you. "
                    "You can only review your own matches."
                ),
            )

        # Re-fetch the row through the repository so the renderer
        # gets the freshest copy of the match (the MatchService.get
        # call above returned a DTO, not the ORM row).
        match_row = self._match_service.repo.get_by_id(match_id)
        if match_row is None:  # pragma: no cover — defensive: race with the get
            return SendMessageRequest(
                chat_id=chat_id,
                text=f"❌ Match {match_id} not found.",
            )

        vacancy = self._vacancy_repo.get_by_id(match_row.vacancy_id)
        if vacancy is None:
            # The vacancy row is gone (deleted by the ingest pipeline)
            # — surface the situation as an error rather than render a
            # half-empty card.
            return SendMessageRequest(
                chat_id=chat_id,
                text=(f"❌ Match {match_id} references a vacancy that is no longer available."),
            )

        cover_letter = self._cover_letter_repo.get_by_match(match_id)

        card = render_review_card(
            match=match_row,
            vacancy=vacancy,
            cover_letter=cover_letter,
        )

        return SendMessageRequest(chat_id=chat_id, text=card)


# Help text for ``/review``. Kept as a module constant so tests and
# the dispatcher share a single source of truth.
REVIEW_HELP_TEXT = (
    "Usage: /review <match_id>\n\n"
    "Render a vacancy review card for one of your matches: title, "
    "employer, location, salary, match score, explanation, skills, "
    "cover-letter status, and the next-step action commands."
)


__all__ = [
    "REVIEW_HELP_TEXT",
    "ReviewActionHandler",
    "ReviewCommand",
    "parse_review_command",
    "render_review_card",
]


if TYPE_CHECKING:  # pragma: no cover
    from apply_pilot.features.matches.service import MatchService
