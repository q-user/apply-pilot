"""TDD tests for the dashboard slice (M6, issue #51).

The dashboard aggregates per-user counts from several repos. The service
is exercised through the in-memory fakes so the slice contract is
verified end-to-end without an external database. The two API-level
tests run the real FastAPI app against an in-memory sqlite engine and
go through the /auth/* endpoints to obtain a bearer token.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

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
from job_apply.features.cover_letter.models import CoverLetterDraft, CoverLetterDraftStatus
from job_apply.features.cover_letter.repository import InMemoryCoverLetterDraftRepository
from job_apply.features.dashboard.service import DashboardService
from job_apply.features.matches import models as _matches_models  # noqa: F401
from job_apply.features.matches.models import MatchStatus, VacancyMatch
from job_apply.features.matches.repository import InMemoryVacancyMatchRepository
from job_apply.features.search_profiles import models as _sp_models  # noqa: F401
from job_apply.features.search_profiles.models import SearchProfile
from job_apply.features.search_profiles.repository import InMemorySearchProfileRepository
from job_apply.features.sources import models as _sources_models  # noqa: F401
from job_apply.features.sources.repository import InMemoryVacancyRepository
from job_apply.features.telegram import models as _telegram_models  # noqa: F401
from job_apply.features.telegram.repository import InMemoryTelegramAccountRepository
from job_apply.features.users import models as _users_models  # noqa: F401
from job_apply.features.users.api import router as auth_router
from job_apply.features.users.models import User
from job_apply.features.users.repository import InMemoryUsersRepository
from job_apply.features.users.security import hash_password

# ---------------------------------------------------------------------------
# Helpers / fixtures — service-level (in-memory fakes only)
# ---------------------------------------------------------------------------


def _profile(user_id: uuid.UUID, *, is_active: bool = True) -> SearchProfile:
    p = SearchProfile(user_id=user_id, title="Python", is_active=is_active)
    p.id = uuid.uuid4()
    return p


def _match(profile_id: uuid.UUID, *, status: str = MatchStatus.NEW.value) -> VacancyMatch:
    m = VacancyMatch(
        search_profile_id=profile_id,
        vacancy_id=uuid.uuid4(),
        status=status,
    )
    m.id = uuid.uuid4()
    return m


def _apply_job(user_id: uuid.UUID, *, status: str = ApplyJobStatus.QUEUED.value) -> ApplyJob:
    j = ApplyJob(
        match_id=uuid.uuid4(),
        user_id=user_id,
        vacancy_id=uuid.uuid4(),
        status=status,
    )
    j.id = uuid.uuid4()
    return j


def _draft(user_id: uuid.UUID) -> CoverLetterDraft:
    d = CoverLetterDraft(
        match_id=uuid.uuid4(),
        user_id=user_id,
        content="hello",
        prompt_version="cover_letter@v1",
        status=CoverLetterDraftStatus.DRAFT.value,
    )
    d.id = uuid.uuid4()
    return d


def _user(*, email: str = "u@example.com") -> User:
    u = User(id=uuid.uuid4(), email=email, hashed_password=hash_password("pw"))
    u.created_at = u.created_at or __import__("datetime").datetime.now(__import__("datetime").UTC)
    return u


@pytest.fixture
def user_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def other_user_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def profile_repo() -> InMemorySearchProfileRepository:
    return InMemorySearchProfileRepository()


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
def vacancy_repo() -> InMemoryVacancyRepository:
    return InMemoryVacancyRepository()


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
    # The dashboard reuses the digest's :class:`StatsService` for the
    # ``digest`` field of the summary, so we wire the telegram and
    # user repos in too. The service builds the :class:`StatsService`
    # lazily on the first call.
    return DashboardService(
        match_repo=match_repo,
        apply_job_repo=apply_job_repo,
        cover_letter_repo=cover_letter_repo,
        vacancy_repo=vacancy_repo,
        profile_repo=profile_repo,
        telegram_account_repo=telegram_repo,
        user_repo=users_repo,
    )


# ---------------------------------------------------------------------------
# Service-level tests
# ---------------------------------------------------------------------------


def test_dashboard_returns_user_scoped_counts(
    service: DashboardService,
    profile_repo: InMemorySearchProfileRepository,
    match_repo: InMemoryVacancyMatchRepository,
    apply_job_repo: InMemoryApplyJobRepository,
    cover_letter_repo: InMemoryCoverLetterDraftRepository,
    user_id: uuid.UUID,
) -> None:
    """The summary for an empty user must be all-zero across the board."""
    profile = _profile(user_id)
    profile_repo.create(profile)
    match_repo.create(_match(profile.id))
    match_repo.create(_match(profile.id, status=MatchStatus.ACCEPTED.value))
    apply_job_repo.create(_apply_job(user_id, status=ApplyJobStatus.SUCCEEDED.value))
    apply_job_repo.create(_apply_job(user_id, status=ApplyJobStatus.QUEUED.value))
    cover_letter_repo.create(_draft(user_id))

    summary = service.get_summary(user_id)

    # Matches: 2 (one new, one accepted)
    assert summary.matches_total == 2
    assert summary.matches_by_status[MatchStatus.NEW.value] == 1
    assert summary.matches_by_status[MatchStatus.ACCEPTED.value] == 1
    # Applications: 2 (one queued, one succeeded)
    assert summary.applications_total == 2
    assert summary.applications_by_status[ApplyJobStatus.QUEUED.value] == 1
    assert summary.applications_by_status[ApplyJobStatus.SUCCEEDED.value] == 1
    # Cover letters: 1
    assert summary.cover_letter_drafts_total == 1
    # Active profiles: 1
    assert summary.search_profiles_active == 1
    # The digest is embedded for convenience.
    assert summary.digest is not None
    assert summary.digest.matches_total == 2


def test_dashboard_counts_matches_by_status(
    service: DashboardService,
    profile_repo: InMemorySearchProfileRepository,
    match_repo: InMemoryVacancyMatchRepository,
    user_id: uuid.UUID,
) -> None:
    """``matches_by_status`` must bucket matches by every :class:`MatchStatus` value."""
    profile = _profile(user_id)
    profile_repo.create(profile)
    # Seed one match for every status — the bucket for each one should be 1.
    for status in MatchStatus:
        match_repo.create(_match(profile.id, status=status.value))

    summary = service.get_summary(user_id)

    for status in MatchStatus:
        assert summary.matches_by_status[status.value] == 1, status.value
    assert summary.matches_total == len(MatchStatus)


def test_dashboard_counts_applications_by_status(
    service: DashboardService,
    apply_job_repo: InMemoryApplyJobRepository,
    user_id: uuid.UUID,
) -> None:
    """``applications_by_status`` must bucket apply jobs by every :class:`ApplyJobStatus` value."""
    # Seed one apply job for every status — the bucket for each should be 1.
    for status in ApplyJobStatus:
        apply_job_repo.create(_apply_job(user_id, status=status.value))

    summary = service.get_summary(user_id)

    for status in ApplyJobStatus:
        assert summary.applications_by_status[status.value] == 1, status.value
    assert summary.applications_total == len(ApplyJobStatus)


def test_dashboard_counts_active_search_profiles(
    service: DashboardService,
    profile_repo: InMemorySearchProfileRepository,
    user_id: uuid.UUID,
) -> None:
    """``search_profiles_active`` must only count profiles with ``is_active=True``."""
    active_one = _profile(user_id, is_active=True)
    active_two = _profile(user_id, is_active=True)
    inactive = _profile(user_id, is_active=False)
    profile_repo.create(active_one)
    profile_repo.create(active_two)
    profile_repo.create(inactive)

    summary = service.get_summary(user_id)

    assert summary.search_profiles_active == 2


def test_dashboard_counts_cover_letter_drafts(
    service: DashboardService,
    cover_letter_repo: InMemoryCoverLetterDraftRepository,
    user_id: uuid.UUID,
) -> None:
    """``cover_letter_drafts_total`` must equal the number of drafts owned by the user."""
    for _ in range(3):
        cover_letter_repo.create(_draft(user_id))

    summary = service.get_summary(user_id)

    assert summary.cover_letter_drafts_total == 3


def test_dashboard_isolates_users(
    service: DashboardService,
    profile_repo: InMemorySearchProfileRepository,
    match_repo: InMemoryVacancyMatchRepository,
    apply_job_repo: InMemoryApplyJobRepository,
    cover_letter_repo: InMemoryCoverLetterDraftRepository,
    user_id: uuid.UUID,
    other_user_id: uuid.UUID,
) -> None:
    """Counts must be scoped to the requested user, not bleed across users."""
    # Seed data for *both* users; only the caller-relevant counts should show up.
    mine = _profile(user_id)
    theirs = _profile(other_user_id)
    profile_repo.create(mine)
    profile_repo.create(theirs)
    match_repo.create(_match(mine.id, status=MatchStatus.ACCEPTED.value))
    match_repo.create(_match(theirs.id, status=MatchStatus.ACCEPTED.value))
    match_repo.create(_match(theirs.id, status=MatchStatus.REJECTED.value))
    apply_job_repo.create(_apply_job(user_id, status=ApplyJobStatus.SUCCEEDED.value))
    apply_job_repo.create(_apply_job(other_user_id, status=ApplyJobStatus.SUCCEEDED.value))
    cover_letter_repo.create(_draft(user_id))
    cover_letter_repo.create(_draft(other_user_id))
    cover_letter_repo.create(_draft(other_user_id))

    mine_summary = service.get_summary(user_id)
    theirs_summary = service.get_summary(other_user_id)

    assert mine_summary.matches_total == 1
    assert mine_summary.matches_by_status[MatchStatus.ACCEPTED.value] == 1
    assert mine_summary.matches_by_status[MatchStatus.REJECTED.value] == 0
    assert mine_summary.applications_total == 1
    assert mine_summary.cover_letter_drafts_total == 1
    assert mine_summary.search_profiles_active == 1

    assert theirs_summary.matches_total == 2
    assert theirs_summary.matches_by_status[MatchStatus.ACCEPTED.value] == 1
    assert theirs_summary.matches_by_status[MatchStatus.REJECTED.value] == 1
    assert theirs_summary.applications_total == 1
    assert theirs_summary.cover_letter_drafts_total == 2
    assert theirs_summary.search_profiles_active == 1


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

    # Import the router lazily so a failure to import the slice (during
    # the TDD red phase) does not mask itself behind a fixture error.
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
    return _register_and_login(client, "dashboard-user@example.com", "hunter2!!")


def test_api_dashboard_endpoint_requires_auth(client: TestClient) -> None:
    """``GET /dashboard`` without a bearer token must return 401."""
    response = client.get("/dashboard")
    assert response.status_code == 401


def test_api_dashboard_endpoint_returns_summary(token: str, client: TestClient) -> None:
    """``GET /dashboard`` with a valid token must return the summary shape."""
    response = client.get("/dashboard", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    body = response.json()

    # Every documented field must be present, even when counts are zero.
    assert body["matches_total"] == 0
    assert isinstance(body["matches_by_status"], dict)
    for status in MatchStatus:
        assert body["matches_by_status"][status.value] == 0
    assert body["applications_total"] == 0
    assert isinstance(body["applications_by_status"], dict)
    for status in ApplyJobStatus:
        assert body["applications_by_status"][status.value] == 0
    assert body["cover_letter_drafts_total"] == 0
    assert body["search_profiles_active"] == 0
    # The digest is always populated in production wiring; the shape
    # mirrors the ``/digest`` message fields.
    assert isinstance(body["digest"], dict)
    assert body["digest"]["matches_total"] == 0
    assert body["digest"]["digest_date"]
