"""Integration tests for the ``GET /apply-history`` endpoint (M6, issue #54).

The endpoint returns the caller's :class:`ApplyStatusHistory` rows
across **all** of their apply jobs — a combined view of every status
transition the apply worker has produced. It complements the
per-job ``GET /apply-jobs/{id}/history`` endpoint with a flat,
dashboard-friendly list that supports pagination, job-id and to_status
filters, and strict user scoping.

The tests stand up a FastAPI app with the apply-history router
mounted and exercise the full request / response cycle through
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
from job_apply.features.apply_worker.models import ApplyJobStatus
from job_apply.features.apply_worker.repository import (
    InMemoryApplyJobRepository,
    InMemoryApplyStatusHistoryRepository,
)
from job_apply.features.matches.models import VacancyMatch
from job_apply.features.search_profiles.models import SearchProfile
from job_apply.features.users.security import issue_token

MAX_LIMIT = 200
DEFAULT_LIMIT = 50


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
    """State shared between the apply-history router and the tests."""

    job_repo: InMemoryApplyJobRepository
    history_repo: InMemoryApplyStatusHistoryRepository
    match_repo: _FakeMatchRepo
    profile_repo: _FakeProfileRepo
    user_id: uuid.UUID
    profile: SearchProfile
    match: VacancyMatch
    service: ApplyJobService


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_user_world() -> _World:
    """Build an isolated ``_World`` for a fresh user.

    Factored out so the user-isolation test can call it twice with
    independent ids without inheriting the ``apply_world`` fixture's
    single-user state.
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

    # The in-memory history repo needs a job→user resolver so its
    # ``list_by_user`` query (M6, #54) can scope rows to the caller
    # without going through SQL. The lambda consults the in-memory
    # job repo that lives in the same world.
    history_repo = InMemoryApplyStatusHistoryRepository(
        get_job_user_id=lambda job_id: (
            job_repo.get_by_id(job_id).user_id if job_repo.get_by_id(job_id) is not None else None
        ),
    )
    service = ApplyJobService(
        job_repo=job_repo,  # type: ignore[arg-type]
        match_repo=match_repo,  # type: ignore[arg-type]
        profile_repo=profile_repo,  # type: ignore[arg-type]
        history_repo=history_repo,
    )

    return _World(
        job_repo=job_repo,
        history_repo=history_repo,
        match_repo=match_repo,
        profile_repo=profile_repo,
        user_id=user_id,
        profile=profile,
        match=match,
        service=service,
    )


@pytest.fixture
def apply_world() -> _World:
    """In-memory fakes shared between the router and the test."""
    return _build_user_world()


@pytest.fixture
def app(apply_world: _World) -> Iterator[FastAPI]:
    """FastAPI app with the apply-history router mounted and DI overridden.

    The apply-history router is a separate ``APIRouter`` living in
    :mod:`job_apply.features.apply_worker.api` so the public path is
    ``/apply-history`` (not ``/apply-jobs/apply-history``). The same
    ``get_apply_job_service`` dependency is overridden so the
    collaborator-injected in-memory fakes are wired.
    """
    from job_apply.features.apply_worker.api import (
        apply_history_router,
        get_apply_job_service,
    )

    application = FastAPI()
    application.include_router(apply_history_router)
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


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _make_match_for_profile(world: _World, profile: SearchProfile | None = None) -> VacancyMatch:
    """Create and register a match owned by ``profile`` (defaults to world.profile)."""
    p = profile or world.profile
    match = VacancyMatch(
        id=uuid.uuid4(),
        search_profile_id=p.id,
        vacancy_id=uuid.uuid4(),
        status="accepted",
    )
    world.match_repo.matches[match.id] = match
    return match


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


def test_history_requires_token(client: TestClient) -> None:
    """A missing bearer token returns 401."""
    response = client.get("/apply-history")
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "authentication_required"


# ---------------------------------------------------------------------------
# User scoping
# ---------------------------------------------------------------------------


def test_history_returns_user_scoped_rows(
    client: TestClient, token: str, apply_world: _World
) -> None:
    """``GET /apply-history`` returns the caller's history across all jobs."""
    job_one = apply_world.service.enqueue_for_match(apply_world.match.id)
    second_match = _make_match_for_profile(apply_world)
    job_two = apply_world.service.enqueue_for_match(second_match.id)

    response = client.get("/apply-history", headers=_auth_headers(token))
    assert response.status_code == 200, response.json()
    payload = response.json()

    # Two jobs, one ``queued`` transition each.
    assert payload["total"] == 2
    assert {row["job_id"] for row in payload["items"]} == {str(job_one.id), str(job_two.id)}
    assert all(row["from_status"] is None for row in payload["items"])
    assert all(row["to_status"] == ApplyJobStatus.QUEUED.value for row in payload["items"])


