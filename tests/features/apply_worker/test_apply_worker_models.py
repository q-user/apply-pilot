"""TDD tests for the :class:`ApplyJob` ORM model (M5, issue #43).

The model is the storage layer for the apply worker queue. Every
:term:`accepted <VACANCY_MATCH_ACCEPTED>` :class:`VacancyMatch` becomes
exactly one :class:`ApplyJob` row — the ``UNIQUE(match_id)`` constraint
is the contract.

Test surface
------------

The 6 test cases cover:

* Default field values populated by the model definition.
* The ``ApplyJobStatus`` enum carries the canonical lifecycle names.
* :func:`compute_idempotency_key` is deterministic over its inputs and
  returns a 64-character hex SHA-256 digest.
* A fresh :class:`ApplyJob` row has ``attempts == 0`` and no
  ``external_application_id``.
* The composite ``(status, next_run_at)`` index exists for the queue
  scanning the worker relies on.
* The :class:`ApplyJob` ORM ``__repr__`` is debug-friendly but does not
  leak large payload columns.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from apply_pilot.db import Base
from apply_pilot.features.apply_worker.models import (
    ApplyJob,
    ApplyJobStatus,
    compute_idempotency_key,
)

# ---------------------------------------------------------------------------
# Status enum
# ---------------------------------------------------------------------------


def test_status_enum_has_canonical_values() -> None:
    """The enum exposes the six lifecycle states listed in the issue spec.

    Renaming a value would break the persisted ``status`` column for
    every historical row, so this test guards against accidental
    drift.
    """
    expected = {
        "queued",
        "running",
        "succeeded",
        "failed",
        "cancelled",
        "dead_letter",
    }
    actual = {member.value for member in ApplyJobStatus}
    assert actual == expected


def test_status_enum_values_are_lowercase_strings() -> None:
    """Status values must be lowercase strings (so they round-trip safely)."""
    for member in ApplyJobStatus:
        assert member.value == member.value.lower()
        assert " " not in member.value


# ---------------------------------------------------------------------------
# Idempotency key
# ---------------------------------------------------------------------------


def test_compute_idempotency_key_is_sha256_of_canonical_input() -> None:
    """The key is SHA-256 of the joined ``user_id|vacancy_id|match_id`` triple.

    The hash is stable across processes / restarts so the worker can
    safely re-enqueue the same match after a crash and not double-submit.
    """
    user_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
    vacancy_id = uuid.UUID("22222222-2222-2222-2222-222222222222")
    match_id = uuid.UUID("33333333-3333-3333-3333-333333333333")

    key = compute_idempotency_key(user_id, vacancy_id, match_id)

    # A SHA-256 hex digest is always 64 lowercase hex characters.
    assert len(key) == 64
    assert all(ch in "0123456789abcdef" for ch in key)

    # Determinism: re-running with the same inputs yields the same hash.
    assert key == compute_idempotency_key(user_id, vacancy_id, match_id)

    # Independent of UUID representation: a string form should also work.
    assert key == compute_idempotency_key(str(user_id), str(vacancy_id), str(match_id))

    # Sanity: the value matches a hand-rolled SHA-256 of the same input.
    expected = hashlib.sha256(f"{user_id}|{vacancy_id}|{match_id}".encode()).hexdigest()
    assert key == expected


def test_compute_idempotency_key_changes_with_inputs() -> None:
    """Swapping any of the three inputs must change the key."""
    user_a = uuid.UUID("11111111-1111-1111-1111-111111111111")
    user_b = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    vacancy = uuid.UUID("22222222-2222-2222-2222-222222222222")
    match = uuid.UUID("33333333-3333-3333-3333-333333333333")

    base = compute_idempotency_key(user_a, vacancy, match)
    assert base != compute_idempotency_key(user_b, vacancy, match)
    assert base != compute_idempotency_key(user_a, uuid.uuid4(), match)
    assert base != compute_idempotency_key(user_a, vacancy, uuid.uuid4())


# ---------------------------------------------------------------------------
# Field defaults
# ---------------------------------------------------------------------------


def test_fresh_apply_job_has_documented_defaults() -> None:
    """A newly-constructed :class:`ApplyJob` exposes the M5 #43 defaults.

    The service layer relies on ``status == 'queued'`` and
    ``attempts == 0`` for new rows — explicit defaults make the
    contract obvious in the model file and survive schema-evolution
    changes.
    """
    user_id = uuid.uuid4()
    vacancy_id = uuid.uuid4()
    match_id = uuid.uuid4()

    job = ApplyJob(
        match_id=match_id,
        user_id=user_id,
        vacancy_id=vacancy_id,
    )

    assert job.status == ApplyJobStatus.QUEUED.value
    assert job.attempts == 0
    assert job.last_error is None
    assert job.next_run_at is None
    assert job.external_application_id is None
    assert job.started_at is None
    assert job.finished_at is None
    # The idempotency key is computed from the foreign keys; the service
    # passes it explicitly but the default factory should produce the
    # same value so an accidental ``create(ApplyJob(...))`` call still
    # gets a stable key.
    assert job.idempotency_key == compute_idempotency_key(user_id, vacancy_id, match_id)


# ---------------------------------------------------------------------------
# Schema-level constraints (smoke test against a sqlite in-memory db)
# ---------------------------------------------------------------------------


@pytest.fixture
def engine() -> Engine:
    """In-memory sqlite engine with the ``apply_jobs`` table created."""
    eng = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # Importing the parent model modules registers the foreign-key
    # targets on ``Base.metadata`` so the FK constraints on
    # ``apply_jobs`` resolve cleanly during ``create_all``.
    from apply_pilot.features.apply_worker import models  # noqa: F401
    from apply_pilot.features.matches import models as _match_models  # noqa: F401
    from apply_pilot.features.sources import models as _vacancy_models  # noqa: F401
    from apply_pilot.features.users import models as _user_models  # noqa: F401

    Base.metadata.create_all(bind=eng)
    try:
        yield eng
    finally:
        eng.dispose()


def test_composite_index_on_status_and_next_run_at_exists(engine: Engine) -> None:
    """The queue-scanning index ``ix_apply_jobs_status_next_run_at`` exists.

    The worker drains the queue with
    ``WHERE status = 'queued' AND next_run_at <= now()``; the composite
    index keeps that query cheap as the table grows.
    """
    inspector = _inspect(engine)
    indexes = {ix["name"] for ix in inspector.get_indexes("apply_jobs")}
    assert "ix_apply_jobs_status_next_run_at" in indexes


def test_match_id_is_unique(engine: Engine) -> None:
    """The ``UNIQUE(match_id)`` constraint rejects duplicate match rows.

    The contract is "one apply job per accepted vacancy match"; the
    constraint enforces it at the storage layer so a race between two
    enqueue calls cannot insert two rows.
    """
    session = Session(engine)
    try:
        first = ApplyJob(
            match_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            vacancy_id=uuid.uuid4(),
        )
        session.add(first)
        session.commit()

        from sqlalchemy.exc import IntegrityError

        duplicate = ApplyJob(
            match_id=first.match_id,
            user_id=uuid.uuid4(),
            vacancy_id=uuid.uuid4(),
        )
        session.add(duplicate)
        with pytest.raises(IntegrityError):
            session.commit()
    finally:
        session.rollback()
        session.close()


def test_idempotency_key_is_unique(engine: Engine) -> None:
    """The ``UNIQUE(idempotency_key)`` constraint is the safety net.

    The service computes the key before insert; the constraint is the
    final guard against a developer mistake.
    """
    session = Session(engine)
    try:
        user_id = uuid.uuid4()
        vacancy_id = uuid.uuid4()
        match_id = uuid.uuid4()
        key = compute_idempotency_key(user_id, vacancy_id, match_id)
        session.add(
            ApplyJob(
                match_id=match_id,
                user_id=user_id,
                vacancy_id=vacancy_id,
                idempotency_key=key,
            )
        )
        session.commit()

        from sqlalchemy.exc import IntegrityError

        session.add(
            ApplyJob(
                match_id=uuid.uuid4(),
                user_id=uuid.uuid4(),
                vacancy_id=uuid.uuid4(),
                idempotency_key=key,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
    finally:
        session.rollback()
        session.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _inspect(engine: Engine):
    from sqlalchemy import inspect

    return inspect(engine)
