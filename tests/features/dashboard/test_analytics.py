"""TDD tests for the dashboard analytics endpoints (M8, issue #67).

The slice extends the existing :mod:`job_apply.features.dashboard` package
with three new read-only endpoints:

* ``GET /dashboard/funnel``           — funnel counts per source
* ``GET /dashboard/conversion``       — conversion per search profile
* ``GET /dashboard/time-to-apply``    — average + median time-to-apply

The tests exercise the service through the in-memory fakes (per the VSA
DI rule) and round-trip the SQL aggregations through a sqlite in-memory
engine — both the service contract and the SQL path are covered.

The slice stays read-only: no writers, no migrations, no new tables.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from job_apply.db import Base, get_db
from job_apply.features.apply_worker import models as _apply_worker_models  # noqa: F401
from job_apply.features.apply_worker.models import ApplyJob, ApplyJobStatus
from job_apply.features.apply_worker.repository import InMemoryApplyJobRepository
from job_apply.features.cover_letter import models as _cl_models  # noqa: F401
from job_apply.features.cover_letter.repository import InMemoryCoverLetterDraftRepository
from job_apply.features.dashboard.service import DashboardService
from job_apply.features.matches import models as _matches_models  # noqa: F401
from job_apply.features.matches.models import MatchStatus, VacancyMatch
from job_apply.features.matches.repository import InMemoryVacancyMatchRepository
from job_apply.features.search_profiles import models as _sp_models  # noqa: F401
from job_apply.features.search_profiles.models import SearchProfile
from job_apply.features.search_profiles.repository import InMemorySearchProfileRepository
from job_apply.features.sources import models as _sources_models  # noqa: F401
from job_apply.features.sources.models import Vacancy
from job_apply.features.sources.repository import InMemoryVacancyRepository
from job_apply.features.telegram import models as _telegram_models  # noqa: F401
from job_apply.features.telegram.repository import InMemoryTelegramAccountRepository
from job_apply.features.users import models as _users_models  # noqa: F401
from job_apply.features.users.api import router as auth_router
from job_apply.features.users.repository import InMemoryUsersRepository
from job_apply.features.users.security import hash_password

# ---------------------------------------------------------------------------
# Helpers / fixtures — service-level (in-memory fakes only)
# ---------------------------------------------------------------------------


_TERMINAL_APPLY_STATUSES = frozenset(
    {
        ApplyJobStatus.SUCCEEDED.value,
        ApplyJobStatus.FAILED.value,
        ApplyJobStatus.DEAD_LETTER.value,
        ApplyJobStatus.CANCELLED.value,
    }
)


def _profile(user_id: uuid.UUID, *, is_active: bool = True) -> SearchProfile:
    p = SearchProfile(user_id=user_id, title="Python", is_active=is_active)
    p.id = uuid.uuid4()
    return p


def _vacancy(*, source: str, source_id: str | None = None) -> Vacancy:
    v = Vacancy(
        source=source,
        source_id=source_id or f"{source}-{uuid.uuid4()}",
        title=f"Job at {source}",
        raw_data={},
    )
    v.id = uuid.uuid4()
    return v


def _match(
    profile_id: uuid.UUID,
    vacancy_id: uuid.UUID,
    *,
    status: str = MatchStatus.NEW.value,
    created_at: datetime | None = None,
) -> VacancyMatch:
    m = VacancyMatch(
        search_profile_id=profile_id,
        vacancy_id=vacancy_id,
        status=status,
    )
    m.id = uuid.uuid4()
    if created_at is not None:
        m.created_at = created_at
    return m


def _apply_job(
    user_id: uuid.UUID,
    match_id: uuid.UUID,
    vacancy_id: uuid.UUID,
    *,
    status: str = ApplyJobStatus.SUCCEEDED.value,
    finished_at: datetime | None = None,
) -> ApplyJob:
    j = ApplyJob(
        match_id=match_id,
        user_id=user_id,
        vacancy_id=vacancy_id,
        status=status,
    )
    j.id = uuid.uuid4()
    if finished_at is not None:
        j.finished_at = finished_at
    elif status in _TERMINAL_APPLY_STATUSES:
        # Terminal-state jobs in the funnel / time-to-apply tests
        # are always expected to carry a ``finished_at``; the
        # service filters on that field to decide whether a job is
        # in the metric. The in-memory ``ApplyJobRepository`` does
        # not default the value for us, so we set it here so the
        # service can see the row.
        j.finished_at = datetime.now(UTC)
    return j


@pytest.fixture
def user_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def profile_repo() -> InMemorySearchProfileRepository:
    return InMemorySearchProfileRepository()


@pytest.fixture
def vacancy_repo() -> InMemoryVacancyRepository:
    return InMemoryVacancyRepository()


@pytest.fixture
def match_repo(profile_repo: InMemorySearchProfileRepository) -> InMemoryVacancyMatchRepository:
    return InMemoryVacancyMatchRepository(list_user_profiles=profile_repo.list_by_user)


@pytest.fixture
def apply_job_repo() -> InMemoryApplyJobRepository:
    return InMemoryApplyJobRepository()


@pytest.fixture
def cover_letter_repo() -> InMemoryCoverLetterDraftRepository:
    return InMemoryCoverLetterDraftRepository()


@pytest.fixture
def users_repo() -> InMemoryUsersRepository:
    return InMemoryUsersRepository()


@pytest.fixture
def telegram_repo() -> InMemoryTelegramAccountRepository:
    return InMemoryTelegramAccountRepository()


@pytest.fixture
def service(
    match_repo: InMemoryVacancyMatchRepository,
    apply_job_repo: InMemoryApplyJobRepository,
    cover_letter_repo: InMemoryCoverLetterDraftRepository,
    vacancy_repo: InMemoryVacancyRepository,
    profile_repo: InMemorySearchProfileRepository,
    users_repo: InMemoryUsersRepository,
    telegram_repo: InMemoryTelegramAccountRepository,
) -> DashboardService:
    return DashboardService(
        match_repo=match_repo,
        apply_job_repo=apply_job_repo,
        cover_letter_repo=cover_letter_repo,
        vacancy_repo=vacancy_repo,
        profile_repo=profile_repo,
        telegram_account_repo=telegram_repo,
        user_repo=users_repo,
    )


def _seed_funnel_data(
    *,
    vacancy_repo: InMemoryVacancyRepository,
    profile_repo: InMemorySearchProfileRepository,
    match_repo: InMemoryVacancyMatchRepository,
    apply_job_repo: InMemoryApplyJobRepository,
    user_id: uuid.UUID,
) -> dict[str, uuid.UUID]:
    """Seed a small funnel for two sources and return useful ids."""
    profile = _profile(user_id)
    profile_repo.create(profile)

    # hh: 2 vacancies, 2 matches, 1 accepted, 1 applied, 1 rejected
    hh_v1 = vacancy_repo.upsert(_vacancy(source="hh", source_id="hh-1"))
    hh_v2 = vacancy_repo.upsert(_vacancy(source="hh", source_id="hh-2"))
    hh_m_accepted = match_repo.create(
        _match(profile.id, hh_v1.id, status=MatchStatus.ACCEPTED.value)
    )
    match_repo.create(_match(profile.id, hh_v2.id, status=MatchStatus.REJECTED.value))
    apply_job_repo.create(_apply_job(user_id, hh_m_accepted.id, hh_v1.id))

    # habr: 1 vacancy, 1 match, 0 accepted, 0 applied, 0 rejected
    habr_v = vacancy_repo.upsert(_vacancy(source="habr", source_id="habr-1"))
    match_repo.create(_match(profile.id, habr_v.id, status=MatchStatus.NEW.value))

    return {
        "profile_id": profile.id,
        "hh_v1": hh_v1.id,
        "hh_v2": hh_v2.id,
        "habr_v": habr_v.id,
    }


# ---------------------------------------------------------------------------
# Funnel tests — service level
# ---------------------------------------------------------------------------


def test_funnel_counts_per_source(
    service: DashboardService,
    vacancy_repo: InMemoryVacancyRepository,
    profile_repo: InMemorySearchProfileRepository,
    match_repo: InMemoryVacancyMatchRepository,
    apply_job_repo: InMemoryApplyJobRepository,
    user_id: uuid.UUID,
) -> None:
    """The funnel returns one row per source with all five counts."""
    _seed_funnel_data(
        vacancy_repo=vacancy_repo,
        profile_repo=profile_repo,
        match_repo=match_repo,
        apply_job_repo=apply_job_repo,
        user_id=user_id,
    )

    rows = service.get_funnel(user_id)
    rows_by_source = {row.source: row for row in rows}

    assert set(rows_by_source) == {"hh", "habr"}

    hh = rows_by_source["hh"]
    assert hh.fetched == 2
    assert hh.matched == 2
    assert hh.accepted == 1
    assert hh.applied == 1
    assert hh.rejected == 1

    habr = rows_by_source["habr"]
    assert habr.fetched == 1
    assert habr.matched == 1
    assert habr.accepted == 0
    assert habr.applied == 0
    assert habr.rejected == 0


def test_funnel_scopes_to_user(
    service: DashboardService,
    vacancy_repo: InMemoryVacancyRepository,
    profile_repo: InMemorySearchProfileRepository,
    match_repo: InMemoryVacancyMatchRepository,
    apply_job_repo: InMemoryApplyJobRepository,
    user_id: uuid.UUID,
) -> None:
    """The funnel never bleeds another user's data into the result."""
    _seed_funnel_data(
        vacancy_repo=vacancy_repo,
        profile_repo=profile_repo,
        match_repo=match_repo,
        apply_job_repo=apply_job_repo,
        user_id=user_id,
    )
    other = uuid.uuid4()
    other_profile = _profile(other)
    profile_repo.create(other_profile)
    other_v = vacancy_repo.upsert(_vacancy(source="hh", source_id="hh-other"))
    other_m = match_repo.create(
        _match(other_profile.id, other_v.id, status=MatchStatus.ACCEPTED.value)
    )
    apply_job_repo.create(_apply_job(other, other_m.id, other_v.id))

    rows = service.get_funnel(user_id)
    hh = next(r for r in rows if r.source == "hh")
    # Only the original 2 hh vacancies / 2 matches belong to *user_id*.
    assert hh.fetched == 2
    assert hh.matched == 2
    assert hh.accepted == 1
    assert hh.applied == 1


