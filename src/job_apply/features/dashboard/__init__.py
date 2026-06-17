"""Dashboard vertical slice (M6, issue #51).

Public surface
--------------

* :class:`DashboardService` — read-only aggregation over the existing
  per-slice repositories. Returns a :class:`DashboardSummary`.
* :class:`DashboardSummary` — frozen dataclass with the per-user
  counts the dashboard renders.
* :class:`DashboardSummaryRead` / :class:`UserStatsRead` — wire
  format for ``GET /dashboard``.
* :func:`dashboard_summary_to_read` — bridge the in-process dataclass
  to the Pydantic wire model.

The slice is intentionally a thin aggregator: there is no new ORM
model, no new database table, and no migration. It composes the
existing ``matches`` / ``apply_worker`` / ``cover_letter`` /
``search_profiles`` / ``sources`` / ``telegram.digest`` repos behind
a single ``GET /dashboard`` endpoint.
"""

from __future__ import annotations

from job_apply.features.dashboard.models import DashboardSummary
from job_apply.features.dashboard.schemas import (
    DashboardSummaryRead,
    UserStatsRead,
    dashboard_summary_to_read,
)
from job_apply.features.dashboard.service import DashboardService

__all__ = [
    "DashboardService",
    "DashboardSummary",
    "DashboardSummaryRead",
    "UserStatsRead",
    "dashboard_summary_to_read",
]
