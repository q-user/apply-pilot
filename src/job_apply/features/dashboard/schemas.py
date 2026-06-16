"""Pydantic schemas for the dashboard slice (M6, issue #51).

The :class:`DashboardSummary` dataclass in :mod:`models` is the
in-process contract; these Pydantic models are the wire format for
``GET /dashboard``. Every field uses ``from_attributes=True`` so the
Pydantic model can be constructed directly from the dataclass via
``DashboardSummaryRead.model_validate(summary)`` — the API layer never
copies fields by hand.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class UserStatsRead(BaseModel):
    """Wire format for the embedded :class:`UserStats` snapshot.

    Mirrors the :class:`job_apply.features.telegram.digest.models.UserStats`
    dataclass so the dashboard card renders the same fields the digest
    card already renders. ``from_attributes=True`` lets the API layer
    hand the dataclass straight to ``model_validate``.
    """

    model_config = ConfigDict(extra="forbid", frozen=False, from_attributes=True)

    matches_total: int
    matches_new: int
    matches_review: int
    matches_accepted: int
    matches_rejected: int
    matches_applied: int
    pending_applications: int
    applied_today: int
    digest_date: date


class DashboardSummaryRead(BaseModel):
    """Wire format for the ``GET /dashboard`` response.

    The :attr:`digest` field is ``None`` when the service is built
    without a :class:`StatsService` (rare; production wiring always
    injects one).
    """

    model_config = ConfigDict(extra="forbid", frozen=False, from_attributes=True)

    matches_total: int
    matches_by_status: dict[str, int] = Field(
        default_factory=dict,
        description="Per-status match counts; every MatchStatus value is a key.",
    )
    applications_total: int
    applications_by_status: dict[str, int] = Field(
        default_factory=dict,
        description="Per-status apply-job counts; every ApplyJobStatus value is a key.",
    )
    cover_letter_drafts_total: int
    search_profiles_active: int
    digest: UserStatsRead | None = Field(
        default=None,
        description="Embedded digest stats for the user; null when the digest is disabled.",
    )


def dashboard_summary_to_read(summary: Any) -> DashboardSummaryRead:
    """Convert a :class:`DashboardSummary` dataclass into a Pydantic model.

    The function handles the nested :class:`UserStats` dataclass the
    same way so callers do not have to reach into the dataclass to
    build the wire model.
    """
    digest = summary.digest
    digest_model: UserStatsRead | None = None
    if digest is not None:
        digest_model = UserStatsRead.model_validate(digest)
    return DashboardSummaryRead(
        matches_total=summary.matches_total,
        matches_by_status=dict(summary.matches_by_status),
        applications_total=summary.applications_total,
        applications_by_status=dict(summary.applications_by_status),
        cover_letter_drafts_total=summary.cover_letter_drafts_total,
        search_profiles_active=summary.search_profiles_active,
        digest=digest_model,
    )


__all__ = [
    "DashboardSummaryRead",
    "UserStatsRead",
    "dashboard_summary_to_read",
]
