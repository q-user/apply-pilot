"""TDD tests for the ``apply_worker`` repository (M5, issue #43).

The :class:`ApplyJobRepository` is the slice's only persistence gateway.
The service layer depends on its Protocol, and the worker drains the
queue through :meth:`claim_next`. This module tests both the in-memory
and the SQLAlchemy-backed implementations against the same scenarios so
a divergence in semantics between them is caught early.

Test surface
------------

The 12 test cases cover:

* ``create`` populates defaults (status, attempts, idempotency_key) and
  assigns an id.
* ``get_by_id`` and ``get_by_match`` resolve rows.
* ``list_by_user`` returns the user's jobs in ``created_at`` order.
* ``list_pending`` filters to ``status=queued`` with
  ``next_run_at <= now()``.
* ``claim_next`` atomically picks the next claimable row, transitions
  it to ``running``, increments ``attempts``, and stamps ``started_at``.
* ``update_status`` mutates the status (and ``external_application_id``
  when supplied).
* ``mark_attempt`` increments ``attempts`` and records ``last_error``.
* The ``UNIQUE(match_id)`` constraint is enforced at the storage layer
  on top of the service-level idempotency check.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from apply_pilot.db import Base
from apply_pilot.features.apply_worker.models import (
    ApplyJob,
    ApplyJobStatus,
    compute_idempotency_key,
)
from apply_pilot.features.apply_worker.repository import (
    ApplyJobRepository,
    InMemoryApplyJobRepository,
    SqlApplyJobRepository,
)
from apply_pilot.features.matches import models as _match_models  # noqa: F401
from apply_pilot.features.sources import models as _vacancy_models  # noqa: F401
from apply_pilot.features.users import models as _user_models  # noqa: F401


def _make_job(**overrides: Any) -> ApplyJob:
    """Build a fresh :class:`ApplyJob` with sensible defaults."""
    payload: dict[str, Any] = {
        "match_id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "vacancy_id": uuid.uuid4(),
    }
    payload.update(overrides)
    return ApplyJob(**payload)


# ---------------------------------------------------------------------------
# In-memory repository
# ---------------------------------------------------------------------------


def test_in_memory_create_assigns_defaults() -> None:
    """``create`` materialises id / status / attempts / idempotency_key.

    The :class:`ApplyJob` model sets Python-level defaults in
    ``__init__``; the repository's ``create`` mirrors what the SQL
    flush would produce for the columns the model does not default.
    """
    repo: ApplyJobRepository = InMemoryApplyJobRepository()
    job = _make_job()

    created = repo.create(job)

    assert created.id is not None
    assert created.status == ApplyJobStatus.QUEUED.value
    assert created.attempts == 0
    assert created.idempotency_key == compute_idempotency_key(
        created.user_id, created.vacancy_id, created.match_id
    )
    # The repo must hold the row and the secondary indices.
    assert repo.get_by_id(created.id) is created
    assert repo.get_by_match(created.match_id) is created


def test_in_memory_get_by_id_and_get_by_match() -> None:
    """Both lookups resolve the same row."""
    repo: ApplyJobRepository = InMemoryApplyJobRepository()
    job = repo.create(_make_job())

    by_id = repo.get_by_id(job.id)
    by_match = repo.get_by_match(job.match_id)

    assert by_id is job
    assert by_match is job
    # Missing lookups return ``None`` (not raise) so the service can
    # distinguish "not found" from "real error".
    assert repo.get_by_id(uuid.uuid4()) is None
    assert repo.get_by_match(uuid.uuid4()) is None


def test_in_memory_list_by_user_orders_newest_first() -> None:
    """``list_by_user`` returns the caller's jobs, newest first."""
    repo: ApplyJobRepository = InMemoryApplyJobRepository()
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    a1 = repo.create(_make_job(user_id=user_a))
    a2 = repo.create(_make_job(user_id=user_a))
    repo.create(_make_job(user_id=user_b))

    listed = list(repo.list_by_user(user_a))

    assert [j.id for j in listed] == [a2.id, a1.id]


