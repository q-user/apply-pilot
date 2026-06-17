"""Business logic for the dashboard slice (M6, issue #51 + M8, issue #67).

The :class:`DashboardService` aggregates per-user counts from the
existing per-slice repositories and returns a :class:`DashboardSummary`
snapshot, plus three analytics snapshots for the M8 dashboard:

* :meth:`get_funnel`           — counts per source
* :meth:`get_conversion`       — counts + rates per search profile
* :meth:`get_time_to_apply`    — average + median time-to-apply

It is deliberately read-only: there are no writers, no mutations, and
no side effects beyond the embedded digest :class:`UserStats` computed
by the digest's :class:`StatsService`.

The service is collaborator-injected: tests build it with the
in-memory fakes (one per slice), the FastAPI dependency in :mod:`api`
builds it with the SQLAlchemy-backed versions sharing the request's
session.

The service constructs its own :class:`StatsService` from the same
repositories it receives — there is no separate digest dependency on
the public constructor. The :class:`StatsService` is built lazily on the
first :meth:`get_summary` call so the dashboard does not pay for the
digest computation when the call is never made.
"""

from __future__ import annotations

import statistics
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from job_apply.features.apply_worker.models import ApplyJob, ApplyJobStatus
from job_apply.features.cover_letter.models import CoverLetterDraft
from job_apply.features.dashboard.analytics import (
    ConversionRow,
    FunnelRow,
    TimeToApplyStats,
)
from job_apply.features.dashboard.models import DashboardSummary
from job_apply.features.matches.models import MatchStatus, VacancyMatch
from job_apply.features.sources.models import Vacancy
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


# Terminal :class:`ApplyJobStatus` values for the ``applied`` /
# time-to-apply metrics. ``QUEUED`` and ``RUNNING`` are in-flight;
# the rest are terminal in the sense that the worker has finished
# processing the job (whether it ultimately succeeded is reflected in
# the ``status`` itself).
_TERMINAL_APPLY_STATUSES: frozenset[str] = frozenset(
    {
        ApplyJobStatus.SUCCEEDED.value,
        ApplyJobStatus.FAILED.value,
        ApplyJobStatus.DEAD_LETTER.value,
        ApplyJobStatus.CANCELLED.value,
    }
)


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
    buckets: dict[str, int] = dict.fromkeys(statuses, 0)
    for row in rows:
        status = get_status(row)
        if status is None:
            continue
        buckets[status] = buckets.get(status, 0) + 1
    return buckets


def _vacancy_source(vacancy: Vacancy | None) -> str | None:
    """Return ``vacancy.source`` or ``None`` when *vacancy* is missing.

    The funnel needs a stable ``source`` value per match; matches whose
    vacancy row has been deleted are silently dropped (their source is
    unknown and would pollute the per-source totals).
    """
    if vacancy is None:
        return None
    return vacancy.source


