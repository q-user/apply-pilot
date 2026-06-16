"""TDD tests for the :class:`ApplyStatusHistory` model and the
:class:`ApplyStatusHistoryRepository` (M5, issue #49).

The apply worker emits an append-only stream of status transitions; this
module covers the model contract and both repository implementations. The
service-level integration (history written on enqueue / claim / cancel /
complete / fail) lives alongside the slice's service tests.

Test surface
------------

The 8 test cases cover:

* :class:`ApplyStatusHistory` carries the documented columns and
  defaults (``id`` generated, ``from_status`` nullable for the initial
  creation row).
* ``InMemoryApplyStatusHistoryRepository.create`` returns the row with a
  fresh ``id`` and ``created_at``.
* ``InMemoryApplyStatusHistoryRepository.list_by_job`` returns the
  chronological history for a job, in the order the rows were written.
* ``ApplyJobService.enqueue_for_match`` writes a creation history row
  with ``from_status=None`` and ``to_status=queued``.
* ``ApplyJobService.claim_next`` writes a ``queued -> running`` history
  row.
* ``ApplyJobService.fail`` writes a history row that carries the error
  message and the new ``to_status``.
* The SQLAlchemy-backed implementation round-trips the row through a
  sqlite in-memory engine.
* ``GET /apply-jobs/{id}/history`` returns the history for a job the
  caller owns.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from job_apply.db import Base
from job_apply.features.apply_worker.models import (
    ApplyJobStatus,
)
from job_apply.features.apply_worker.repository import (
    InMemoryApplyJobRepository,
    SqlApplyStatusHistoryRepository,
)
from job_apply.features.apply_worker.service import ApplyJobService
from job_apply.features.matches.models import VacancyMatch
from job_apply.features.search_profiles.models import SearchProfile
from job_apply.features.users.security import issue_token

# Local alias to keep the import footprint narrow.
MatchStatus = __import__("job_apply.features.matches.models", fromlist=["MatchStatus"]).MatchStatus


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


def test_model_columns_and_defaults() -> None:
    """The model exposes the documented columns with the right nullability.

    ``from_status`` is the only column that allows ``NULL`` — it is unset
    on the initial creation row.
    """
    from job_apply.features.apply_worker.models import ApplyStatusHistory

    job_id = uuid.uuid4()
    row = ApplyStatusHistory(
        job_id=job_id,
        from_status=ApplyJobStatus.QUEUED.value,
        to_status=ApplyJobStatus.RUNNING.value,
    )

    assert row.id is not None
    assert row.job_id == job_id
    assert row.from_status == "queued"
    assert row.to_status == "running"
    assert row.error is None
    assert row.metadata_json is None
    assert row.created_at is not None


def test_model_allows_initial_creation_row() -> None:
    """The initial creation row has ``from_status=None``."""
    from job_apply.features.apply_worker.models import ApplyStatusHistory

    row = ApplyStatusHistory(
        job_id=uuid.uuid4(),
        from_status=None,
        to_status=ApplyJobStatus.QUEUED.value,
    )
    assert row.from_status is None
    assert row.to_status == "queued"


# ---------------------------------------------------------------------------
# In-memory repository
# ---------------------------------------------------------------------------


def test_in_memory_create_returns_persisted_row() -> None:
    """``create`` materialises ``id`` and ``created_at`` for the row."""
    from job_apply.features.apply_worker.models import ApplyStatusHistory
    from job_apply.features.apply_worker.repository import (
        InMemoryApplyStatusHistoryRepository,
    )

    repo = InMemoryApplyStatusHistoryRepository()
    row = ApplyStatusHistory(
        job_id=uuid.uuid4(),
        from_status=ApplyJobStatus.QUEUED.value,
        to_status=ApplyJobStatus.RUNNING.value,
    )

    created = repo.create(row)

    assert created is row
    assert created.id is not None
    assert created.created_at is not None
    assert created.from_status == "queued"
    assert created.to_status == "running"


def test_in_memory_list_by_job_orders_by_created_at() -> None:
    """``list_by_job`` returns rows in the order they were written.

    Three rows are written for the same job; the call must return them
    in chronological order (oldest first) so the API consumer reads the
    timeline forwards.
    """
    from job_apply.features.apply_worker.models import ApplyStatusHistory
    from job_apply.features.apply_worker.repository import (
        InMemoryApplyStatusHistoryRepository,
    )

    repo = InMemoryApplyStatusHistoryRepository()
    job_id = uuid.uuid4()
    other_job_id = uuid.uuid4()

    first = ApplyStatusHistory(
        job_id=job_id, from_status=None, to_status=ApplyJobStatus.QUEUED.value
    )
    first.created_at = datetime.now(UTC) - timedelta(seconds=2)
    second = ApplyStatusHistory(
        job_id=job_id,
        from_status=ApplyJobStatus.QUEUED.value,
        to_status=ApplyJobStatus.RUNNING.value,
    )
    second.created_at = datetime.now(UTC) - timedelta(seconds=1)
    third = ApplyStatusHistory(
        job_id=job_id,
        from_status=ApplyJobStatus.RUNNING.value,
        to_status=ApplyJobStatus.DEAD_LETTER.value,
        error="upstream outage",
    )
    third.created_at = datetime.now(UTC)
    repo.create(first)
    repo.create(second)
    repo.create(third)
    # A row for a different job that must not leak into the result.
    other = ApplyStatusHistory(
        job_id=other_job_id,
        from_status=None,
        to_status=ApplyJobStatus.QUEUED.value,
    )
    repo.create(other)

    listed = repo.list_by_job(job_id)

    assert [r.id for r in listed] == [first.id, second.id, third.id]
    assert listed[-1].error == "upstream outage"
    assert repo.list_by_job(other_job_id) == [other]


def test_in_memory_create_stamps_created_at_when_missing() -> None:
    """A row whose ``created_at`` is ``None`` is stamped with the current time.

    The model already fills ``created_at`` in ``__init__``; this test
    forces the ``None`` case to verify the repository's defensive
    fallback so a future refactor that drops the model default does
    not silently leave the in-memory rows without a timestamp.
    """
    from job_apply.features.apply_worker.models import ApplyStatusHistory
    from job_apply.features.apply_worker.repository import (
        InMemoryApplyStatusHistoryRepository,
    )

    repo = InMemoryApplyStatusHistoryRepository()
    row = ApplyStatusHistory(
        job_id=uuid.uuid4(),
        from_status=None,
        to_status=ApplyJobStatus.QUEUED.value,
    )
    # Simulate a model that did not fill the timestamp.
    row.created_at = None
    before = datetime.now(UTC)
    repo.create(row)
    after = datetime.now(UTC)

    assert row.created_at is not None
    assert before <= row.created_at <= after


# ---------------------------------------------------------------------------
# Service integration
# ---------------------------------------------------------------------------


@dataclass
class _FakeMatchRepo:
    """In-memory match repository exposing only ``get_by_id``."""

    matches: dict[uuid.UUID, VacancyMatch] = field(default_factory=dict)

    def get_by_id(self, match_id: uuid.UUID) -> VacancyMatch | None:
        return self.matches.get(match_id)

    def add(self, match: VacancyMatch) -> VacancyMatch:
        self.matches[match.id] = match
        return match


@dataclass
class _FakeProfileRepo:
    """In-memory search-profile repository exposing only ``get_by_id``."""

    profiles: dict[uuid.UUID, SearchProfile] = field(default_factory=dict)

    def get_by_id(self, profile_id: uuid.UUID) -> SearchProfile | None:
        return self.profiles.get(profile_id)

    def add(self, profile: SearchProfile) -> SearchProfile:
        self.profiles[profile.id] = profile
        return profile


@dataclass
class _World:
    """Tiny in-memory world wired up for one test."""

    user_id: uuid.UUID
    profile: SearchProfile
    vacancy_id: uuid.UUID
    match: VacancyMatch
    job_repo: InMemoryApplyJobRepository
    match_repo: _FakeMatchRepo
    profile_repo: _FakeProfileRepo
    service: ApplyJobService


@pytest.fixture
def world() -> _World:
    """Build a world with collaborator-injected in-memory fakes."""
    from job_apply.features.apply_worker.repository import (
        InMemoryApplyStatusHistoryRepository,
    )

    user_id = uuid.uuid4()
    profile = SearchProfile(
        id=uuid.uuid4(),
        user_id=user_id,
        title="Senior Python",
        keywords="python, fastapi",
        is_active=True,
    )
    vacancy_id = uuid.uuid4()
    match = VacancyMatch(
        id=uuid.uuid4(),
        search_profile_id=profile.id,
        vacancy_id=vacancy_id,
        status=MatchStatus.ACCEPTED.value,
    )

    match_repo = _FakeMatchRepo()
    match_repo.add(match)
    profile_repo = _FakeProfileRepo()
    profile_repo.add(profile)

    job_repo = InMemoryApplyJobRepository()
    history_repo = InMemoryApplyStatusHistoryRepository()
    service = ApplyJobService(
        job_repo=job_repo,
        match_repo=match_repo,  # type: ignore[arg-type]
        profile_repo=profile_repo,  # type: ignore[arg-type]
        history_repo=history_repo,
    )

    return _World(
        user_id=user_id,
        profile=profile,
        vacancy_id=vacancy_id,
        match=match,
        job_repo=job_repo,
        match_repo=match_repo,
        profile_repo=profile_repo,
        service=service,
    )


def test_job_creation_writes_initial_history(world: _World) -> None:
    """Enqueueing writes a single creation row with ``from_status=None``."""
    from job_apply.features.apply_worker.repository import (
        InMemoryApplyStatusHistoryRepository,
    )

    history_repo: InMemoryApplyStatusHistoryRepository = (
        world.service.history_repo  # type: ignore[assignment]
    )

    job = world.service.enqueue_for_match(world.match.id)
    rows = history_repo.list_by_job(job.id)

    assert len(rows) == 1
    assert rows[0].job_id == job.id
    assert rows[0].from_status is None
    assert rows[0].to_status == ApplyJobStatus.QUEUED.value


def test_claim_next_writes_history(world: _World) -> None:
    """``claim_next`` writes a ``queued -> running`` history row."""
    from job_apply.features.apply_worker.repository import (
        InMemoryApplyStatusHistoryRepository,
    )

    history_repo: InMemoryApplyStatusHistoryRepository = (
        world.service.history_repo  # type: ignore[assignment]
    )

    job = world.service.enqueue_for_match(world.match.id)
    world.service.claim_next()
    rows = history_repo.list_by_job(job.id)

    assert [r.to_status for r in rows] == [
        ApplyJobStatus.QUEUED.value,
        ApplyJobStatus.RUNNING.value,
    ]
    assert rows[1].from_status == ApplyJobStatus.QUEUED.value


def test_complete_writes_history(world: _World) -> None:
    """``complete`` writes a ``running -> succeeded`` history row."""
    from job_apply.features.apply_worker.repository import (
        InMemoryApplyStatusHistoryRepository,
    )

    history_repo: InMemoryApplyStatusHistoryRepository = (
        world.service.history_repo  # type: ignore[assignment]
    )

    job = world.service.enqueue_for_match(world.match.id)
    world.service.claim_next()
    world.service.complete(job.id, external_application_id="hh-1")
    rows = history_repo.list_by_job(job.id)

    assert [r.to_status for r in rows] == [
        ApplyJobStatus.QUEUED.value,
        ApplyJobStatus.RUNNING.value,
        ApplyJobStatus.SUCCEEDED.value,
    ]
    assert rows[2].from_status == ApplyJobStatus.RUNNING.value


def test_failure_writes_history_with_error(world: _World) -> None:
    """``fail`` writes a history row carrying the error message.

    For ``retryable=False`` the row transitions to ``dead_letter``; for
    ``retryable=True`` it transitions back to ``queued`` with a
    ``next_run_at`` scheduled into the future. The history row must
    surface both branches and always carry the supplied ``error``.
    """
    from job_apply.features.apply_worker.repository import (
        InMemoryApplyStatusHistoryRepository,
    )

    history_repo: InMemoryApplyStatusHistoryRepository = (
        world.service.history_repo  # type: ignore[assignment]
    )

    # retryable=False: dead_letter
    job_dead = world.service.enqueue_for_match(world.match.id)
    world.service.claim_next()
    world.service.fail(job_dead.id, error="permanent", retryable=False)
    rows_dead = history_repo.list_by_job(job_dead.id)
    assert rows_dead[-1].from_status == ApplyJobStatus.RUNNING.value
    assert rows_dead[-1].to_status == ApplyJobStatus.DEAD_LETTER.value
    assert rows_dead[-1].error == "permanent"

    # retryable=True: back to queued
    second_match = VacancyMatch(
        id=uuid.uuid4(),
        search_profile_id=world.profile.id,
        vacancy_id=uuid.uuid4(),
        status=MatchStatus.ACCEPTED.value,
    )
    world.match_repo.add(second_match)
    job_retry = world.service.enqueue_for_match(second_match.id)
    world.service.claim_next()
    world.service.fail(job_retry.id, error="transient", retryable=True)
    rows_retry = history_repo.list_by_job(job_retry.id)
    assert rows_retry[-1].from_status == ApplyJobStatus.RUNNING.value
    assert rows_retry[-1].to_status == ApplyJobStatus.QUEUED.value
    assert rows_retry[-1].error == "transient"
    # The retry metadata captures the attempt number for the dashboard.
    assert rows_retry[-1].metadata_json is not None
    assert json.loads(rows_retry[-1].metadata_json)["retryable"] is True


def test_cancel_writes_history(world: _World) -> None:
    """``cancel`` writes a ``queued -> cancelled`` history row."""
    from job_apply.features.apply_worker.repository import (
        InMemoryApplyStatusHistoryRepository,
    )

    history_repo: InMemoryApplyStatusHistoryRepository = (
        world.service.history_repo  # type: ignore[assignment]
    )

    job = world.service.enqueue_for_match(world.match.id)
    world.service.cancel(job.id, user_id=world.user_id)
    rows = history_repo.list_by_job(job.id)

    assert [r.to_status for r in rows] == [
        ApplyJobStatus.QUEUED.value,
        ApplyJobStatus.CANCELLED.value,
    ]
    assert rows[1].from_status == ApplyJobStatus.QUEUED.value


# ---------------------------------------------------------------------------
# SQL repository — sqlite in-memory engine
# ---------------------------------------------------------------------------


@pytest.fixture
def engine() -> Iterator[Engine]:
    """Fresh in-memory sqlite engine per test with all tables created."""
    eng = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    from job_apply.features.apply_worker import models  # noqa: F401

    Base.metadata.create_all(bind=eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> Iterator[sessionmaker[Session]]:
    yield sessionmaker(bind=engine, class_=Session, autocommit=False, autoflush=False)


@pytest.fixture
def sql_history_repo(
    session_factory: sessionmaker[Session],
) -> SqlApplyStatusHistoryRepository:
    from job_apply.features.apply_worker.repository import (
        SqlApplyStatusHistoryRepository,
    )

    return SqlApplyStatusHistoryRepository(session_factory=session_factory)


def test_sql_create_and_list_round_trip(
    sql_history_repo: Any,
) -> None:
    """The SQL repo persists a row and reads it back."""
    from job_apply.features.apply_worker.models import ApplyStatusHistory

    job_id = uuid.uuid4()
    row = ApplyStatusHistory(
        job_id=job_id,
        from_status=ApplyJobStatus.QUEUED.value,
        to_status=ApplyJobStatus.RUNNING.value,
    )
    created = sql_history_repo.create(row)
    listed = sql_history_repo.list_by_job(job_id)

    assert created.id is not None
    assert created.created_at is not None
    assert len(listed) == 1
    assert listed[0].id == created.id
    assert listed[0].from_status == "queued"
    assert listed[0].to_status == "running"


def test_sql_list_by_job_filters_and_orders(
    sql_history_repo: Any,
) -> None:
    """``list_by_job`` only returns rows for the requested job, ordered by time."""
    from job_apply.features.apply_worker.models import ApplyStatusHistory

    job_id = uuid.uuid4()
    other_job_id = uuid.uuid4()
    a = sql_history_repo.create(
        ApplyStatusHistory(job_id=job_id, from_status=None, to_status=ApplyJobStatus.QUEUED.value)
    )
    b = sql_history_repo.create(
        ApplyStatusHistory(
            job_id=job_id,
            from_status=ApplyJobStatus.QUEUED.value,
            to_status=ApplyJobStatus.RUNNING.value,
        )
    )
    sql_history_repo.create(
        ApplyStatusHistory(
            job_id=other_job_id, from_status=None, to_status=ApplyJobStatus.QUEUED.value
        )
    )

    listed = sql_history_repo.list_by_job(job_id)

    assert [r.id for r in listed] == [a.id, b.id]


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@dataclass
class _ApiWorld:
    job_repo: InMemoryApplyJobRepository
    match_repo: _FakeMatchRepo
    profile_repo: _FakeProfileRepo
    user_id: uuid.UUID
    profile: SearchProfile
    match: VacancyMatch
    service: ApplyJobService


@pytest.fixture
def api_world() -> _ApiWorld:
    """In-memory fakes shared between the router and the API tests."""
    from job_apply.features.apply_worker.repository import (
        InMemoryApplyStatusHistoryRepository,
    )

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
        status=MatchStatus.ACCEPTED.value,
    )

    job_repo = InMemoryApplyJobRepository()
    match_repo = _FakeMatchRepo()
    match_repo.matches[match.id] = match
    profile_repo = _FakeProfileRepo()
    profile_repo.profiles[profile.id] = profile

    service = ApplyJobService(
        job_repo=job_repo,  # type: ignore[arg-type]
        match_repo=match_repo,  # type: ignore[arg-type]
        profile_repo=profile_repo,  # type: ignore[arg-type]
        history_repo=InMemoryApplyStatusHistoryRepository(),
    )

    return _ApiWorld(
        job_repo=job_repo,
        match_repo=match_repo,
        profile_repo=profile_repo,
        user_id=user_id,
        profile=profile,
        match=match,
        service=service,
    )


@pytest.fixture
def app(api_world: _ApiWorld) -> Iterator[FastAPI]:
    from job_apply.features.apply_worker.api import (
        get_apply_job_service,
    )
    from job_apply.features.apply_worker.api import (
        router as apply_worker_router,
    )

    application = FastAPI()
    application.include_router(apply_worker_router)
    application.dependency_overrides[get_apply_job_service] = lambda: api_world.service
    try:
        yield application
    finally:
        application.dependency_overrides.clear()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def token(api_world: _ApiWorld) -> str:
    return issue_token(str(api_world.user_id), ttl_seconds=3600)


def test_api_history_endpoint_returns_history(
    client: TestClient, token: str, api_world: _ApiWorld
) -> None:
    """``GET /apply-jobs/{id}/history`` returns the caller's history."""
    job = api_world.service.enqueue_for_match(api_world.match.id)
    api_world.service.claim_next()

    response = client.get(
        f"/apply-jobs/{job.id}/history",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.json()
    payload = response.json()
    assert [row["to_status"] for row in payload] == [
        ApplyJobStatus.QUEUED.value,
        ApplyJobStatus.RUNNING.value,
    ]
    assert payload[0]["from_status"] is None
    assert payload[1]["from_status"] == ApplyJobStatus.QUEUED.value


def test_api_history_endpoint_404_when_job_missing(client: TestClient, token: str) -> None:
    """A missing job returns 404 with the not_found code."""
    response = client.get(
        f"/apply-jobs/{uuid.uuid4()}/history",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "apply_job_not_found"


def test_api_history_endpoint_403_for_other_users_job(
    client: TestClient, token: str, api_world: _ApiWorld
) -> None:
    """A job owned by a different user returns 403."""
    from job_apply.features.apply_worker.models import (
        ApplyJob,
        compute_idempotency_key,
    )

    other_user_id = uuid.uuid4()
    other_match = VacancyMatch(
        id=uuid.uuid4(),
        search_profile_id=api_world.profile.id,
        vacancy_id=uuid.uuid4(),
        status=MatchStatus.ACCEPTED.value,
    )
    other = ApplyJob(
        match_id=other_match.id,
        user_id=other_user_id,
        vacancy_id=other_match.vacancy_id,
    )
    other.idempotency_key = compute_idempotency_key(
        other_user_id, other_match.vacancy_id, other_match.id
    )
    other_job = api_world.job_repo.create(other)

    response = client.get(
        f"/apply-jobs/{other_job.id}/history",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "forbidden"


def test_api_history_endpoint_401_without_token(client: TestClient) -> None:
    """A missing bearer token returns 401."""
    response = client.get(f"/apply-jobs/{uuid.uuid4()}/history")
    assert response.status_code == 401
