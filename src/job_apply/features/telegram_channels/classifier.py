"""Telegram-channels vacancy classifier (M7, issue #58).

Telegram channels mix vacancy posts with discussion, news, and
self-promotion. The classifier is a fast keyword-driven pre-filter
that runs **before** the (more expensive) normaliser + ingest path
so the scanner can skip irrelevant traffic cheaply.

The classifier is intentionally simple — a tuple of case-insensitive
"vacancy" markers, and a tuple of "this is a discussion /
non-vacancy" markers that win when both fire. The lists live in
:data:`DEFAULT_VACANCY_MARKERS` / :data:`DEFAULT_NON_VACANCY_MARKERS`
and are overridable on construction so a channel-specific subset can
be plugged in by callers that know the channel's vocabulary.

False-positive cost vs. false-negative cost
-------------------------------------------

A false positive (a non-vacancy post that slips through) is cheap:
the normaliser produces a low-quality :class:`Vacancy` that the
existing dedup + scoring layers filter out downstream. A false
negative (a real vacancy that gets dropped) is expensive: the user
never sees it. The default marker set is therefore tuned for
recall, with the anti-marker list (``#novacancy`` and friends)
catching the obvious self-rejects.
"""

from __future__ import annotations

#: Default vacancy markers — a post is a candidate vacancy if any of
#: these tokens appear (case-insensitive substring match). The list
#: is intentionally short: adding more markers trades precision for
#: recall in a way that's hard to undo from a unit test.
DEFAULT_VACANCY_MARKERS: tuple[str, ...] = (
    "#vacancy",
    "#вакансия",
    "hiring",
    "we are looking",
    "ищу",
    "открыта позиция",
)

#: Default non-vacancy markers — a post that carries any of these
#: tokens is *not* a vacancy, even if a vacancy marker also appears
#: in the text. The anti-marker check runs first so a post like
#: "we are hiring... no, just kidding, #novacancy" lands in the
#: non-vacancy bucket.
DEFAULT_NON_VACANCY_MARKERS: tuple[str, ...] = (
    "#novacancy",
    "#невакансия",
    "#closed",
    "#закрыто",
)


class TelegramChannelClassifier:
    """Decide whether a Telegram-channel post is a vacancy.

    The classifier is state-free and side-effect free. Build it
    once and reuse it from the adapter / scanner.

    Args:
        vacancy_markers: Tuple of case-insensitive substrings that
            positively identify a vacancy post. Defaults to
            :data:`DEFAULT_VACANCY_MARKERS`.
        non_vacancy_markers: Tuple of case-insensitive substrings
            that **negate** the vacancy call. Defaults to
            :data:`DEFAULT_NON_VACANCY_MARKERS`.

    Examples:
        >>> c = TelegramChannelClassifier()
        >>> c.is_vacancy_post("#vacancy Senior Python Developer")
        True
        >>> c.is_vacancy_post("Just chatting today.")
        False
    """

    def __init__(
        self,
        *,
        vacancy_markers: tuple[str, ...] | None = None,
        non_vacancy_markers: tuple[str, ...] | None = None,
    ) -> None:
        # Lower-cased at construction so the per-call check is a
        # single case-fold + substring search, not a per-marker
        # lower() call. The whole tuple is small enough that the
        # extra startup cost is negligible.
        self._vacancy_markers: tuple[str, ...] = tuple(
            m.lower() for m in (vacancy_markers or DEFAULT_VACANCY_MARKERS)
        )
        self._non_vacancy_markers: tuple[str, ...] = tuple(
            m.lower() for m in (non_vacancy_markers or DEFAULT_NON_VACANCY_MARKERS)
        )

    @property
    def vacancy_markers(self) -> tuple[str, ...]:
        """Return the configured vacancy markers (read-only)."""
        return self._vacancy_markers

    @property
    def non_vacancy_markers(self) -> tuple[str, ...]:
        """Return the configured non-vacancy markers (read-only)."""
        return self._non_vacancy_markers

    def is_vacancy_post(self, text: str | None) -> bool:
        """Return ``True`` if ``text`` looks like a vacancy post.

        The check is three steps, in order:

        1. ``None`` or whitespace-only text is *not* a vacancy —
           Telegram channels do carry image-only posts, and we do
           not want the classifier to second-guess the user's
           caption-less share.
        2. Any non-vacancy marker wins, even if a vacancy marker
           also appears.
        3. Otherwise, any vacancy marker is enough.
        """
        if not text:
            return False
        lowered = text.lower()
        if any(marker and marker in lowered for marker in self._non_vacancy_markers):
            return False
        return any(marker and marker in lowered for marker in self._vacancy_markers)


__all__ = [
    "DEFAULT_NON_VACANCY_MARKERS",
    "DEFAULT_VACANCY_MARKERS",
    "TelegramChannelClassifier",
]