def _apply_job_is_terminal(job: ApplyJob) -> bool:
    return job.status in _TERMINAL_APPLY_STATUSES


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
      ``matches_total`` / ``matches_by_status``, the digest and the
      analytics endpoints).
    * ``apply_job_repo``     — apply-job repo (used for
      ``applications_total`` / ``applications_by_status`` and the
      ``applied`` count in the funnel / conversion tables).
    * ``cover_letter_repo``  — cover-letter-draft repo (used for
      ``cover_letter_drafts_total``).
    * ``vacancy_repo``       — vacancy repo. Used by the funnel
      endpoint to resolve ``VacancyMatch.vacancy_id`` → source.
      ``list_recent(limit=large)`` is the only call exercised today.
    * ``profile_repo``       — search-profile repo (used for
      ``search_profiles_active``, the conversion table and the
      digest).
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
        # ``vacancy_repo`` is used by the M8 analytics endpoints
        # (:meth:`get_funnel` needs to resolve each match's
        # ``vacancy_id`` to a ``source``). It is part of the M6
        # contract — see the M6 #51 history.
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
    def vacancy_repo(self) -> VacancyRepository:
        return self._vacancy_repo

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

    # -- analytics endpoints (M8, issue #67) -----------------------------

    def get_funnel(
        self,
        user_id: uuid.UUID,
        *,
        source: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[FunnelRow]:
        """Return the per-source funnel for *user_id*.

        Each :class:`FunnelRow` carries five counts:

        * ``fetched``  — number of distinct :class:`Vacancy` rows for
          the source that the user has been matched against within
          the date window. A vacancy the user never matched against
          is not counted.
        * ``matched``  — number of :class:`VacancyMatch` rows whose
          underlying vacancy belongs to the source.
        * ``accepted`` — number of matches with
          ``status='accepted'``.
        * ``applied``  — number of :class:`ApplyJob` rows in a
          terminal state.
        * ``rejected`` — number of matches with
          ``status='rejected'``.

        ``source`` limits the result to a single row. ``since`` /
        ``until`` are inclusive lower / exclusive upper bounds on
        ``vacancies.created_at`` (the "fetched" event) and on
        ``vacancy_matches.created_at`` (the "matched" event). The
        ``applied`` count is filtered on
        ``apply_jobs.finished_at`` when it is non-``NULL``; rows with
        a ``NULL`` ``finished_at`` are excluded (they are not
        terminal yet).
        """
        vacancies = self._all_vacancies()
        vacancies_by_id = {v.id: v for v in vacancies}

        matches = list(self._match_repo.list_by_user(user_id))
        applications = list(self._apply_job_repo.list_by_user(user_id, limit=10_000))

        # Build a per-match source lookup so the funnel can group
        # matches, acceptances, rejections and apply jobs by their
        # underlying vacancy source.
        match_sources: dict[uuid.UUID, str | None] = {
            m.id: _vacancy_source(vacancies_by_id.get(m.vacancy_id)) for m in matches
        }
        match_id_to_source: dict[uuid.UUID, str | None] = match_sources

        # Group by source.
        fetched_by_source: dict[str, int] = {}
        matched_by_source: dict[str, int] = {}
        accepted_by_source: dict[str, int] = {}
        rejected_by_source: dict[str, int] = {}
        applied_by_source: dict[str, int] = {}

        for m in matches:
            src = match_sources.get(m.id)
            if src is None:
                continue
            if source is not None and src != source:
                continue
            if since is not None and (m.created_at is None or m.created_at < since):
                continue
            if until is not None and (m.created_at is not None and m.created_at >= until):
                continue
            vacancy = vacancies_by_id.get(m.vacancy_id)
            if vacancy is not None:
                if since is not None and (vacancy.created_at is None or vacancy.created_at < since):
                    continue
                if until is not None and (
                    vacancy.created_at is not None and vacancy.created_at >= until
                ):
                    continue
                fetched_by_source[vacancy.source] = fetched_by_source.get(vacancy.source, 0) + 1
            matched_by_source[src] = matched_by_source.get(src, 0) + 1
            if m.status == MatchStatus.ACCEPTED.value:
                accepted_by_source[src] = accepted_by_source.get(src, 0) + 1
            elif m.status == MatchStatus.REJECTED.value:
                rejected_by_source[src] = rejected_by_source.get(src, 0) + 1

        for job in applications:
            if not _apply_job_is_terminal(job):
                continue
            src = match_id_to_source.get(job.match_id)
            if src is None:
                continue
            if source is not None and src != source:
                continue
            # ``finished_at`` is the wall-clock end of the apply
            # pipeline; filter the applied count on that field
            # because that is the timestamp the user is asking
            # about ("how many did we apply to in this window?").
            finished_at = job.finished_at
            if finished_at is None:
                continue
            if since is not None and finished_at < since:
                continue
            if until is not None and finished_at >= until:
                continue
            applied_by_source[src] = applied_by_source.get(src, 0) + 1

        all_sources: set[str] = (
            set(fetched_by_source) | set(matched_by_source) | set(applied_by_source)
        )
        if source is not None:
            # Honour the filter even when the source has no data
            # yet — keep the row so the response shape is stable.
            all_sources.add(source)

        rows: list[FunnelRow] = []
        for src in sorted(all_sources):
            rows.append(
                FunnelRow(
                    source=src,
                    fetched=fetched_by_source.get(src, 0),
                    matched=matched_by_source.get(src, 0),
                    accepted=accepted_by_source.get(src, 0),
                    applied=applied_by_source.get(src, 0),
                    rejected=rejected_by_source.get(src, 0),
                )
            )
        return rows

    def get_conversion(
        self,
        user_id: uuid.UUID,
        *,
        profile_id: uuid.UUID | None = None,
    ) -> list[ConversionRow]:
        """Return the per-profile conversion table for *user_id*.

        Each :class:`ConversionRow` reports:

        * ``matches`` / ``accepted`` / ``applied`` — raw counts for
          the profile.
        * ``accepted_rate`` — ``accepted / matches``, defaulting to
          ``0.0`` on a zero denominator.
        * ``applied_rate``  — ``applied / accepted``, defaulting to
          ``0.0`` on a zero denominator.

        ``profile_id`` limits the result to a single profile; when
        omitted every active profile the user owns is reported.
        """
        profiles = list(self._profile_repo.list_by_user(user_id))
        if profile_id is not None:
            profiles = [p for p in profiles if getattr(p, "id", None) == profile_id]

        matches = list(self._match_repo.list_by_user(user_id))
        applications = list(self._apply_job_repo.list_by_user(user_id, limit=10_000))

        match_count_by_profile: dict[uuid.UUID, int] = {}
        accepted_count_by_profile: dict[uuid.UUID, int] = {}
        for m in matches:
            match_count_by_profile[m.search_profile_id] = (
                match_count_by_profile.get(m.search_profile_id, 0) + 1
            )
            if m.status == MatchStatus.ACCEPTED.value:
                accepted_count_by_profile[m.search_profile_id] = (
                    accepted_count_by_profile.get(m.search_profile_id, 0) + 1
                )

        # ``applied`` joins ApplyJob to VacancyMatch via
        # ``match_id``; we resolve the profile through the match.
        match_to_profile: dict[uuid.UUID, uuid.UUID] = {m.id: m.search_profile_id for m in matches}
        applied_count_by_profile: dict[uuid.UUID, int] = {}
        for job in applications:
            if not _apply_job_is_terminal(job):
                continue
            pid = match_to_profile.get(job.match_id)
            if pid is None:
                continue
            applied_count_by_profile[pid] = applied_count_by_profile.get(pid, 0) + 1

        rows: list[ConversionRow] = []
        for profile in profiles:
            pid: uuid.UUID = profile.id
            matches_count = match_count_by_profile.get(pid, 0)
            accepted_count = accepted_count_by_profile.get(pid, 0)
            applied_count = applied_count_by_profile.get(pid, 0)
            accepted_rate = (accepted_count / matches_count) if matches_count else 0.0
            applied_rate = (applied_count / accepted_count) if accepted_count else 0.0
            rows.append(
                ConversionRow(
                    profile_id=pid,
                    matches=matches_count,
                    accepted=accepted_count,
                    applied=applied_count,
                    accepted_rate=accepted_rate,
                    applied_rate=applied_rate,
                )
            )
        return rows

    def get_time_to_apply(
        self,
        user_id: uuid.UUID,
        *,
        source: str | None = None,
        profile_id: uuid.UUID | None = None,
    ) -> TimeToApplyStats | None:
        """Return the average + median time-to-apply for *user_id*.

        The metric is the wall-clock delta between
        :attr:`VacancyMatch.created_at` and
        :attr:`ApplyJob.finished_at` for every terminal-state
        :class:`ApplyJob` the user owns. Returns ``None`` when no
        data is available so the API layer can serialise the empty
        case as ``null``.

        ``source`` and ``profile_id`` are applied as filters on the
        underlying match (source via the match's vacancy;
        ``profile_id`` directly on :attr:`VacancyMatch.search_profile_id`).
        """
        matches = list(self._match_repo.list_by_user(user_id))
        if profile_id is not None:
            matches = [m for m in matches if m.search_profile_id == profile_id]

        applications = list(self._apply_job_repo.list_by_user(user_id, limit=10_000))
        terminals = [j for j in applications if _apply_job_is_terminal(j)]

        # Resolve the source of each match (if a vacancy lookup is
        # needed) and pre-build a per-match index.
        match_by_id: dict[uuid.UUID, VacancyMatch] = {m.id: m for m in matches}
        if source is not None:
            vacancies = self._all_vacancies()
            vacancies_by_id = {v.id: v for v in vacancies}
        else:
            vacancies_by_id = {}

        deltas_seconds: list[float] = []
        for job in terminals:
            match = match_by_id.get(job.match_id)
            if match is None:
                continue
            if source is not None:
                vacancy = vacancies_by_id.get(match.vacancy_id)
                if vacancy is None or vacancy.source != source:
                    continue
            finished_at = job.finished_at
            created_at = match.created_at
            if finished_at is None or created_at is None:
                continue
            delta = (finished_at - created_at).total_seconds()
            if delta < 0:
                # Clock skew or a manually-edited row; drop the
                # negative-delta sample so it does not skew the
                # mean / median downwards.
                continue
            deltas_seconds.append(delta)

        if not deltas_seconds:
            return None

        return TimeToApplyStats(
            average_seconds=statistics.fmean(deltas_seconds),
            median_seconds=float(statistics.median(deltas_seconds)),
            sample_size=len(deltas_seconds),
        )

    # -- helpers ----------------------------------------------------------

    def _all_vacancies(self) -> Sequence[Vacancy]:
        """Return every :class:`Vacancy` in the repo.

        The dashboard's analytics aggregations need a full
        vacancy-by-id index to resolve :class:`VacancyMatch.vacancy_id`
        → source. The SQL implementation's :meth:`list_recent` is
        used as the cheapest "give me everything" call; the in-memory
        implementation just returns the underlying dict. The limit is
        generous so the entire catalogue fits; the slice is a
        low-traffic read endpoint and the rows are small.
        """
        return list(self._vacancy_repo.list_recent(limit=1_000_000))

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
