"""TDD tests for the learning-signals slice (M8, issue #63).

The learning slice captures structured "user rejected this match"
events so future prompt-tuning work (issue #29 / the M8 follow-ups)
can read them out by user, by prompt version, or by time window.

These tests pin down three contracts:

* :class:`LearningSignal` value object — frozen, all fields accessible.
* :class:`InMemoryLearningSignalRepository` — list-backed fake that
  satisfies the :class:`LearningSignalRepository` Protocol.
* :class:`SqlLearningSignalRepository` — SQLAlchemy-backed production
  implementation that round-trips every field through a real
  sqlite in-memory database.
* :class:`LearningSignalsService` — high-level facade that converts a
  ``record_rejection(...)`` call into a fully-formed
  :class:`LearningSignal` row.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from apply_pilot.db import Base
from apply_pilot.features.learning import models as _learning_models  # noqa: F401
from apply_pilot.features.learning.models import LearningSignalRow
from apply_pilot.features.learning.repository import (
    InMemoryLearningSignalRepository,
    LearningSignalRepository,
    SqlLearningSignalRepository,
)
from apply_pilot.features.learning.service import (
    LearningSignal,
    LearningSignalsService,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _signal(
    *,
    user_id: uuid.UUID | None = None,
    match_id: uuid.UUID | None = None,
    vacancy_id: uuid.UUID | None = None,
    search_profile_id: uuid.UUID | None = None,
    rejection_reason: str | None = "salary too low",
    prompt_version: str | None = "1.0.0",
    score: float | None = 42.0,
    signal_type: Literal["rejection", "dismissal", "low_score"] = "rejection",
    created_at: datetime | None = None,
) -> LearningSignal:
    """Build a fully-populated :class:`LearningSignal`."""
    return LearningSignal(
        id=uuid.uuid4(),
        user_id=user_id or uuid.uuid4(),
        match_id=match_id or uuid.uuid4(),
        vacancy_id=vacancy_id or uuid.uuid4(),
        search_profile_id=search_profile_id or uuid.uuid4(),
        rejection_reason=rejection_reason,
        prompt_version=prompt_version,
        score=score,
        signal_type=signal_type,
        created_at=created_at or datetime(2026, 6, 17, 12, 0, 0, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------
# LearningSignal value object
# ---------------------------------------------------------------------------


def test_learning_signal_is_frozen() -> None:
    """A :class:`LearningSignal` is immutable — ``frozen=True``."""
    signal = _signal()

    with pytest.raises((AttributeError, Exception)):  # FrozenInstanceError
        signal.score = 0.0  # type: ignore[misc]


def test_learning_signal_carries_all_fields() -> None:
    """All nine public fields are accessible on the dataclass."""
    user_id = uuid.uuid4()
    match_id = uuid.uuid4()
    vacancy_id = uuid.uuid4()
    search_profile_id = uuid.uuid4()
    when = datetime(2026, 6, 17, 12, 0, 0, tzinfo=UTC)
    signal = LearningSignal(
        id=uuid.uuid4(),
        user_id=user_id,
        match_id=match_id,
        vacancy_id=vacancy_id,
        search_profile_id=search_profile_id,
        rejection_reason="not a fit",
        prompt_version="1.2.0",
        score=87.5,
        signal_type="rejection",
        created_at=when,
    )

    assert signal.user_id == user_id
    assert signal.match_id == match_id
    assert signal.vacancy_id == vacancy_id
    assert signal.search_profile_id == search_profile_id
    assert signal.rejection_reason == "not a fit"
    assert signal.prompt_version == "1.2.0"
    assert signal.score == 87.5
    assert signal.signal_type == "rejection"
    assert signal.created_at == when


# ---------------------------------------------------------------------------
# In-memory repository
# ---------------------------------------------------------------------------


@pytest.fixture
def in_memory_repo() -> InMemoryLearningSignalRepository:
    return InMemoryLearningSignalRepository()


def test_in_memory_satisfies_protocol(in_memory_repo: InMemoryLearningSignalRepository) -> None:
    """The in-memory fake must satisfy the :class:`LearningSignalRepository` Protocol."""
    assert isinstance(in_memory_repo, LearningSignalRepository)


def test_in_memory_record_returns_signal_with_id(
    in_memory_repo: InMemoryLearningSignalRepository,
) -> None:
    """``record`` must persist the signal and return the same instance."""
    signal = _signal()

    returned = in_memory_repo.record(signal)

    assert returned is signal
    assert in_memory_repo.list_for_user(signal.user_id) == [signal]


def test_in_memory_list_for_user_filters_by_user(
    in_memory_repo: InMemoryLearningSignalRepository,
) -> None:
    """``list_for_user`` returns only signals belonging to the user."""
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    a = _signal(user_id=user_a)
    b = _signal(user_id=user_b)

    in_memory_repo.record(a)
    in_memory_repo.record(b)

    assert in_memory_repo.list_for_user(user_a) == [a]
    assert in_memory_repo.list_for_user(user_b) == [b]


def test_in_memory_list_for_user_respects_limit(
    in_memory_repo: InMemoryLearningSignalRepository,
) -> None:
    """``list_for_user`` caps the result to ``limit`` items, newest first."""
    user = uuid.uuid4()
    base = datetime(2026, 6, 17, 12, 0, 0, tzinfo=UTC)
    older = _signal(user_id=user, created_at=base)
    middle = _signal(user_id=user, created_at=base + timedelta(minutes=1))
    newest = _signal(user_id=user, created_at=base + timedelta(minutes=2))
    in_memory_repo.record(older)
    in_memory_repo.record(middle)
    in_memory_repo.record(newest)

    result = in_memory_repo.list_for_user(user, limit=2)

    assert result == [newest, middle]


def test_in_memory_list_for_user_returns_empty_for_unknown_user(
    in_memory_repo: InMemoryLearningSignalRepository,
) -> None:
    """An unknown user gets an empty list, not None."""
    assert in_memory_repo.list_for_user(uuid.uuid4()) == []


def test_in_memory_list_for_prompt_filters_by_version_and_since(
    in_memory_repo: InMemoryLearningSignalRepository,
) -> None:
    """``list_for_prompt`` must filter by prompt_version and since timestamp."""
    base = datetime(2026, 6, 17, 12, 0, 0, tzinfo=UTC)
    p1_old = _signal(prompt_version="1.0.0", created_at=base - timedelta(days=1))
    p1_new = _signal(prompt_version="1.0.0", created_at=base)
    p2_new = _signal(prompt_version="2.0.0", created_at=base)
    in_memory_repo.record(p1_old)
    in_memory_repo.record(p1_new)
    in_memory_repo.record(p2_new)

    result = in_memory_repo.list_for_prompt("1.0.0", since=base - timedelta(minutes=1))

    assert result == [p1_new]


def test_in_memory_list_for_prompt_returns_empty_when_none_match(
    in_memory_repo: InMemoryLearningSignalRepository,
) -> None:
    """No matching prompt version means an empty result list."""
    in_memory_repo.record(_signal(prompt_version="1.0.0"))
    assert in_memory_repo.list_for_prompt("9.9.9", since=datetime.now(UTC)) == []


# ---------------------------------------------------------------------------
# SQL repository
# ---------------------------------------------------------------------------


@pytest.fixture
def engine() -> Iterator[Engine]:
    """Fresh in-memory sqlite engine per test with the learning_signals table."""
    eng = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=eng, tables=[LearningSignalRow.__table__])
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> Iterator[sessionmaker[Session]]:
    factory = sessionmaker(bind=engine, class_=Session, autocommit=False, autoflush=False)
    yield factory


@pytest.fixture
def sql_repo(session_factory: sessionmaker[Session]) -> SqlLearningSignalRepository:
    return SqlLearningSignalRepository(session_factory=session_factory)


def test_sql_record_persists_row(sql_repo: SqlLearningSignalRepository) -> None:
    """A recorded signal must round-trip through the SQL repo."""
    signal = _signal(rejection_reason="bad fit", score=10.0, prompt_version="1.0.0")

    sql_repo.record(signal)

    fetched = sql_repo.list_for_user(signal.user_id, limit=10)
    assert len(fetched) == 1
    assert fetched[0].rejection_reason == "bad fit"
    assert fetched[0].score == 10.0
    assert fetched[0].prompt_version == "1.0.0"


def test_sql_list_for_prompt_filters_by_version_and_since(
    sql_repo: SqlLearningSignalRepository,
) -> None:
    """The SQL repo must respect the prompt_version and ``since`` filters."""
    base = datetime(2026, 6, 17, 12, 0, 0, tzinfo=UTC)
    sql_repo.record(_signal(prompt_version="1.0.0", created_at=base - timedelta(days=1)))
    sql_repo.record(_signal(prompt_version="1.0.0", created_at=base))
    sql_repo.record(_signal(prompt_version="2.0.0", created_at=base))

    result = sql_repo.list_for_prompt("1.0.0", since=base - timedelta(minutes=1))

    assert len(result) == 1
    assert result[0].prompt_version == "1.0.0"


# ---------------------------------------------------------------------------
# LearningSignalsService
# ---------------------------------------------------------------------------


def test_service_record_rejection_persists_a_rejection_signal(
    in_memory_repo: InMemoryLearningSignalRepository,
) -> None:
    """``record_rejection`` must produce a ``rejection`` signal with all fields."""
    service = LearningSignalsService(repo=in_memory_repo)
    user_id = uuid.uuid4()
    match_id = uuid.uuid4()
    vacancy_id = uuid.uuid4()
    profile_id = uuid.uuid4()

    returned = service.record_rejection(
        user_id=user_id,
        match_id=match_id,
        vacancy_id=vacancy_id,
        search_profile_id=profile_id,
        reason="not a fit",
        score=42.0,
        prompt_version="1.0.0",
    )

    assert returned.signal_type == "rejection"
    assert returned.user_id == user_id
    assert returned.match_id == match_id
    assert returned.vacancy_id == vacancy_id
    assert returned.search_profile_id == profile_id
    assert returned.rejection_reason == "not a fit"
    assert returned.score == 42.0
    assert returned.prompt_version == "1.0.0"
    # The signal must actually be persisted.
    signals = in_memory_repo.list_for_user(user_id)
    assert len(signals) == 1
    assert signals[0] is returned


def test_service_record_rejection_with_none_score_and_prompt(
    in_memory_repo: InMemoryLearningSignalRepository,
) -> None:
    """Score and prompt_version are optional — None must round-trip cleanly."""
    service = LearningSignalsService(repo=in_memory_repo)
    user_id = uuid.uuid4()
    match_id = uuid.uuid4()

    returned = service.record_rejection(
        user_id=user_id,
        match_id=match_id,
        vacancy_id=uuid.uuid4(),
        search_profile_id=uuid.uuid4(),
        reason="just no",
        score=None,
        prompt_version=None,
    )

    assert returned.score is None
    assert returned.prompt_version is None
    assert returned.rejection_reason == "just no"


def test_service_record_rejection_with_none_reason(
    in_memory_repo: InMemoryLearningSignalRepository,
) -> None:
    """A rejection without an explicit reason must still persist with reason=None."""
    service = LearningSignalsService(repo=in_memory_repo)
    user_id = uuid.uuid4()

    returned = service.record_rejection(
        user_id=user_id,
        match_id=uuid.uuid4(),
        vacancy_id=uuid.uuid4(),
        search_profile_id=uuid.uuid4(),
        reason=None,
        score=None,
        prompt_version=None,
    )

    assert returned.rejection_reason is None
    assert in_memory_repo.list_for_user(user_id) == [returned]


def test_service_list_for_user_delegates_to_repo(
    in_memory_repo: InMemoryLearningSignalRepository,
) -> None:
    """``list_for_user`` must be a thin pass-through to the repository."""
    service = LearningSignalsService(repo=in_memory_repo)
    user_id = uuid.uuid4()
    signal = service.record_rejection(
        user_id=user_id,
        match_id=uuid.uuid4(),
        vacancy_id=uuid.uuid4(),
        search_profile_id=uuid.uuid4(),
        reason="r",
        score=None,
        prompt_version=None,
    )

    assert service.list_for_user(user_id) == [signal]
