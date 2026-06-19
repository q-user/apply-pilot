"""Pure renderer for the daily digest message.

The renderer is intentionally a free function with no I/O, no clock and
no configuration. It takes a stats value object and returns the
Markdown text that the channel-specific sender forwards to the bot's
HTTP API. Tests pin the exact output so a future reformat stays a
deliberate, reviewable change.

The parameter is typed as a :class:`DigestStats` Protocol rather than
the concrete :class:`apply_pilot.features.telegram.digest.models.UserStats`
so the module stays free of cross-slice imports. Both the Telegram
``UserStats`` and any future MAX equivalent satisfy the Protocol
structurally.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol


class DigestStats(Protocol):
    """The minimal stats surface the renderer depends on.

    Mirrors the attributes on
    :class:`apply_pilot.features.telegram.digest.models.UserStats`.
    Duck typing covers the rest: any object with these attributes
    renders correctly.
    """

    digest_date: date
    matches_total: int
    matches_new: int
    matches_review: int
    matches_accepted: int
    matches_rejected: int
    applied_today: int
    pending_applications: int


def render_digest_message(stats: DigestStats) -> str:
    """Render *stats* as a Markdown-formatted digest message.

    The output layout is stable: a header line with the date, a
    ``Matches`` block, an ``Applications`` block and a closing hint
    pointing the user at the ``/matches`` command. Empty sections are
    rendered with a zero rather than collapsed so the message shape
    does not surprise the user.
    """
    return (
        f"📊 Your daily digest for {stats.digest_date.isoformat()}\n"
        "\n"
        "Matches:\n"
        f"• {stats.matches_total} total "
        f"({stats.matches_new} new, {stats.matches_review} in review)\n"
        f"• {stats.matches_accepted} accepted, {stats.matches_rejected} rejected\n"
        "\n"
        "Applications:\n"
        f"• {stats.applied_today} applied today\n"
        f"• {stats.pending_applications} pending\n"
        "\n"
        "Reply /matches to see your new matches."
    )


__all__ = ["DigestStats", "render_digest_message"]
