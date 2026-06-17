"""Pure renderer for the daily digest message.

The renderer is intentionally a free function with no I/O, no clock and
no configuration. It takes a :class:`UserStats` and returns the
Markdown text that :class:`DigestSender` forwards to the Telegram Bot
API. Tests pin the exact output so a future reformat stays a
deliberate, reviewable change.
"""

from __future__ import annotations

from apply_pilot.features.telegram.digest.models import UserStats


def render_digest_message(stats: UserStats) -> str:
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


__all__ = ["render_digest_message"]