def test_funnel_filters_by_source_name(
    service: DashboardService,
    vacancy_repo: InMemoryVacancyRepository,
    profile_repo: InMemorySearchProfileRepository,
    match_repo: InMemoryVacancyMatchRepository,
    apply_job_repo: InMemoryApplyJobRepository,
    user_id: uuid.UUID,
) -> None:
    """The ``source`` filter limits the funnel to a single source."""
    _seed_funnel_data(
        vacancy_repo=vacancy_repo,
        profile_repo=profile_repo,
        match_repo=match_repo,
        apply_job_repo=apply_job_repo,
        user_id=user_id,
    )

    rows = service.get_funnel(user_id, source="hh")

    assert [r.source for r in rows] == ["hh"]
    assert rows[0].fetched == 2


def test_funnel_filters_by_date_range(
    service: DashboardService,
    vacancy_repo: InMemoryVacancyRepository,
    profile_repo: InMemorySearchProfileRepository,
    match_repo: InMemoryVacancyMatchRepository,
    apply_job_repo: InMemoryApplyJobRepository,
    user_id: uuid.UUID,
) -> None:
    """``since`` / ``until`` exclude rows created outside the window."""
    profile = _profile(user_id)
    profile_repo.create(profile)

    now = datetime.now(UTC)
    old_v = vacancy_repo.upsert(_vacancy(source="hh", source_id="hh-old"))
    old_v.created_at = now - timedelta(days=30)
    new_v = vacancy_repo.upsert(_vacancy(source="hh", source_id="hh-new"))
    new_v.created_at = now - timedelta(days=1)

    old_match = match_repo.create(_match(profile.id, old_v.id, status=MatchStatus.ACCEPTED.value))
    old_match.created_at = now - timedelta(days=30)
    new_match = match_repo.create(_match(profile.id, new_v.id, status=MatchStatus.ACCEPTED.value))
    new_match.created_at = now - timedelta(days=1)

    apply_job_repo.create(
        _apply_job(
            user_id,
            new_match.id,
            new_v.id,
            finished_at=now - timedelta(hours=12),
        )
    )

    rows = service.get_funnel(user_id, source="hh", since=now - timedelta(days=7), until=now)
    assert len(rows) == 1
    assert rows[0].fetched == 1
    assert rows[0].matched == 1
    assert rows[0].accepted == 1
    assert rows[0].applied == 1