def test_history_isolates_users() -> None:
    """The endpoint must not return rows for a different user.

    This test is fully self-contained — it builds two independent
    worlds (one for the bearer-token caller, one for a second user)
    and wires a fresh ``TestClient`` so the standard fixtures (which
    are bound to a single world) don't interfere with the
    user-isolation assertion.
    """
    caller_world = _build_user_world()
    other_world = _build_user_world()

    from job_apply.features.apply_worker.api import (
        apply_history_router,
        get_apply_job_service,
    )

    application = FastAPI()
    application.include_router(apply_history_router)
    application.dependency_overrides[get_apply_job_service] = lambda: caller_world.service
    test_client = TestClient(application)
    test_client.__enter__()
    try:
        caller_token = issue_token(str(caller_world.user_id), ttl_seconds=3600)

        # The caller has a single ``queued`` transition. The other
        # user has a full enqueue+claim+complete lifecycle that
        # produces three transitions. The endpoint must only ever
        # return the caller's row.
        caller_world.service.enqueue_for_match(caller_world.match.id)
        other_job = other_world.service.enqueue_for_match(other_world.match.id)
        other_world.service.claim_next()
        other_world.service.complete(other_job.id, external_application_id="hh-x")

        response = test_client.get("/apply-history", headers=_auth_headers(caller_token))
        assert response.status_code == 200, response.json()
        payload = response.json()
        assert payload["total"] == 1
        assert len(payload["items"]) == 1
        # The single returned row belongs to the caller's job, never
        # to the other user's job.
        assert all(row["job_id"] != str(other_job.id) for row in payload["items"])
    finally:
        test_client.__exit__(None, None, None)
        application.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def test_history_filters_by_job_id(client: TestClient, token: str, apply_world: _World) -> None:
    """The ``job_id`` query parameter narrows the result to a single job."""
    job_one = apply_world.service.enqueue_for_match(apply_world.match.id)
    second_match = _make_match_for_profile(apply_world)
    job_two = apply_world.service.enqueue_for_match(second_match.id)

    response = client.get(f"/apply-history?job_id={job_one.id}", headers=_auth_headers(token))
    assert response.status_code == 200, response.json()
    payload = response.json()
    assert payload["total"] == 1
    assert len(payload["items"]) == 1
    assert payload["items"][0]["job_id"] == str(job_one.id)
    assert payload["items"][0]["job_id"] != str(job_two.id)


def test_history_filters_by_to_status(client: TestClient, token: str, apply_world: _World) -> None:
    """The ``status`` query parameter filters by ``to_status``."""
    job = apply_world.service.enqueue_for_match(apply_world.match.id)
    # ``claim_next`` writes a ``queued`` → ``running`` transition.
    apply_world.service.claim_next()
    # ``complete`` writes a ``running`` → ``succeeded`` transition.
    apply_world.service.complete(job.id, external_application_id="hh-1")

    response = client.get("/apply-history?status=succeeded", headers=_auth_headers(token))
    assert response.status_code == 200, response.json()
    payload = response.json()
    assert payload["total"] == 1
    assert len(payload["items"]) == 1
    assert payload["items"][0]["to_status"] == ApplyJobStatus.SUCCEEDED.value
    assert payload["items"][0]["job_id"] == str(job.id)


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def test_history_pagination(client: TestClient, token: str, apply_world: _World) -> None:
    """``limit`` / ``offset`` paginate the user's full history."""
    # Each job produces one ``queued`` history row on enqueue, plus a
    # ``running`` row on claim, plus a ``succeeded`` row on complete.
    # We build three jobs and claim+complete two of them so the
    # total row count is 3 (jobs) * 1 (enqueue) + 2 (claim) + 2
    # (complete) = 7 rows. We then paginate with ``limit=3``.
    jobs = []
    for _ in range(3):
        match = _make_match_for_profile(apply_world)
        jobs.append(apply_world.service.enqueue_for_match(match.id))
    # First two jobs: claim + complete.
    apply_world.service.claim_next()  # first
    apply_world.service.claim_next()  # second
    apply_world.service.complete(jobs[0].id, external_application_id="hh-1")
    apply_world.service.complete(jobs[1].id, external_application_id="hh-2")

    first_page = client.get("/apply-history?limit=3&offset=0", headers=_auth_headers(token))
    assert first_page.status_code == 200, first_page.json()
    first_payload = first_page.json()
    assert first_payload["total"] == 7
    assert len(first_payload["items"]) == 3

    second_page = client.get("/apply-history?limit=3&offset=3", headers=_auth_headers(token))
    assert second_page.status_code == 200, second_page.json()
    second_payload = second_page.json()
    assert second_payload["total"] == 7
    assert len(second_payload["items"]) == 3

    third_page = client.get("/apply-history?limit=3&offset=6", headers=_auth_headers(token))
    assert third_page.status_code == 200, third_page.json()
    third_payload = third_page.json()
    assert third_payload["total"] == 7
    assert len(third_payload["items"]) == 1

    # The three pages together cover the full set, with no overlap.
    seen_ids = {
        row["id"]
        for page in (first_payload, second_payload, third_payload)
        for row in page["items"]
    }
    assert len(seen_ids) == 7


# ---------------------------------------------------------------------------
# Metadata passthrough
# ---------------------------------------------------------------------------


def test_history_includes_metadata(client: TestClient, token: str, apply_world: _World) -> None:
    """Metadata written by the service round-trips through the API as a dict."""
    job = apply_world.service.enqueue_for_match(apply_world.match.id)
    apply_world.service.claim_next()
    # ``fail`` writes a transition whose ``metadata`` includes the
    # retryable flag and the bumped attempt counter.
    apply_world.service.fail(job.id, error="hh 5xx", retryable=False)

    response = client.get("/apply-history", headers=_auth_headers(token))
    assert response.status_code == 200, response.json()
    payload = response.json()
    assert payload["total"] == 3  # queued, running, dead_letter

    dead_letter_row = next(row for row in payload["items"] if row["to_status"] == "dead_letter")
    assert dead_letter_row["error"] == "hh 5xx"
    assert dead_letter_row["metadata"] is not None
    assert dead_letter_row["metadata"]["retryable"] is False
    assert "attempts" in dead_letter_row["metadata"]
