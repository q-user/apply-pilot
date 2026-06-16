"""Business logic for the dashboard slice (M6, issue #51).

The :class:`DashboardService` aggregates per-user counts from the
existing per-slice repositories and returns a :class:`DashboardSummary`
snapshot. It is deliberately read-only: there are no writers, no
mutations, and no side effects beyond the embedded digest
:class:`UserStats` computed by the digest's :class:`StatsService`.

The service is collaborator-injected: tests build it with the
in-memory fakes (one per slice), the FastAPI dependency in
:mod:`api` builds it with the SQLAlchemy-backed versions sharing the
request's session.

The service constructs its own :class:`StatsService` from the same
repositories it receives — there is no separate digest dependency on
the public constructor. The :class:`StatsService` is built lazily on
the first :meth:`get_summary` call so the dashboard does not pay for
the digest computation when the call is never made.

The ``vacancy_repo`` parameter is part of the M6 contract — it lets
future slices expose a "vacancies indexed" metric from the same
endpoint — but is not currently used by the summary itself. The slice
documents the reservation rather than introducing dead code paths.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from typing import Protocol, runtime_checkable

from job_apply.features.apply_worker.models import ApplyJob, ApplyJobStatus
from job_apply.features.cover_letter.models import CoverLetterDraft
from job_apply.features.dashboard.models import DashboardSummary
from job_apply.features.matches.models import MatchStatus, VacancyMatch
from job_apply.features.sources.repository import VacancyRepository
from job_apply.features.telegram.digest.models import UserStats
from job_apply.features.telegram.digest.service import StatsService

# ---------------------------------------------------------------------------
# Cross-slice Protocols
# ---------------------------------------------------------------------------
#
# The dashboard slice only needs a small subset of each repository, so the
# service depends on Protocol types rather than the full repository
# interfaces. This keeps the import surface minimal (the service does not
# need to import the SQL implementations) and makes the in-memory fakes
# trivially substitutable.


@runtime_checkable
class _MatchRepo(Protocol):
    """Subset of :class:`VacancyMatchRepository` the service uses."""

    def list_by_user(
        self,
        user_id: uuid.UUID,
        *,
        status: str | None = None,
    ) -> Sequence[VacancyMatch]: ...


@runtime_checkable
class _ApplyJobRepo(Protocol):
    """Subset of :class:`ApplyJobRepository` the service uses."""

    def list_by_user(
        self,
        user_id: uuid.UUID,
        *,
        limit: int = 50,
    ) -> Sequence[ApplyJob]: ...


@runtime_checkable
class _CoverLetterRepo(Protocol):
    """Subset of :class:`CoverLetterDraftRepository` the service uses."""

    def list_by_user(
        self,
        user_id: uuid.UUID,
        *,
        status: str | None = None,
        limit: int = 20,
    ) -> Sequence[CoverLetterDraft]: ...


@runtime_checkable
class _TelegramAccountRepo(Protocol):
    """Subset of the telegram-account repository the digest uses."""

    def list_all(self) -> Sequence[object]: ...


@runtime_checkable
class _UserRepo(Protocol):
    """Subset of the users repository the digest uses."""

    def list_all(self) -> Sequence[object]: ...
    def get_by_id(self, user_id: uuid.UUID) -> object | None: ...


@runtime_checkable
class _ProfileRepo(Protocol):
    """Subset of :class:`SearchProfileRepository` the service uses."""

    def list_by_user(self, user_id: uuid.UUID) -> Sequence[object]: ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bucket_by_status(
    rows: Sequence[object],
    *,
    get_status: Callable[[object], str | None],
    statuses: Sequence[str],
) -> dict[str, int]:
    """Return a ``{status_value: count}`` dict with every status as a key.

    Statuses with no rows still appear in the result with value ``0`` so
    the dashboard front-end can iterate the keys without
    special-casing missing buckets.
    """
    buckets: dict[str, int] = {value: 0 for value in statuses}
    for row in rows:
        status = get_status(row)
        if status is None:
            continue
        buckets[status] = buckets.get(status, 0) + 1
    return buckets


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class DashboardService:
    """Compute a :class:`DashboardSummary` for a single user.

    The service is intentionally a thin orchestrator: every count is a
    single repository call followed by an in-memory bucket-by-status
    walk. The :class:`StatsService` is the only collaborator with its
    own orchestration logic — the dashboard embeds its
    :class:`UserStats` so the endpoint can ship the digest card in a
    single response.

    Constructor parameters
    ----------------------

    * ``match_repo``         — vacancy-match repo (used for
      ``matches_total`` / ``matches_by_status`` and the digest).
    * ``apply_job_repo``     — apply-job repo (used for
      ``applications_total`` / ``applications_by_status``).
    * ``cover_letter_repo``  — cover-letter-draft repo (used for
      ``cover_letter_drafts_total``).
    * ``vacancy_repo``       — vacancy repo. Reserved for future
      M6+ metrics (e.g. "total vacancies indexed"); the current
      summary does not call into it.
    * ``profile_repo``       — search-profile repo (used for
      ``search_profiles_active`` and the digest).
    * ``telegram_account_repo`` — telegram-account repo (used by the
      digest's per-user stats).
    * ``user_repo``          — users repo (used by the digest).
    """

    def __init__(
        self,
        match_repo: _MatchRepo,
        apply_job_repo: _ApplyJobRepo,
        cover_letter_repo: _CoverLetterRepo,
        vacancy_repo: VacancyRepository,
        profile_repo: _ProfileRepo,
        telegram_account_repo: _TelegramAccountRepo,
        user_repo: _UserRepo,
    ) -> None:
        self._match_repo = match_repo
        self._apply_job_repo = apply_job_repo
        self._cover_letter_repo = cover_letter_repo
        # ``vacancy_repo`` is accepted per the M6 contract but is not
        # used by the current summary shape. Storing it on the
        # instance keeps the parameter live for static analysis and
        # gives future M6+ slices a place to plug in without changing
        # the public service signature.
        self._vacancy_repo: VacancyRepository = vacancy_repo
        self._profile_repo = profile_repo
        self._telegram_account_repo = telegram_account_repo
        self._user_repo = user_repo
        # The :class:`StatsService` is built lazily on the first
        # :meth:`get_summary` call so the constructor stays cheap when
        # the service is instantiated but never queried.
        self._stats_service: StatsService | None = None

    @property
    def match_repo(self) -> _MatchRepo:
        return self._match_repo

    @property
    def apply_job_repo(self) -> _ApplyJobRepo:
        return self._apply_job_repo

    @property
    def cover_letter_repo(self) -> _CoverLetterRepo:
        return self._cover_letter_repo

    @property
    def profile_repo(self) -> _ProfileRepo:
        return self._profile_repo

    # -- main entry point -------------------------------------------------

    def get_summary(self, user_id: uuid.UUID) -> DashboardSummary:
        """Return a :class:`DashboardSummary` for *user_id*.

        All counts are scoped to *user_id*. The ``digest`` field is
        populated by re-using the digest's :class:`StatsService` so
        the dashboard can render the digest card without a second
        request.

        The method is synchronous: the async :class:`StatsService`
        call is bridged with :func:`asyncio.run` so the service has a
        sync contract for callers (HTTP handlers, unit tests) that do
        not want to deal with the event loop themselves.
        """
        import asyncio

        matches = list(self._match_repo.list_by_user(user_id))
        applications = list(self._apply_job_repo.list_by_user(user_id))
        drafts = list(self._cover_letter_repo.list_by_user(user_id))
        profiles = list(self._profile_repo.list_by_user(user_id))

        match_statuses = [s.value for s in MatchStatus]
        application_statuses = [s.value for s in ApplyJobStatus]

        matches_by_status = _bucket_by_status(
            matches, get_status=lambda m: m.status, statuses=match_statuses
        )
        applications_by_status = _bucket_by_status(
            applications,
            get_status=lambda j: j.status,
            statuses=application_statuses,
        )

        digest = asyncio.run(self._compute_digest(user_id))

        return DashboardSummary(
            matches_total=len(matches),
            applications_total=len(applications),
            cover_letter_drafts_total=len(drafts),
            search_profiles_active=sum(1 for p in profiles if bool(getattr(p, "is_active", False))),
            matches_by_status=matches_by_status,
            applications_by_status=applications_by_status,
            digest=digest,
        )

    # -- helpers ----------------------------------------------------------

    async def _compute_digest(self, user_id: uuid.UUID) -> UserStats:
        """Return the embedded :class:`UserStats`.

        The :class:`StatsService` is built lazily so the constructor
        stays cheap and the dependency is only paid for when the
        dashboard is actually queried.
        """
        if self._stats_service is None:
            self._stats_service = StatsService(
                match_repo=self._match_repo,
                telegram_account_repo=self._telegram_account_repo,
                user_repo=self._user_repo,
                profile_repo=self._profile_repo,
            )
        return await self._stats_service.get_user_stats(user_id)


__all__ = ["DashboardService"]
