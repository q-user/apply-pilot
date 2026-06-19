"""Pydantic schemas for the dashboard slice (M6, issue #51 + M8, #67).

The :class:`DashboardSummary` dataclass in :mod:`models` is the
in-process contract; these Pydantic models are the wire format for the
``/dashboard`` endpoints. Every field uses ``from_attributes=True`` so
the Pydantic model can be constructed directly from the dataclass via
``Model.model_validate(thing)`` — the API layer never copies fields by
hand.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class UserStatsRead(BaseModel):
    """Wire format for the embedded :class:`UserStats` snapshot.

    Mirrors the :class:`apply_pilot.features.telegram.digest.models.UserStats`
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


class ApplyJobSummaryRead(BaseModel):
    """Wire format for one :class:`ApplyJob` row in the dashboard table.

    Used by the dashboard web page's "Recent apply jobs" section
    (M6, issue #172). The :attr:`vacancy_id` is the **source**
    identifier (``Vacancy.source_id`` when the underlying vacancy
    row exists, otherwise the bare UUID string of
    :attr:`ApplyJob.vacancy_id`). Resolved by the web layer at render
    time so the schema stays decoupled from the :class:`Vacancy`
    model.
    """

    model_config = ConfigDict(extra="forbid", frozen=False, from_attributes=True)

    id: uuid.UUID
    status: str
    vacancy_id: str
    created_at: datetime
    last_error: str | None = None


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


# ---------------------------------------------------------------------------
# Analytics schemas (M8, issue #67)
# ---------------------------------------------------------------------------


class FunnelRowRead(BaseModel):
    """Wire format for one :class:`FunnelRow` of the source funnel."""

    model_config = ConfigDict(extra="forbid", frozen=False, from_attributes=True)

    source: str
    fetched: int
    matched: int
    accepted: int
    applied: int
    rejected: int


class FunnelFiltersRead(BaseModel):
    """Echoes the query parameters the funnel was computed for."""

    model_config = ConfigDict(extra="forbid", frozen=False, from_attributes=True)

    source: str | None = None
    since: datetime | None = None
    until: datetime | None = None


class FunnelRead(BaseModel):
    """Wire format for ``GET /dashboard/funnel``.

    The response is always a ``FunnelRead`` even when no data is
    available; ``rows`` is the empty list in that case and ``filters``
    echoes the input. The shape stays stable so the front-end does
    not have to branch on the empty / non-empty cases.
    """

    model_config = ConfigDict(extra="forbid", frozen=False, from_attributes=True)

    rows: list[FunnelRowRead] = Field(default_factory=list)
    filters: FunnelFiltersRead = Field(default_factory=FunnelFiltersRead)


class ConversionRowRead(BaseModel):
    """Wire format for one :class:`ConversionRow` of the conversion table."""

    model_config = ConfigDict(extra="forbid", frozen=False, from_attributes=True)

    profile_id: str
    matches: int
    accepted: int
    applied: int
    accepted_rate: float
    applied_rate: float


class ConversionRead(BaseModel):
    """Wire format for ``GET /dashboard/conversion``.

    ``rows`` is empty when the user owns no profiles. The ``rows``
    field is always a list (never ``null``) so the front-end can
    iterate without a presence check.
    """

    model_config = ConfigDict(extra="forbid", frozen=False, from_attributes=True)

    rows: list[ConversionRowRead] = Field(default_factory=list)


class TimeToApplyRead(BaseModel):
    """Wire format for ``GET /dashboard/time-to-apply``.

    The endpoint serialises ``None`` (no data) as JSON ``null`` so
    the front-end can render a "no data" placeholder without an extra
    round-trip.
    """

    model_config = ConfigDict(extra="forbid", frozen=False, from_attributes=True)

    average_seconds: float
    median_seconds: float
    sample_size: int


def funnel_to_read(
    rows: list[Any],
    *,
    source: str | None,
    since: datetime | None,
    until: datetime | None,
) -> FunnelRead:
    """Bridge the in-process :class:`FunnelRow` list to the wire model."""
    return FunnelRead(
        rows=[FunnelRowRead.model_validate(row) for row in rows],
        filters=FunnelFiltersRead(source=source, since=since, until=until),
    )


def conversion_to_read(rows: list[Any]) -> ConversionRead:
    """Bridge the in-process :class:`ConversionRow` list to the wire model."""
    return ConversionRead(rows=[ConversionRowRead.model_validate(row) for row in rows])


def time_to_apply_to_read(stats: Any | None) -> TimeToApplyRead | None:
    """Bridge the in-process :class:`TimeToApplyStats` dataclass to the wire model.

    ``None`` flows through unchanged so the API can serialise the
    "no data" case as JSON ``null``.
    """
    if stats is None:
        return None
    return TimeToApplyRead.model_validate(stats)


__all__ = [
    "ApplyJobSummaryRead",
    "ConversionRead",
    "ConversionRowRead",
    "DashboardSummaryRead",
    "FunnelFiltersRead",
    "FunnelRead",
    "FunnelRowRead",
    "TimeToApplyRead",
    "UserStatsRead",
    "conversion_to_read",
    "dashboard_summary_to_read",
    "funnel_to_read",
    "time_to_apply_to_read",
]
