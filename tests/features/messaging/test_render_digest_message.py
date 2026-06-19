"""Tests for the daily digest message renderer.

The renderer is a pure function: given a :class:`UserStats` value object
it returns a Markdown-formatted string. No I/O, no clock, no
configuration. These tests pin the exact rendered output so a future
reformat stays a deliberate, reviewable change.
"""

from __future__ import annotations

from datetime import date

from apply_pilot.features.messaging.digest import render_digest_message
from apply_pilot.features.telegram.digest.models import UserStats


def _stats(**overrides: object) -> UserStats:
    """Build a deterministic :class:`UserStats` with overridable fields."""
    base: dict[str, object] = {
        "matches_total": 42,
        "matches_new": 12,
        "matches_review": 5,
        "matches_accepted": 8,
        "matches_rejected": 3,
        "matches_applied": 6,
        "pending_applications": 4,
        "applied_today": 2,
        "digest_date": date(2026, 6, 15),
    }
    base.update(overrides)
    return UserStats(**base)  # type: ignore[arg-type]


def test_render_digest_message_full_snapshot() -> None:
    """The canonical digest message matches the documented Markdown."""
    rendered = render_digest_message(_stats())

    expected = (
        "📊 Your daily digest for 2026-06-15\n"
        "\n"
        "Matches:\n"
        "• 42 total (12 new, 5 in review)\n"
        "• 8 accepted, 3 rejected\n"
        "\n"
        "Applications:\n"
        "• 2 applied today\n"
        "• 4 pending\n"
        "\n"
        "Reply /matches to see your new matches."
    )
    assert rendered == expected


def test_render_digest_message_includes_header_emoji_and_date() -> None:
    """The first line is the emoji + the digest date in ISO-8601 format."""
    rendered = render_digest_message(_stats(digest_date=date(2026, 1, 3)))
    first_line = rendered.splitlines()[0]
    assert "📊" in first_line
    assert "2026-01-03" in first_line


def test_render_digest_message_reflects_zero_counts() -> None:
    """Zero counts still render the corresponding bullets (not omitted)."""
    rendered = render_digest_message(
        _stats(
            matches_total=0,
            matches_new=0,
            matches_review=0,
            matches_accepted=0,
            matches_rejected=0,
            matches_applied=0,
            pending_applications=0,
            applied_today=0,
        )
    )

    assert "• 0 total (0 new, 0 in review)" in rendered
    assert "• 0 accepted, 0 rejected" in rendered
    assert "• 0 applied today" in rendered
    assert "• 0 pending" in rendered
    # The reply hint should still be there so the user knows what to do next.
    assert "/matches" in rendered


def test_render_digest_message_ends_with_reply_hint() -> None:
    """The last line nudges the user to /matches to drill into the new ones."""
    rendered = render_digest_message(_stats())
    assert rendered.endswith("Reply /matches to see your new matches.")
