"""Telegram-channels vacancy normaliser (M7, issue #58).

The normaliser maps a raw :class:`TelegramChannelMessage` dict (the
adapter's "raw" payload) into the canonical
:class:`~apply_pilot.features.sources.models.Vacancy` row. It is
intentionally a pure mapper: classification has already happened by
the time we get here, so the normaliser assumes the message *is* a
vacancy post.

Why a separate normaliser (and not a branch on ``VacancyNormalizer``)
--------------------------------------------------------------------

The :class:`VacancyNormalizer` already dispatches on the ``source``
string and owns the hh.ru mapping. The Telegram-channel payload is
shaped differently (channel-id/message-id tuple, plain text body,
no structured salary / employer / area fields) so a sibling
``TelegramChannelNormalizer`` keeps each source's quirks in its own
file. The :class:`TelegramChannelSourceAdapter` is the only place
that wires the two together.

Natural key
-----------

The natural key for dedup is ``f"{channel_id}:{message_id}"``. The
scanner always operates on the same channel, so a re-poll of the
same Telegram message is detected by the
``(source, source_id)`` unique constraint at the database level
*and* by the in-memory dedup check in
:class:`VacancyDeduplicator`.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Final

from apply_pilot.features.sources.models import Vacancy

#: Source identifier persisted in
#: :attr:`Vacancy.source`. Must match the adapter's
#: :attr:`TelegramChannelSourceAdapter.name` and the
#: :class:`AdapterRegistry` key.
SOURCE_NAME: Final[str] = "telegram_channel"

#: Recognised "company name" lines inside the post text. The normaliser
#: scans the first few non-empty lines for a case-insensitive prefix
#: match against this tuple and uses the value as
#: :attr:`Vacancy.employer_name` when found.
_COMPANY_LINE_PREFIXES: Final[tuple[str, ...]] = (
    "company:",
    "компания:",
    "фирма:",
    "client:",
)

#: Pre-compiled matcher for the company line: any of the
#: :data:`_COMPANY_LINE_PREFIXES` at the start of a line, with
#: surrounding whitespace tolerated.
_COMPANY_LINE_RE = re.compile(
    r"^\s*(?P<prefix>"
    + "|".join(re.escape(p) for p in _COMPANY_LINE_PREFIXES)
    + r")\s*(?P<value>.+)$",
    re.IGNORECASE,
)


def compute_content_hash(
    title: str,
    description: str | None,
    employer_name: str | None,
) -> str:
    """Return the SHA-256 hex digest of ``title|description|employer_name``.

    Same shape as the :class:`VacancyNormalizer` helper so the
    cross-source dedup detector finds Telegram-channel vacancies
    that share content with hh.ru ones.
    """
    parts = [title, description or "", employer_name or ""]
    joined = "|".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _first_non_empty_line(text: str) -> str:
    """Return the first non-empty line of ``text``, stripped.

    The post's first line is, by community convention, the
    one-line job title. Lines that only contain a leading hashtag
    token (``#vacancy …``) are stripped of the hashtag prefix so
    the title carries meaningful content, not the classifier's
    marker. A line that is hashtag-only (``#vacancy`` with nothing
    after) is skipped so the real title (on the following line)
    is used instead.
    """
    # Strip a single leading hashtag token (``#foo``) and any
    # whitespace after it. We deliberately do not match multiple
    # hashtags — ``#foo #bar Senior Python`` keeps the hashtags
    # because the user meant the whole line to be the title.
    hashtag_prefix = re.compile(r"^\s*#\S+\s*")
    for raw_line in text.splitlines():
        stripped = hashtag_prefix.sub("", raw_line).strip()
        if stripped:
            return stripped
    return ""


def _extract_company_name(text: str) -> str | None:
    """Return the company name from a ``Company: …`` line, if any.

    The check is intentionally loose — Telegram posts are not
    structured, so a "company" line might be capitalised
    inconsistently or surrounded by emojis. The regex matches the
    prefix case-insensitively and returns the trailing text as the
    candidate name.
    """
    for raw_line in text.splitlines():
        match = _COMPANY_LINE_RE.match(raw_line)
        if match is None:
            continue
        value = match.group("value").strip()
        if value:
            return value
    return None


def _build_telegram_message_url(channel_id: str, message_id: int) -> str:
    """Build a ``t.me`` deep-link for a channel message.

    Telegram exposes two URL shapes:

    * ``https://t.me/{channel}/{message_id}`` for public channels
      (e.g. ``@jobs``).
    * ``https://t.me/c/{numeric_id}/{message_id}`` for supergroup
      channels identified by a numeric id (the id is the absolute
      value of the channel id, since the URL form is unsigned).
    """
    if channel_id.startswith("-"):
        # Numeric channel id (e.g. ``-1001234567890``). The URL
        # shape is ``t.me/c/{id_without_leading_dash}/{message}``.
        return f"https://t.me/c/{channel_id[1:]}/{message_id}"
    return f"https://t.me/{channel_id}/{message_id}"


def _source_id(channel_id: str, message_id: int) -> str:
    """Build the natural key for the (channel, message) tuple.

    The format ``"{channel_id}:{message_id}"`` is stable and
    collision-free as long as ``message_id`` is unique within the
    channel (which Telegram guarantees).
    """
    return f"{channel_id}:{message_id}"


class TelegramChannelNormalizer:
    """Map a raw Telegram-channel message dict into a canonical :class:`Vacancy`.

    The normaliser does **not** classify — by the time
    :meth:`normalize` is called, the upstream
    :class:`TelegramChannelClassifier` has already accepted the post.
    The normaliser's job is the structured mapping: title from the
    first line, description from the full text, employer from an
    explicit ``Company: …`` line (falling back to the post author),
    URL from the ``t.me`` deep-link, and the
    :attr:`Vacancy.content_hash` from the canonical triple.
    """

    def normalize(self, raw: dict[str, Any]) -> Vacancy:
        """Map ``raw`` into a canonical :class:`Vacancy`.

        Args:
            raw: Dict shape produced by
                :meth:`TelegramChannelMessage.to_raw_dict`. The
                required keys are ``channel_id``, ``message_id`` and
                ``text``; ``author`` and ``published_at`` are
                optional.

        Returns:
            A :class:`Vacancy` with ``source == "telegram_channel"``
            and ``source_id == f"{channel_id}:{message_id}"``.

        Raises:
            ValueError: If ``raw`` is not a dict, or if any of the
                required keys are missing.
        """
        if not isinstance(raw, dict):
            raise ValueError(
                f"TelegramChannelNormalizer expected a dict payload, got {type(raw).__name__}."
            )
        channel_id = raw.get("channel_id")
        if not channel_id or not isinstance(channel_id, str):
            raise ValueError(
                "TelegramChannelNormalizer requires 'channel_id' "
                "(non-empty str) in the raw payload."
            )
        message_id = raw.get("message_id")
        if not isinstance(message_id, int) or isinstance(message_id, bool) or message_id <= 0:
            raise ValueError(
                "TelegramChannelNormalizer requires 'message_id' (positive int) in the raw payload."
            )
        if "text" not in raw:
            raise ValueError(
                "TelegramChannelNormalizer requires 'text' in the raw payload "
                "(use None explicitly to signal a media-only post)."
            )

        text = raw.get("text") or ""
        author = raw.get("author")
        if author is not None and not isinstance(author, str):
            # Defensive: a misbehaving transport should not produce
            # a non-string author. Treat anything else as missing.
            author = None

        title = _first_non_empty_line(text)
        # Empty text is allowed (media-only posts) — the title is
        # left empty rather than synthesised; the cross-source
        # dedup detector still matches on the content_hash.
        description = text if text else None
        employer_name = _extract_company_name(text) or author
        url = _build_telegram_message_url(channel_id, message_id)
        source_id = _source_id(channel_id, message_id)

        return Vacancy(
            source=SOURCE_NAME,
            source_id=source_id,
            title=title,
            description=description,
            url=url,
            employer_name=employer_name,
            raw_data=dict(raw),
            content_hash=compute_content_hash(title, description, employer_name),
        )


__all__ = ["SOURCE_NAME", "TelegramChannelNormalizer"]
