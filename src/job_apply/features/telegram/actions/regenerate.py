"""/regenerate <match_id> Telegram action handler (M4, issue #40).

This module owns the use-case for refreshing a CoverLetterDraft by
the user from a Telegram chat. Regenerate re-asks the LLM to write a
new cover letter for the same VacancyMatch and updates the existing
row in place (the match_id UNIQUE constraint makes "one draft per
match" the slice's contract). The version column is bumped so the
audit log can record which iteration the user asked for.

The handler is intentionally thin: it resolves the local user from
the Telegram account link, looks up the target match to enforce
ownership, delegates the actual regeneration to
CoverLetterService.regenerate_for_match (which re-runs the LLM and
bumps the version), records a COVER_LETTER_REGENERATED audit event,
and returns a SendMessageRequest carrying a MarkdownV2 preview of the
new content for the chat.

The handler never talks to the network or to the SQLAlchemy session
directly — every collaborator is constructor-injected so the
vertical slice can be exercised end-to-end with the in-memory fakes.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from job_apply.features.audit.models import AuditEventType
from job_apply.features.audit.service import AuditService
from job_apply.features.cover_letter.service import (
    CoverLetterDependencyMissingError,
    CoverLetterService,
)
from job_apply.features.telegram.dto import SendMessageRequest
from job_apply.features.telegram.repository import TelegramAccountRepository

_LOGGER = logging.getLogger("job_apply.features.telegram.actions.regenerate")

# MarkdownV2 special characters that must be backslash-escaped in
# user-supplied text. Mirrors the set used by the review action so
# the two commands render consistently.
_MARKDOWNV2_SPECIAL_CHARS: frozenset[str] = frozenset("_*[]()~`>#+-=|{}.!")

# Truncation marker appended to cover-letter bodies longer than
# _MAX_PREVIEW_CHARS. The three ASCII dots are kept literal (no
# Unicode ellipsis) so the truncation is visible regardless of the
# user's font / locale and is also easy to assert in tests without
# dealing with character classes.
_TRUNCATION_MARKER: str = "..."

# Maximum number of characters of the regenerated body that fits in
# the Telegram preview. The cap is generous (the cover letter is
# typically ~500-1500 characters) and matches the same "preview
# slice" convention used by the review action for the LLM
# explanation.
_MAX_PREVIEW_CHARS: int = 200


def _escape_markdownv2(text: str) -> str:
    """Return text with MarkdownV2 special characters backslash-escaped.

    Empty input is returned as the empty string so the caller can use
    a single _escape_markdownv2(value) for every field regardless of
    whether the LLM produced any content.
    """
    if not text:
        return ""
    out: list[str] = []
    for ch in text:
        if ch in _MARKDOWNV2_SPECIAL_CHARS:
            out.append("\\")
        out.append(ch)
    return "".join(out)


def _format_preview(content: str) -> str:
    """Return a MarkdownV2-safe preview of the regenerated body.

    The body is escaped and truncated to _MAX_PREVIEW_CHARS
    characters; a trailing _TRUNCATION_MARKER is appended when the
    body was truncated so the user knows the full text was longer.
    The returned slice is itself MarkdownV2-safe (every special
    character in the source is backslash-escaped, including the dots
    in the truncation marker, which are also on Telegram's reserved
    list).
    """
    if len(content) <= _MAX_PREVIEW_CHARS:
        return _escape_markdownv2(content)
    head = content[: _MAX_PREVIEW_CHARS - len(_TRUNCATION_MARKER)].rstrip()
    return _escape_markdownv2(head) + _escape_markdownv2(_TRUNCATION_MARKER)


@dataclass(frozen=True)
class RegenerateCommand:
    """The parsed /regenerate command.

    * match_id — the target VacancyMatch UUID, parsed from the first
      positional argument. The parser rejects non-UUID input so the
      caller can show a usage hint instead of crashing.
    * raw_args — the raw trailing text after the /regenerate token,
      kept so the dispatcher can echo the user's input back when
      showing help or an error message.
    """

    match_id: uuid.UUID
    raw_args: str


def parse_regenerate_command(text: str) -> RegenerateCommand | None:
    """Parse a /regenerate ... text message into a RegenerateCommand.

    Returns None for any of:

    * the command has no positional argument (caller shows usage);
    * the first positional argument is not a valid UUID (caller
      shows usage);
    * the text does not start with /regenerate.

    The trailing text after the UUID is preserved in raw_args so the
    dispatcher can show the user's input back to them. The body is
    stripped; whitespace-only input is treated as absent.
    """
    stripped = text.strip()
    if not stripped.startswith("/regenerate"):
        return None

    body = stripped[len("/regenerate") :].strip()
    if not body:
        return None

    parts = body.split(maxsplit=1)
    raw_id = parts[0]
    try:
        match_id = uuid.UUID(raw_id)
    except ValueError:
        return None

    return RegenerateCommand(match_id=match_id, raw_args=body)


class RegenerateActionHandler:
    """Handle the /regenerate <match_id> Telegram command.

    Collaborators are injected through the constructor. handle is a
    regular async method because
    CoverLetterService.regenerate_for_match is async — the slice
    calls the LLM to produce the new body. The audit log and Telegram
    account lookups are all in-process; the only reason the method is
    async is to await the LLM.

    The dispatcher (TelegramBot) is responsible for extracting
    chat_id and telegram_user_id from the incoming update and
    passing them in. The handler does not look at the raw update
    payload, which keeps the action slice-independent from the
    Telegram transport.
    """

    def __init__(
        self,
        *,
        cover_letter_service: CoverLetterService,
        telegram_account_repo: TelegramAccountRepository,
        audit_service: AuditService,
        profile_repo: Any,
    ) -> None:
        self._cover_letter_service = cover_letter_service
        self._telegram_account_repo = telegram_account_repo
        self._audit_service = audit_service
        # ``profile_repo`` is typed as ``Any`` to keep this module
        # free of a hard import on the matches slice. The only
        # method we call is ``get_by_id``, which both the production
        # :class:`SearchProfileRepository` and the in-memory
        # :class:`InMemorySearchProfileRepository` expose.
        self._profile_repo = profile_repo

    async def handle(
        self,
        *,
        chat_id: int,
        telegram_user_id: int,
        command: RegenerateCommand,
    ) -> SendMessageRequest:
        """Execute the use-case and return the single chat reply."""
        account = self._telegram_account_repo.find_by_telegram_user_id(telegram_user_id)
        if account is None:
            return SendMessageRequest(
                chat_id=chat_id,
                text=(
                    "This Telegram account is not linked to apply-pilot. "
                    "Use /link to connect it first."
                ),
            )

        user_id = account.user_id

        # Pre-validate the match + ownership so the user gets a
        # friendly "match not found" / "does not belong to you"
        # message rather than a generic exception. The cover-letter
        # service is delegated to for the actual regeneration; we
        # never want to call the LLM before we know the caller has
        # the right to ask.
        match_row = self._cover_letter_service.match_repo.get_by_id(command.match_id)
        if match_row is None:
            return SendMessageRequest(
                chat_id=chat_id,
                text=(
                    f"Match {command.match_id} not found. Use /list to see your current matches."
                ),
            )
        profile = self._profile_repo.get_by_id(match_row.search_profile_id)
        if profile is None or profile.user_id != user_id:
            return SendMessageRequest(
                chat_id=chat_id,
                text=(
                    f"Match {command.match_id} does not belong to you. "
                    "You can only regenerate your own matches."
                ),
            )

        try:
            draft = await self._cover_letter_service.regenerate_for_match(command.match_id)
        except CoverLetterDependencyMissingError:
            # Two scenarios land here:
            #
            # * the match has no draft yet (the user has never
            #   called /review to surface the cover letter);
            # * a cross-slice lookup failed (vacancy / profile /
            #   user / resume missing). The first is the common case
            #   and is a UX hint rather than a programmer error: the
            #   user has to ask for a draft before asking to
            #   regenerate one.
            return SendMessageRequest(
                chat_id=chat_id,
                text=(
                    f"No cover letter draft for match {command.match_id} yet. "
                    "Use /review to generate one first."
                ),
            )

        # draft is the freshly regenerated row. The version attribute
        # is the new iteration count (1 -> 2 -> 3 ...).
        self._audit_service.log_event(
            AuditEventType.COVER_LETTER_REGENERATED,
            user_id=user_id,
            details={
                "match_id": str(command.match_id),
                "version": draft.version,
            },
        )

        _LOGGER.info(
            "telegram.regenerate.success",
            extra={
                "event": "telegram.regenerate.success",
                "match_id": str(command.match_id),
                "user_id": str(user_id),
                "version": draft.version,
            },
        )

        preview = _format_preview(draft.content)
        return SendMessageRequest(
            chat_id=chat_id,
            text=(
                f"*Cover letter regenerated*\n"
                f"\n"
                f"*Match:* {_escape_markdownv2(str(command.match_id))}\n"
                f"*Version:* v{draft.version}\n"
                f"\n"
                f"{preview}"
            ),
        )


# Help text for /regenerate. Kept as a module constant so tests and
# the dispatcher share a single source of truth.
REGENERATE_HELP_TEXT = (
    "Usage: /regenerate <match_id>\n\n"
    "Regenerate the cover letter for one of your matches. The LLM is\n"
    "asked to write a fresh version using your resume, the vacancy,\n"
    "and your style preferences; the new body is shown in chat as a\n"
    "preview."
)


__all__ = [
    "REGENERATE_HELP_TEXT",
    "RegenerateActionHandler",
    "RegenerateCommand",
    "parse_regenerate_command",
]