def test_in_memory_list_pending_filters_by_status_and_next_run_at() -> None:
    """``list_pending`` only surfaces claimable rows.

    A row is claimable when its status is ``queued`` AND its
    ``next_run_at`` is either ``None`` or in the past. Rows that are
    already running, succeeded, or scheduled for the future are
    excluded.
    """
    repo: ApplyJobRepository = InMemoryApplyJobRepository()
    now = datetime.now(UTC)

    ready_now = repo.create(_make_job())
    ready_past = repo.create(_make_job(next_run_at=now - timedelta(minutes=5)))
    scheduled_future = repo.create(_make_job(next_run_at=now + timedelta(minutes=5)))
    running = repo.create(_make_job())
    running.status = ApplyJobStatus.RUNNING.value
    succeeded = repo.create(_make_job())
    succeeded.status = ApplyJobStatus.SUCCEEDED.value

    pending_ids = {j.id for j in repo.list_pending()}

    assert pending_ids == {ready_now.id, ready_past.id}
    assert scheduled_future.id not in pending_ids
    assert running.id not in pending_ids
    assert succeeded.id not in pending_ids


def test_in_memory_claim_next_transitions_to_running() -> None:
    """``claim_next`` returns the oldest claimable row, marks it running.

    The method must:

    * return the job with the oldest ``created_at`` (FIFO) so workers
      that race do not starve early enqueues;
    * atomically transition ``status`` to ``running``;
    * increment ``attempts``;
    * stamp ``started_at`` with the current time.
    """
    repo: ApplyJobRepository = InMemoryApplyJobRepository()
    first = repo.create(_make_job())
    second = repo.create(_make_job())
    third = repo.create(_make_job())

    claimed = repo.claim_next()

    assert claimed is not None
    assert claimed.id == first.id
    assert claimed.status == ApplyJobStatus.RUNNING.value
    assert claimed.attempts == 1
    assert claimed.started_at is not None
    # The remaining rows stay claimable.
    assert {j.id for j in repo.list_pending()} == {second.id, third.id}


def test_in_memory_claim_next_returns_none_when_empty() -> None:
    """``claim_next`` returns ``None`` when there is nothing to claim."""
    repo: ApplyJobRepository = InMemoryApplyJobRepository()
    assert repo.claim_next() is None


def test_in_memory_update_status_mutates_fields() -> None:
    """``update_status`` changes the status (and optional external id)."""
    repo: ApplyJobRepository = InMemoryApplyJobRepository()
    job = repo.create(_make_job())

    updated = repo.update_status(
        job.id,
        ApplyJobStatus.SUCCEEDED.value,
        external_application_id="hh-12345",
    )

    assert updated.status == ApplyJobStatus.SUCCEEDED.value
    assert updated.external_application_id == "hh-12345"
    assert repo.get_by_id(job.id).external_application_id == "hh-12345"  # type: ignore[union-attr]


