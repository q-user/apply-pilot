"""Value object for the daily digest stats.

A :class:`UserStats` is a flat, immutable snapshot of the counts that
the digest message renders. Keeping it frozen makes the renderer
trivially testable and lets the same object flow from
:class:`StatsService` through the renderer to the sender without any
adapters in between.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class UserStats:
    """Per-user counts that the daily digest renders.

    Attributes:
        matches_total: All matches that belong to the user's profiles,
            regardless of status. Includes ``dismissed`` matches.
        matches_new: Matches with status in ``{new, scored}`` — the
            queue the user has not reviewed yet.
        matches_review: Matches flagged for human review.
        matches_accepted: Matches the user has accepted (eligible for
            the apply worker).
        matches_rejected: Matches the user has rejected.
        matches_applied: Matches that have moved to ``applied`` — a
            counter independent of the calendar date.
        pending_applications: Proxy for the apply-worker queue; until
            the apply worker exists, this is the number of accepted
            matches that have not been submitted yet.
        applied_today: Matches that moved to ``applied`` on the digest
            date (UTC). Used to highlight daily throughput.
        digest_date: The date the digest covers. Drives the header and
            the ``applied_today`` filter.
    """

    matches_total: int
    matches_new: int
    matches_review: int
    matches_accepted: int
    matches_rejected: int
    matches_applied: int
    pending_applications: int
    applied_today: int
    digest_date: date


__all__ = ["UserStats"]
