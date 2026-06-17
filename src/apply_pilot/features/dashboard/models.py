"""Dashboard summary value object (M6, issue #51).

A :class:`DashboardSummary` is the flat, immutable snapshot of
per-user counts that the ``GET /dashboard`` endpoint returns. The
shape is intentionally aligned with the dashboard cards:

* ``matches_by_status`` and ``applications_by_status`` always carry
  every status value as a key so the front-end can render a complete
  card without doing its own bookkeeping.
* ``digest`` re-embeds the :class:`UserStats` from the daily digest so
  the dashboard does not have to make a second round-trip to populate
  the digest card.

The dataclass is the in-process contract; the FastAPI response uses a
Pydantic model that mirrors the same shape (:mod:`schemas`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apply_pilot.features.telegram.digest.models import UserStats


@dataclass(frozen=True)
class DashboardSummary:
    """Per-user counts that the dashboard renders.

    Attributes:
        matches_total: All matches owned by the user's profiles.
        matches_by_status: Bucket-by-status match counts. Every
            :class:`MatchStatus` value appears as a key, with
            ``0`` for buckets the user has no matches in.
        applications_total: All apply jobs owned by the user.
        applications_by_status: Bucket-by-status apply-job counts.
            Every :class:`ApplyJobStatus` value appears as a key.
        cover_letter_drafts_total: All cover-letter drafts owned by
            the user, regardless of ``status``.
        search_profiles_active: Number of search profiles owned by
            the user with ``is_active=True``. Inactive profiles are
            excluded.
        digest: The :class:`UserStats` from the digest slice, embedded
            for convenience so the dashboard can render the digest
            card without a second request. ``None`` when the service
            is built without a :class:`StatsService`.
    """

    matches_total: int
    applications_total: int
    cover_letter_drafts_total: int
    search_profiles_active: int
    matches_by_status: dict[str, int] = field(default_factory=dict)
    applications_by_status: dict[str, int] = field(default_factory=dict)
    digest: UserStats | None = None


__all__ = ["DashboardSummary"]