def test_funnel_excludes_non_terminal_apply_jobs(
    service: DashboardService,
    vacancy_repo: InMemoryVacancyRepository,
    profile_repo: InMemorySearchProfileRepository,
    match_repo: InMemoryVacancyMatchRepository,
    apply_job_repo: InMemoryApplyJobRepository,
    user_id: uuid.UUID,
) -> None:
    """The ``applied`` count only includes terminal ApplyJob states."""
    profile = _profile(user_id)
    profile_repo.create(profile)
    v = vacancy_repo.upsert(_vacancy(source="hh", source_id="hh-1"))
    m = match_repo.create(_match(profile.id, v.id, status=MatchStatus.ACCEPTED.value))
    # queued + running must NOT count, succeeded + failed + dead_letter + cancelled must.
    apply_job_repo.create(_apply_job(user_id, m.id, v.id, status=ApplyJobStatus.QUEUED.value))
    apply_job_repo.create(_apply_job(user_id, m.id, v.id, status=ApplyJobStatus.RUNNING.value))
    # The model UNIQUE(match_id) constraint means we can only create one
    # ApplyJob per match; create extra matches to cover the remaining
    # terminal statuses instead.
    for status in (
        ApplyJobStatus.SUCCEEDED.value,
        ApplyJobStatus.FAILED.value,
        ApplyJobStatus.DEAD_LETTER.value,
        ApplyJobStatus.CANCELLED.value,
    ):
        v2 = vacancy_repo.upsert(_vacancy(source="hh", source_id=f"hh-{status}"))
        m2 = match_repo.create(_match(profile.id, v2.id, status=MatchStatus.ACCEPTED.value))
        apply_job_repo.create(_apply_job(user_id, m2.id, v2.id, status=status))

    rows = service.get_funnel(user_id, source="hh")
    assert rows[0].applied == 4


