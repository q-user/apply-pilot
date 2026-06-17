"""Dashboard vertical slice (M6, issue #51 + M8, issue #67).

Public surface
--------------

* :class:`DashboardService` — read-only aggregation over the existing
  per-slice repositories. Returns a :class:`DashboardSummary` plus
  three M8 analytics snapshots (:class:`FunnelRow`,
  :class:`ConversionRow`, :class:`TimeToApplyStats`).
* :class:`DashboardSummary` — frozen dataclass with the per-user
  counts the dashboard renders.
* :class:`FunnelRow` / :class:`ConversionRow` /
  :class:`TimeToApplyStats` — frozen dataclasses for the M8
  analytics endpoints.
* :class:`DashboardSummaryRead` / :class:`UserStatsRead` —
  :class:`FunnelRead` / :class:`ConversionRead` /
  :class:`TimeToApplyRead` — wire format for ``GET /dashboard``,
  ``GET /dashboard/funnel``, ``GET /dashboard/conversion`` and
  ``GET /dashboard/time-to-apply``.
* :func:`dashboard_summary_to_read` / :func:`funnel_to_read` /
  :func:`conversion_to_read` / :func:`time_to_apply_to_read` —
  bridges from the in-process dataclasses to the Pydantic wire
  models.

The slice is intentionally a thin aggregator: there is no new ORM
model, no new database table, and no migration. It composes the
existing ``matches`` / ``apply_worker`` / ``cover_letter`` /
``search_profiles`` / ``sources`` / ``telegram.digest`` repos behind
a set of ``/dashboard`` endpoints.
"""

from __future__ import annotations

from job_apply.features.dashboard.analytics import (
    ConversionRow,
    FunnelRow,
    TimeToApplyStats,
)
from job_apply.features.dashboard.models import DashboardSummary
from job_apply.features.dashboard.schemas import (
    ConversionRead,
    ConversionRowRead,
    DashboardSummaryRead,
    FunnelFiltersRead,
    FunnelRead,
    FunnelRowRead,
    TimeToApplyRead,
    UserStatsRead,
    conversion_to_read,
    dashboard_summary_to_read,
    funnel_to_read,
    time_to_apply_to_read,
)
from job_apply.features.dashboard.service import DashboardService

__all__ = [
    "ConversionRead",
    "ConversionRow",
    "ConversionRowRead",
    "DashboardService",
    "DashboardSummary",
    "DashboardSummaryRead",
    "FunnelFiltersRead",
    "FunnelRead",
    "FunnelRow",
    "FunnelRowRead",
    "TimeToApplyRead",
    "TimeToApplyStats",
    "UserStatsRead",
    "conversion_to_read",
    "dashboard_summary_to_read",
    "funnel_to_read",
    "time_to_apply_to_read",
]