def test_in_memory_mark_attempt_increments_and_records_error() -> None:
    """``mark_attempt`` bumps ``attempts`` and stores the error message."""
    repo: ApplyJobRepository = InMemoryApplyJobRepository()
    job = repo.create(_make_job())
    assert job.attempts == 0

    after_first = repo.mark_attempt(job.id, "network timeout")
    assert after_first.attempts == 1
    assert after_first.last_error == "network timeout"

    after_second = repo.mark_attempt(job.id, "another failure")
    assert after_second.attempts == 2
    assert after_second.last_error == "another failure"


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
    from apply_pilot.features.apply_worker import models  # noqa: F401

    Base.metadata.create_all(bind=eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> Iterator[sessionmaker[Session]]:
    yield sessionmaker(bind=engine, class_=Session, autocommit=False, autoflush=False)


@pytest.fixture
def sql_repo(
    session_factory: sessionmaker[Session],
) -> SqlApplyJobRepository:
    return SqlApplyJobRepository(session_factory=session_factory)


def test_sql_create_and_lookup_round_trip(sql_repo: SqlApplyJobRepository) -> None:
    """The SQL repo honours the Protocol on a sqlite in-memory engine."""
    user_id = uuid.uuid4()
    match_id = uuid.uuid4()
    vacancy_id = uuid.uuid4()

    created = sql_repo.create(_make_job(user_id=user_id, match_id=match_id, vacancy_id=vacancy_id))
    assert created.id is not None
    assert created.status == ApplyJobStatus.QUEUED.value
    assert created.attempts == 0

    by_id = sql_repo.get_by_id(created.id)
    by_match = sql_repo.get_by_match(match_id)
    assert by_id is not None and by_id.id == created.id
    assert by_match is not None and by_match.id == created.id


def test_sql_list_by_user_filters_by_owner(
    sql_repo: SqlApplyJobRepository,
) -> None:
    """``list_by_user`` only returns rows owned by the caller."""
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    a1 = sql_repo.create(_make_job(user_id=user_a))
    a2 = sql_repo.create(_make_job(user_id=user_a))
    sql_repo.create(_make_job(user_id=user_b))

    listed = list(sql_repo.list_by_user(user_a))

    assert {j.id for j in listed} == {a1.id, a2.id}


def test_sql_list_pending_uses_status_and_next_run_at(
    sql_repo: SqlApplyJobRepository,
    session_factory: sessionmaker[Session],
) -> None:
    """``list_pending`` pushes the filter down as a SQL query."""
    now = datetime.now(UTC)
    ready_now = sql_repo.create(_make_job())
    ready_past = sql_repo.create(_make_job(next_run_at=now - timedelta(minutes=5)))
    scheduled_future = sql_repo.create(_make_job(next_run_at=now + timedelta(minutes=5)))
    # A running row that was inserted in the same repository must not
    # appear in the pending scan. Mutate it through a fresh session so
    # we exercise the same path a real worker would use. The instance
    # is detached from the original (closed) session, so we ``merge``
    # it into the new session.
    running = sql_repo.create(_make_job())
    running.status = ApplyJobStatus.RUNNING.value
    session = session_factory()
    try:
        session.merge(running)
        session.commit()
    finally:
        session.close()

    pending_ids = {j.id for j in sql_repo.list_pending()}

    assert pending_ids == {ready_now.id, ready_past.id}
    assert scheduled_future.id not in pending_ids
    assert running.id not in pending_ids


def test_sql_claim_next_transitions_and_increments(
    sql_repo: SqlApplyJobRepository,
) -> None:
    """``claim_next`` performs an atomic state transition on the SQL path.

    The transition (queued → running) and the ``attempts`` increment
    must be visible to subsequent reads in a different session; the
    implementation commits before returning. The exact row picked is
    not asserted (sqlite timestamps can tie), only that one row was
    claimed and the other remains in the queue.
    """
    first = sql_repo.create(_make_job())
    second = sql_repo.create(_make_job())

    claimed = sql_repo.claim_next()
    assert claimed is not None
    assert claimed.id in {first.id, second.id}
    assert claimed.status == ApplyJobStatus.RUNNING.value
    assert claimed.attempts == 1
    assert claimed.started_at is not None
    # The un-claimed row stays in the queue.
    remaining = list(sql_repo.list_pending())
    assert len(remaining) == 1
    assert remaining[0].id != claimed.id


def test_sql_update_status_and_mark_attempt(
    sql_repo: SqlApplyJobRepository,
) -> None:
    """``update_status`` and ``mark_attempt`` commit their changes."""
    job = sql_repo.create(_make_job())

    updated = sql_repo.update_status(
        job.id,
        ApplyJobStatus.SUCCEEDED.value,
        external_application_id="hh-sql-1",
    )
    assert updated.status == ApplyJobStatus.SUCCEEDED.value
    assert updated.external_application_id == "hh-sql-1"

    retried = sql_repo.mark_attempt(job.id, "transient failure")
    assert retried.attempts == 1
    assert retried.last_error == "transient failure"


def test_sql_match_id_unique_constraint_enforced(
    sql_repo: SqlApplyJobRepository,
) -> None:
    """The ``UNIQUE(match_id)`` constraint rejects a second row for a match.

    The service-level idempotency in :meth:`ApplyJobService.enqueue_for_match`
    is the first line of defence; the constraint is the safety net
    for racing requests that both pass the lookup at the same time.
    """
    user_id = uuid.uuid4()
    match_id = uuid.uuid4()
    vacancy_id = uuid.uuid4()

    sql_repo.create(_make_job(user_id=user_id, match_id=match_id, vacancy_id=vacancy_id))
    with pytest.raises(IntegrityError):
        sql_repo.create(_make_job(user_id=user_id, match_id=match_id, vacancy_id=vacancy_id))


def test_sql_idempotency_key_unique_constraint_enforced(
    sql_repo: SqlApplyJobRepository,
) -> None:
    """The ``UNIQUE(idempotency_key)`` constraint is the final guard."""
    user_id = uuid.uuid4()
    match_id = uuid.uuid4()
    vacancy_id = uuid.uuid4()
    key = compute_idempotency_key(user_id, vacancy_id, match_id)

    sql_repo.create(
        _make_job(user_id=user_id, match_id=match_id, vacancy_id=vacancy_id, idempotency_key=key)
    )
    with pytest.raises(IntegrityError):
        sql_repo.create(
            _make_job(
                user_id=uuid.uuid4(),
                match_id=uuid.uuid4(),
                vacancy_id=uuid.uuid4(),
                idempotency_key=key,
            )
        )
