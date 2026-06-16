"""Integration tests for the ``/apply-jobs/*`` HTTP endpoints (M5, issue #43).

These tests stand up a FastAPI app with the apply-jobs router mounted
and exercise the full request / response cycle through
:class:`fastapi.testclient.TestClient`. The service is wired with the
in-memory fakes so the test can inspect and manipulate state without
going through the SQL path. Authentication uses the in-memory token
issuer (``users.security.issue_token``) so the bearer-token plumbing is
the real one from the users slice without paying for a
``/auth/register`` + ``/auth/login`` round-trip per test.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from job_apply.features.apply_worker import ApplyJobService
from job_apply.features.apply_worker.repository import InMemoryApplyJobRepository
from job_apply.features.apply_worker.schemas import ApplyJobRead
from job_apply.features.matches.models import VacancyMatch
from job_apply.features.search_profiles.models import SearchProfile
from job_apply.features.users.security import issue_token

# ---------------------------------------------------------------------------
# Local fakes (replacing the cross-slice lookups the service depends on)
# ---------------------------------------------------------------------------


@dataclass
class _FakeMatchRepo:
    """In-memory match repository exposing only ``get_by_id``."""

    matches: dict[uuid.UUID, VacancyMatch] = field(default_factory=dict)

    def get_by_id(self, match_id: uuid.UUID) -> VacancyMatch | None:
        return self.matches.get(match_id)


@dataclass
class _FakeProfileRepo:
    """In-memory search-profile repository exposing only ``get_by_id``."""

    profiles: dict[uuid.UUID, SearchProfile] = field(default_factory=dict)

    def get_by_id(self, profile_id: uuid.UUID) -> SearchProfile | None:
        return self.profiles.get(profile_id)


@dataclass
class _World:
    """State shared between the apply-jobs router and the tests."""

    job_repo: InMemoryApplyJobRepository
    match_repo: _FakeMatchRepo
    profile_repo: _FakeProfileRepo
    user_id: uuid.UUID
    profile: SearchProfile
    match: VacancyMatch
    service: ApplyJobService


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def apply_world() -> _World:
    """In-memory fakes shared between the router and the test.

    The user id is supplied by the ``token`` fixture so the bearer
    token the test sends resolves to the same user the match's
    search profile is owned by. Using the in-memory token issuer
    (``users.security.issue_token``) keeps the test independent of
    the /auth/register + /auth/login round-trip.
    """
    user_id = uuid.uuid4()
    profile = SearchProfile(
        id=uuid.uuid4(),
        user_id=user_id,
        title="Senior Python",
        keywords="python, fastapi",
        is_active=True,
    )
    match = VacancyMatch(
        id=uuid.uuid4(),
        search_profile_id=profile.id,
        vacancy_id=uuid.uuid4(),
        status="accepted",
    )

    job_repo = InMemoryApplyJobRepository()
    match_repo = _FakeMatchRepo()
    match_repo.matches[match.id] = match
    profile_repo = _FakeProfileRepo()
    profile_repo.profiles[profile.id] = profile

    # M5 #49 — the service now requires a history repo. The router's
    # in-memory fakes use the same collaborator-injected fakes as the
    # rest of the test world.
    from job_apply.features.apply_worker.repository import (
        InMemoryApplyStatusHistoryRepository,
    )

    history_repo = InMemoryApplyStatusHistoryRepository()
    service = ApplyJobService(
        job_repo=job_repo,  # type: ignore[arg-type]
        match_repo=match_repo,  # type: ignore[arg-type]
        profile_repo=profile_repo,  # type: ignore[arg-type]
        history_repo=history_repo,
    )

    return _World(
        job_repo=job_repo,
        match_repo=match_repo,
        profile_repo=profile_repo,
        user_id=user_id,
        profile=profile,
        match=match,
        service=service,
    )


@pytest.fixture
def app(apply_world: _World) -> Iterator[FastAPI]:
    application = FastAPI()
    from job_apply.features.apply_worker.api import (
        get_apply_job_service,
    )
    from job_apply.features.apply_worker.api import (
        router as apply_worker_router,
    )

    application.include_router(apply_worker_router)
    application.dependency_overrides[get_apply_job_service] = lambda: apply_world.service

    try:
        yield application
    finally:
        application.dependency_overrides.clear()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def token(apply_world: _World) -> str:
    """A bearer token resolving to ``apply_world.user_id``."""
    return issue_token(str(apply_world.user_id), ttl_seconds=3600)


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


def test_endpoints_require_token(client: TestClient) -> None:
    """Every apply-jobs endpoint must reject requests without a bearer token."""
    assert client.get("/apply-jobs").status_code == 401
    assert client.get(f"/apply-jobs/{uuid.uuid4()}").status_code == 401
    assert client.get(f"/apply-jobs/{uuid.uuid4()}/history").status_code == 401
    assert client.post(f"/apply-jobs/{uuid.uuid4()}/cancel").status_code == 401
    assert client.post(f"/apply-jobs/enqueue/{uuid.uuid4()}").status_code == 401


# ---------------------------------------------------------------------------
# POST /apply-jobs/enqueue/{match_id}
# ---------------------------------------------------------------------------


def test_enqueue_creates_a_queued_job(client: TestClient, token: str, apply_world: _World) -> None:
    """Enqueueing a match persists a job and returns it."""
    response = client.post(
        f"/apply-jobs/enqueue/{apply_world.match.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 201, response.json()

    payload = response.json()
    assert payload["match_id"] == str(apply_world.match.id)
    assert payload["user_id"] == str(apply_world.user_id)
    assert payload["status"] == "queued"
    assert payload["attempts"] == 0
    assert payload["external_application_id"] is None
    # The in-memory repo holds exactly one row.
    assert len(list(apply_world.job_repo.list_by_user(apply_world.user_id))) == 1


def test_enqueue_is_idempotent(client: TestClient, token: str, apply_world: _World) -> None:
    """A second enqueue for the same match returns the same job."""
    first = client.post(
        f"/apply-jobs/enqueue/{apply_world.match.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    second = client.post(
        f"/apply-jobs/enqueue/{apply_world.match.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    # Exactly one job for the user.
    listed = client.get("/apply-jobs", headers={"Authorization": f"Bearer {token}"}).json()
    assert len(listed) == 1


def test_enqueue_404_when_match_missing(client: TestClient, token: str) -> None:
    """A missing match returns 404."""
    response = client.post(
        f"/apply-jobs/enqueue/{uuid.uuid4()}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "apply_job_dependency_missing"


# ---------------------------------------------------------------------------
# GET /apply-jobs
# ---------------------------------------------------------------------------


def test_list_returns_only_the_callers_jobs(
    client: TestClient, token: str, apply_world: _World
) -> None:
    """The list endpoint shows the caller's jobs newest first."""
    other_user_id = uuid.uuid4()
    other_profile = SearchProfile(
        id=uuid.uuid4(),
        user_id=other_user_id,
        title="Other",
        keywords="x",
        is_active=True,
    )
    other_match = VacancyMatch(
        id=uuid.uuid4(),
        search_profile_id=other_profile.id,
        vacancy_id=uuid.uuid4(),
        status="accepted",
    )
    apply_world.match_repo.matches[other_match.id] = other_match
    apply_world.profile_repo.profiles[other_profile.id] = other_profile

    apply_world.service.enqueue_for_match(apply_world.match.id)
    apply_world.service.enqueue_for_match(other_match.id)

    response = client.get("/apply-jobs", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    listed = response.json()
    assert len(listed) == 1
    assert listed[0]["match_id"] == str(apply_world.match.id)
    assert listed[0]["user_id"] == str(apply_world.user_id)


# ---------------------------------------------------------------------------
# GET /apply-jobs/{id}
# ---------------------------------------------------------------------------


def test_get_returns_owners_job(client: TestClient, token: str, apply_world: _World) -> None:
    """A job owned by the caller is returned with the full DTO."""
    job = apply_world.service.enqueue_for_match(apply_world.match.id)

    response = client.get(f"/apply-jobs/{job.id}", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == str(job.id)
    assert payload["status"] == "queued"


def test_get_returns_403_for_other_users_job(
    client: TestClient, token: str, apply_world: _World
) -> None:
    """A job owned by someone else returns 403 to the caller.

    The slice uses the same ownership pattern as :mod:`features.matches`:
    the service raises :class:`ApplyJobOwnershipError` when the caller's
    user id does not match the job's owner, and the API translates the
    error to a 403 response.
    """
    other_job = _make_other_user_job(apply_world)

    response = client.get(
        f"/apply-jobs/{other_job.id}", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "forbidden"


def test_get_404_when_job_missing(client: TestClient, token: str) -> None:
    response = client.get(
        f"/apply-jobs/{uuid.uuid4()}", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# POST /apply-jobs/{id}/cancel
# ---------------------------------------------------------------------------


def test_cancel_transitions_queued_job(client: TestClient, token: str, apply_world: _World) -> None:
    """Cancelling a queued job returns the cancelled row."""
    job = apply_world.service.enqueue_for_match(apply_world.match.id)

    response = client.post(
        f"/apply-jobs/{job.id}/cancel",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "cancelled"
    assert payload["finished_at"] is not None


def test_cancel_409_when_terminal(client: TestClient, token: str, apply_world: _World) -> None:
    """A succeeded job cannot be cancelled."""
    job = apply_world.service.enqueue_for_match(apply_world.match.id)
    apply_world.service.claim_next()
    apply_world.service.complete(job.id, external_application_id="hh-1")

    response = client.post(
        f"/apply-jobs/{job.id}/cancel",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "apply_job_already_terminal"


def test_cancel_404_when_job_missing(client: TestClient, token: str) -> None:
    response = client.post(
        f"/apply-jobs/{uuid.uuid4()}/cancel",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# DTO
# ---------------------------------------------------------------------------


def test_dto_round_trip(apply_world: _World) -> None:
    """``ApplyJobRead.model_validate`` accepts an ORM row."""
    job = apply_world.service.enqueue_for_match(apply_world.match.id)
    dto = ApplyJobRead.model_validate(job)
    assert dto.id == job.id
    assert dto.status == "queued"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_other_user_job(world: _World) -> object:
    """Create a job owned by a different user and return it."""
    other_user_id = uuid.uuid4()
    other_match = VacancyMatch(
        id=uuid.uuid4(),
        search_profile_id=world.profile.id,
        vacancy_id=uuid.uuid4(),
        status="accepted",
    )
    # Build an ApplyJob directly so we can stamp a foreign user id.
    from job_apply.features.apply_worker.models import (
        ApplyJob,
        compute_idempotency_key,
    )

    other = ApplyJob(
        match_id=other_match.id,
        user_id=other_user_id,
        vacancy_id=other_match.vacancy_id,
    )
    other.idempotency_key = compute_idempotency_key(
        other_user_id, other_match.vacancy_id, other_match.id
    )
    return world.job_repo.create(other)