def test_funnel_returns_empty_when_no_data(
    service: DashboardService,
    user_id: uuid.UUID,
) -> None:
    """A user with no vacancies / matches / jobs gets an empty funnel."""
    assert service.get_funnel(user_id) == []


# ---------------------------------------------------------------------------
# Conversion tests — service level
# ---------------------------------------------------------------------------


def test_conversion_per_profile(
    service: DashboardService,
    vacancy_repo: InMemoryVacancyRepository,
    profile_repo: InMemorySearchProfileRepository,
    match_repo: InMemoryVacancyMatchRepository,
    apply_job_repo: InMemoryApplyJobRepository,
    user_id: uuid.UUID,
) -> None:
    """The conversion table reports matches/accepted/applied plus rates per profile."""
    profile = _profile(user_id)
    profile_repo.create(profile)

    # 4 matches, 2 accepted, 1 applied
    v1 = vacancy_repo.upsert(_vacancy(source="hh", source_id="hh-1"))
    v2 = vacancy_repo.upsert(_vacancy(source="hh", source_id="hh-2"))
    v3 = vacancy_repo.upsert(_vacancy(source="hh", source_id="hh-3"))
    v4 = vacancy_repo.upsert(_vacancy(source="hh", source_id="hh-4"))
    m1 = match_repo.create(_match(profile.id, v1.id, status=MatchStatus.ACCEPTED.value))
    match_repo.create(_match(profile.id, v2.id, status=MatchStatus.ACCEPTED.value))
    match_repo.create(_match(profile.id, v3.id, status=MatchStatus.REJECTED.value))
    match_repo.create(_match(profile.id, v4.id, status=MatchStatus.NEW.value))
    apply_job_repo.create(_apply_job(user_id, m1.id, v1.id))

    rows = service.get_conversion(user_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.profile_id == profile.id
    assert row.matches == 4
    assert row.accepted == 2
    assert row.applied == 1
    assert row.accepted_rate == pytest.approx(0.5)
    assert row.applied_rate == pytest.approx(0.5)


def test_conversion_filters_by_profile_id(
    service: DashboardService,
    vacancy_repo: InMemoryVacancyRepository,
    profile_repo: InMemorySearchProfileRepository,
    match_repo: InMemoryVacancyMatchRepository,
    apply_job_repo: InMemoryApplyJobRepository,
    user_id: uuid.UUID,
) -> None:
    """Passing ``profile_id`` returns just that profile's row."""
    p_a = _profile(user_id)
    p_b = _profile(user_id)
    profile_repo.create(p_a)
    profile_repo.create(p_b)

    va = vacancy_repo.upsert(_vacancy(source="hh", source_id="hh-a"))
    vb = vacancy_repo.upsert(_vacancy(source="hh", source_id="hh-b"))
    ma = match_repo.create(_match(p_a.id, va.id, status=MatchStatus.ACCEPTED.value))
    mb = match_repo.create(_match(p_b.id, vb.id, status=MatchStatus.ACCEPTED.value))
    apply_job_repo.create(_apply_job(user_id, ma.id, va.id))
    apply_job_repo.create(_apply_job(user_id, mb.id, vb.id))

    rows = service.get_conversion(user_id, profile_id=p_a.id)

    assert len(rows) == 1
    assert rows[0].profile_id == p_a.id
    assert rows[0].matches == 1
    assert rows[0].accepted == 1
    assert rows[0].applied == 1


def test_conversion_rates_handle_zero_denominators(
    service: DashboardService,
    vacancy_repo: InMemoryVacancyRepository,
    profile_repo: InMemorySearchProfileRepository,
    match_repo: InMemoryVacancyMatchRepository,
    user_id: uuid.UUID,
) -> None:
    """Empty profiles report ``0.0`` rates instead of raising."""
    profile = _profile(user_id)
    profile_repo.create(profile)

    rows = service.get_conversion(user_id)
    assert len(rows) == 1
    assert rows[0].matches == 0
    assert rows[0].accepted == 0
    assert rows[0].applied == 0
    assert rows[0].accepted_rate == 0.0
    assert rows[0].applied_rate == 0.0


def test_conversion_scopes_to_user(
    service: DashboardService,
    vacancy_repo: InMemoryVacancyRepository,
    profile_repo: InMemorySearchProfileRepository,
    match_repo: InMemoryVacancyMatchRepository,
    apply_job_repo: InMemoryApplyJobRepository,
    user_id: uuid.UUID,
) -> None:
    """Conversion never includes another user's profile data."""
    mine = _profile(user_id)
    profile_repo.create(mine)
    v = vacancy_repo.upsert(_vacancy(source="hh", source_id="hh-1"))
    m = match_repo.create(_match(mine.id, v.id, status=MatchStatus.ACCEPTED.value))
    apply_job_repo.create(_apply_job(user_id, m.id, v.id))

    other = uuid.uuid4()
    other_p = _profile(other)
    profile_repo.create(other_p)
    other_v = vacancy_repo.upsert(_vacancy(source="hh", source_id="hh-other"))
    other_m = match_repo.create(_match(other_p.id, other_v.id, status=MatchStatus.ACCEPTED.value))
    apply_job_repo.create(_apply_job(other, other_m.id, other_v.id))

    rows = service.get_conversion(user_id)
    assert [r.profile_id for r in rows] == [mine.id]


# ---------------------------------------------------------------------------
# Time-to-apply tests — service level
# ---------------------------------------------------------------------------


def test_time_to_apply_average_and_median(
    service: DashboardService,
    vacancy_repo: InMemoryVacancyRepository,
    profile_repo: InMemorySearchProfileRepository,
    match_repo: InMemoryVacancyMatchRepository,
    apply_job_repo: InMemoryApplyJobRepository,
    user_id: uuid.UUID,
) -> None:
    """The metric returns the average and median delta for terminal jobs."""
    profile = _profile(user_id)
    profile_repo.create(profile)

    base = datetime(2026, 1, 1, tzinfo=UTC)
    deltas_seconds = [600, 1200, 1800]  # 10, 20, 30 minutes; median 1200
    expected_avg = sum(deltas_seconds) / len(deltas_seconds)
    for i, delta in enumerate(deltas_seconds):
        v = vacancy_repo.upsert(_vacancy(source="hh", source_id=f"hh-{i}"))
        m = match_repo.create(_match(profile.id, v.id, status=MatchStatus.ACCEPTED.value))
        m.created_at = base
        apply_job_repo.create(
            _apply_job(user_id, m.id, v.id, finished_at=base + timedelta(seconds=delta))
        )

    stats = service.get_time_to_apply(user_id)
    assert stats is not None
    assert stats.sample_size == 3
    assert stats.average_seconds == pytest.approx(expected_avg)
    assert stats.median_seconds == pytest.approx(1200.0)


def test_time_to_apply_returns_none_when_no_data(
    service: DashboardService,
    user_id: uuid.UUID,
) -> None:
    """No terminal apply jobs → ``None`` (the endpoint serialises as null)."""
    assert service.get_time_to_apply(user_id) is None


def test_time_to_apply_ignores_non_terminal_jobs(
    service: DashboardService,
    vacancy_repo: InMemoryVacancyRepository,
    profile_repo: InMemorySearchProfileRepository,
    match_repo: InMemoryVacancyMatchRepository,
    apply_job_repo: InMemoryApplyJobRepository,
    user_id: uuid.UUID,
) -> None:
    """Queued / running jobs do not contribute to the metric."""
    profile = _profile(user_id)
    profile_repo.create(profile)
    base = datetime(2026, 1, 1, tzinfo=UTC)

    # A queued job must not count.
    v = vacancy_repo.upsert(_vacancy(source="hh", source_id="hh-1"))
    m = match_repo.create(_match(profile.id, v.id, status=MatchStatus.ACCEPTED.value))
    m.created_at = base
    apply_job_repo.create(
        _apply_job(user_id, m.id, v.id, status=ApplyJobStatus.QUEUED.value, finished_at=base)
    )

    # A succeeded job (delta 900s) must count and is the only data point.
    v2 = vacancy_repo.upsert(_vacancy(source="hh", source_id="hh-2"))
    m2 = match_repo.create(_match(profile.id, v2.id, status=MatchStatus.ACCEPTED.value))
    m2.created_at = base
    apply_job_repo.create(
        _apply_job(
            user_id,
            m2.id,
            v2.id,
            status=ApplyJobStatus.SUCCEEDED.value,
            finished_at=base + timedelta(seconds=900),
        )
    )

    stats = service.get_time_to_apply(user_id)
    assert stats is not None
    assert stats.sample_size == 1
    assert stats.average_seconds == pytest.approx(900.0)
    assert stats.median_seconds == pytest.approx(900.0)


def test_time_to_apply_filters_by_source(
    service: DashboardService,
    vacancy_repo: InMemoryVacancyRepository,
    profile_repo: InMemorySearchProfileRepository,
    match_repo: InMemoryVacancyMatchRepository,
    apply_job_repo: InMemoryApplyJobRepository,
    user_id: uuid.UUID,
) -> None:
    """``source`` limits the metric to a single source."""
    profile = _profile(user_id)
    profile_repo.create(profile)
    base = datetime(2026, 1, 1, tzinfo=UTC)

    # Two sources, one job each.
    for source, delta in (("hh", 600), ("habr", 1200)):
        v = vacancy_repo.upsert(_vacancy(source=source, source_id=f"{source}-1"))
        m = match_repo.create(_match(profile.id, v.id, status=MatchStatus.ACCEPTED.value))
        m.created_at = base
        apply_job_repo.create(
            _apply_job(user_id, m.id, v.id, finished_at=base + timedelta(seconds=delta))
        )

    stats = service.get_time_to_apply(user_id, source="hh")
    assert stats is not None
    assert stats.sample_size == 1
    assert stats.average_seconds == pytest.approx(600.0)


def test_time_to_apply_filters_by_profile(
    service: DashboardService,
    vacancy_repo: InMemoryVacancyRepository,
    profile_repo: InMemorySearchProfileRepository,
    match_repo: InMemoryVacancyMatchRepository,
    apply_job_repo: InMemoryApplyJobRepository,
    user_id: uuid.UUID,
) -> None:
    """``profile_id`` limits the metric to matches owned by that profile."""
    p_a = _profile(user_id)
    p_b = _profile(user_id)
    profile_repo.create(p_a)
    profile_repo.create(p_b)
    base = datetime(2026, 1, 1, tzinfo=UTC)

    for profile, delta in ((p_a, 600), (p_b, 1200)):
        v = vacancy_repo.upsert(_vacancy(source="hh", source_id=f"hh-{profile.id}"))
        m = match_repo.create(_match(profile.id, v.id, status=MatchStatus.ACCEPTED.value))
        m.created_at = base
        apply_job_repo.create(
            _apply_job(user_id, m.id, v.id, finished_at=base + timedelta(seconds=delta))
        )

    stats = service.get_time_to_apply(user_id, profile_id=p_a.id)
    assert stats is not None
    assert stats.sample_size == 1
    assert stats.average_seconds == pytest.approx(600.0)


# ---------------------------------------------------------------------------
# API-level tests (real FastAPI + sqlite in-memory engine)
# ---------------------------------------------------------------------------


def _register_and_login(client: TestClient, email: str, password: str) -> str:
    resp = client.post("/auth/register", json={"email": email, "password": password})
    assert resp.status_code == 201, resp.json()
    resp = client.post("/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.json()
    return resp.json()["access_token"]


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session_factory(engine: Engine):
    return sessionmaker(bind=engine, class_=Session, autocommit=False, autoflush=False)


@pytest.fixture
def app(session_factory) -> Iterator[FastAPI]:
    def _override_get_db() -> Iterator[Session]:
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    from job_apply.features.dashboard.api import router as dashboard_router

    application = FastAPI()
    application.include_router(auth_router)
    application.include_router(dashboard_router)
    application.dependency_overrides[get_db] = _override_get_db

    try:
        yield application
    finally:
        application.dependency_overrides.clear()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def token(client: TestClient) -> str:
    return _register_and_login(client, "analytics-user@example.com", "hunter2!!")


def test_api_funnel_requires_auth(client: TestClient) -> None:
    """``GET /dashboard/funnel`` without a bearer token must return 401."""
    response = client.get("/dashboard/funnel")
    assert response.status_code == 401


def test_api_funnel_returns_rows(token: str, client: TestClient) -> None:
    """The funnel endpoint returns the documented shape even on empty data."""
    response = client.get("/dashboard/funnel", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    body = response.json()
    assert body == {"rows": [], "filters": {"source": None, "since": None, "until": None}}


def test_api_funnel_filters_by_source(token: str, client: TestClient) -> None:
    """The funnel endpoint honours the ``source`` query param.

    The response always carries a row for the requested source so
    the front-end can confirm the filter was applied even on an
    empty data set; the row's counts are all zero in that case.
    """
    response = client.get(
        "/dashboard/funnel",
        params={"source": "hh"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["filters"]["source"] == "hh"
    assert body["rows"] == [
        {
            "source": "hh",
            "fetched": 0,
            "matched": 0,
            "accepted": 0,
            "applied": 0,
            "rejected": 0,
        }
    ]


def test_api_funnel_rejects_invalid_date(token: str, client: TestClient) -> None:
    """An unparseable ``since`` returns 422 from FastAPI's validator."""
    response = client.get(
        "/dashboard/funnel",
        params={"since": "not-a-date"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422


def test_api_conversion_requires_auth(client: TestClient) -> None:
    """``GET /dashboard/conversion`` without a bearer token must return 401."""
    response = client.get("/dashboard/conversion")
    assert response.status_code == 401


def test_api_conversion_returns_rows(token: str, client: TestClient) -> None:
    """The conversion endpoint returns the documented shape on empty data."""
    response = client.get("/dashboard/conversion", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    body = response.json()
    assert body == {"rows": []}


def test_api_time_to_apply_requires_auth(client: TestClient) -> None:
    """``GET /dashboard/time-to-apply`` without a bearer token must return 401."""
    response = client.get("/dashboard/time-to-apply")
    assert response.status_code == 401


def test_api_time_to_apply_returns_null_when_no_data(token: str, client: TestClient) -> None:
    """An empty queue serialises the metric as ``null`` (not ``{}``)."""
    response = client.get("/dashboard/time-to-apply", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json() is None


# ---------------------------------------------------------------------------
# Sqlite round-trip — SQL aggregations exercised end-to-end
# ---------------------------------------------------------------------------


@pytest.fixture
def sql_service(session_factory) -> DashboardService:
    """Build a :class:`DashboardService` wired against the sqlite engine.

    The service's SQL aggregation path is exercised by inserting
    rows directly with the session, then calling the same service
    methods as the unit tests. Coverage of the Python-side branch is
    handled by the in-memory fakes above.
    """
    from job_apply.features.apply_worker.repository import SqlApplyJobRepository
    from job_apply.features.cover_letter.repository import SqlCoverLetterDraftRepository
    from job_apply.features.matches.repository import SqlVacancyMatchRepository
    from job_apply.features.search_profiles.repository import SqlSearchProfileRepository
    from job_apply.features.sources.repository import SqlVacancyRepository
    from job_apply.features.telegram.repository import (
        SqlAlchemyTelegramAccountRepository,
    )
    from job_apply.features.users.repository import SqlAlchemyUsersRepository

    # Repos that accept ``session=`` share the same session so the
    # SQL aggregations see the rows we insert below. The remaining
    # repos use ``session_factory=`` and open short-lived sessions
    # per call — that is fine for an aggregation read path.
    return DashboardService(
        match_repo=SqlVacancyMatchRepository(session_factory=session_factory),
        apply_job_repo=SqlApplyJobRepository(session_factory=session_factory),
        cover_letter_repo=SqlCoverLetterDraftRepository(session_factory=session_factory),
        vacancy_repo=SqlVacancyRepository(session_factory=session_factory),
        profile_repo=SqlSearchProfileRepository(session_factory=session_factory),
        telegram_account_repo=SqlAlchemyTelegramAccountRepository(session_factory=session_factory),
        user_repo=SqlAlchemyUsersRepository(session_factory=session_factory),
    )


def _insert_user(session: Session, *, email: str = "sql@example.com") -> uuid.UUID:
    from job_apply.features.users.models import User

    user = User(
        id=uuid.uuid4(),
        email=email,
        hashed_password=hash_password("pw"),
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user.id


def _insert_profile(session: Session, user_id: uuid.UUID) -> uuid.UUID:
    profile = SearchProfile(user_id=user_id, title="sql", is_active=True)
    profile.id = uuid.uuid4()
    session.add(profile)
    session.commit()
    session.refresh(profile)
    return profile.id


def _insert_vacancy(session: Session, *, source: str) -> uuid.UUID:
    v = Vacancy(
        source=source,
        source_id=f"{source}-{uuid.uuid4()}",
        title=f"Job at {source}",
        raw_data={},
    )
    v.id = uuid.uuid4()
    session.add(v)
    session.commit()
    session.refresh(v)
    return v.id


def _insert_match(
    session: Session, *, profile_id: uuid.UUID, vacancy_id: uuid.UUID, status: str
) -> uuid.UUID:
    m = VacancyMatch(
        search_profile_id=profile_id,
        vacancy_id=vacancy_id,
        status=status,
    )
    m.id = uuid.uuid4()
    session.add(m)
    session.commit()
    session.refresh(m)
    return m.id


def _insert_apply_job(
    session: Session,
    *,
    user_id: uuid.UUID,
    match_id: uuid.UUID,
    vacancy_id: uuid.UUID,
    status: str,
    finished_at: datetime | None = None,
) -> None:
    from job_apply.features.apply_worker.models import (
        ApplyJob,
        compute_idempotency_key,
    )

    j = ApplyJob(
        match_id=match_id,
        user_id=user_id,
        vacancy_id=vacancy_id,
        status=status,
        idempotency_key=compute_idempotency_key(user_id, vacancy_id, match_id),
    )
    j.id = uuid.uuid4()
    if finished_at is not None:
        j.finished_at = finished_at
    session.add(j)
    session.commit()
    session.refresh(j)


def test_sql_funnel_runs_against_sqlite(session_factory, sql_service: DashboardService) -> None:
    """The SQL path of the funnel returns the same shape as the in-memory path."""
    session = session_factory()
    try:
        user_id = _insert_user(session)
        profile_id = _insert_profile(session, user_id)
        v1 = _insert_vacancy(session, source="hh")
        v2 = _insert_vacancy(session, source="hh")
        m1 = _insert_match(
            session, profile_id=profile_id, vacancy_id=v1, status=MatchStatus.ACCEPTED.value
        )
        _insert_match(
            session, profile_id=profile_id, vacancy_id=v2, status=MatchStatus.REJECTED.value
        )
        _insert_apply_job(
            session,
            user_id=user_id,
            match_id=m1,
            vacancy_id=v1,
            status=ApplyJobStatus.SUCCEEDED.value,
            finished_at=datetime.now(UTC),
        )

        rows = sql_service.get_funnel(user_id)
        hh = next(r for r in rows if r.source == "hh")
        assert hh.fetched == 2
        assert hh.matched == 2
        assert hh.accepted == 1
        assert hh.rejected == 1
        assert hh.applied == 1
    finally:
        session.close()


def test_sql_conversion_runs_against_sqlite(session_factory, sql_service: DashboardService) -> None:
    """The SQL path of the conversion table returns the same shape as the in-memory path."""
    session = session_factory()
    try:
        user_id = _insert_user(session)
        profile_id = _insert_profile(session, user_id)
        v1 = _insert_vacancy(session, source="hh")
        v2 = _insert_vacancy(session, source="hh")
        m1 = _insert_match(
            session, profile_id=profile_id, vacancy_id=v1, status=MatchStatus.ACCEPTED.value
        )
        _insert_match(session, profile_id=profile_id, vacancy_id=v2, status=MatchStatus.NEW.value)
        _insert_apply_job(
            session,
            user_id=user_id,
            match_id=m1,
            vacancy_id=v1,
            status=ApplyJobStatus.SUCCEEDED.value,
            finished_at=datetime.now(UTC),
        )

        rows = sql_service.get_conversion(user_id, profile_id=profile_id)
        assert len(rows) == 1
        assert rows[0].profile_id == profile_id
        assert rows[0].matches == 2
        assert rows[0].accepted == 1
        assert rows[0].applied == 1
    finally:
        session.close()


def test_sql_time_to_apply_runs_against_sqlite(
    session_factory, sql_service: DashboardService
) -> None:
    """The SQL path of time-to-apply returns the same shape as the in-memory path."""
    session = session_factory()
    try:
        user_id = _insert_user(session)
        profile_id = _insert_profile(session, user_id)
        base = datetime(2026, 1, 1, tzinfo=UTC)
        for delta in (600, 1200):
            v = _insert_vacancy(session, source="hh")
            m_id = _insert_match(
                session,
                profile_id=profile_id,
                vacancy_id=v,
                status=MatchStatus.ACCEPTED.value,
            )
            # Override the match's created_at via the session.
            match = session.get(VacancyMatch, m_id)
            assert match is not None
            match.created_at = base
            session.commit()
            _insert_apply_job(
                session,
                user_id=user_id,
                match_id=m_id,
                vacancy_id=v,
                status=ApplyJobStatus.SUCCEEDED.value,
                finished_at=base + timedelta(seconds=delta),
            )

        stats = sql_service.get_time_to_apply(user_id)
        assert stats is not None
        assert stats.sample_size == 2
        assert stats.average_seconds == pytest.approx(900.0)
        assert stats.median_seconds == pytest.approx(900.0)
    finally:
        session.close()
