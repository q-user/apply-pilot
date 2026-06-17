"""TDD tests for the ``StyleMemoryRepository`` persistence gateway.

Two implementations are exercised, mirroring the convention used by the
``cover_letter_style`` slice:

* :class:`InMemoryStyleMemoryRepository` — list-backed fake.
* :class:`SqlStyleMemoryRepository` — SQLAlchemy-backed production
  implementation against a sqlite in-memory engine.

Both implementations must satisfy the same Protocol contract: record an
entry on accept, list the most recent entries for a user (limit-bounded),
and return the aggregated summary (a string) on demand.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from apply_pilot.db import Base
from apply_pilot.features.users import models as _users_models  # noqa: F401
from apply_pilot.features.writing_style_memory.models import StyleMemoryEntryModel
from apply_pilot.features.writing_style_memory.repository import (
    InMemoryStyleMemoryRepository,
    SqlStyleMemoryRepository,
)

# ---------------------------------------------------------------------------
# In-memory repository
# ---------------------------------------------------------------------------


@pytest.fixture
def in_memory_repo() -> InMemoryStyleMemoryRepository:
    return InMemoryStyleMemoryRepository()


def test_in_memory_record_persists_entry(
    in_memory_repo: InMemoryStyleMemoryRepository,
) -> None:
    """``record`` must persist the entry and return it with id and timestamp."""
    user_id = uuid.uuid4()
    cover_letter_id = uuid.uuid4()

    entry = in_memory_repo.record(
        user_id=user_id,
        cover_letter_id=cover_letter_id,
        letter_text="Hello there! I would love to join your team.",
        style_summary=(
            "first-sentence: Hello there!; words=9; trigrams=love-join-your, your-team-warm"
        ),
    )

    assert entry.id is not None
    assert entry.user_id == user_id
    assert entry.cover_letter_id == cover_letter_id
    assert entry.letter_text == "Hello there! I would love to join your team."
    assert entry.style_summary.startswith("first-sentence: Hello there!")
    assert entry.created_at is not None


def test_in_memory_list_for_user_returns_recent_first(
    in_memory_repo: InMemoryStyleMemoryRepository,
) -> None:
    """``list_for_user`` must return entries newest-first and respect ``limit``.

    The fixture inserts a tiny sleep between the two records so the
    ``created_at`` timestamps differ by at least a millisecond — the
    repository breaks ties on id (UUID lex order) but the test wants to
    assert the timestamp-based ordering is correct.
    """
    import time

    user_id = uuid.uuid4()
    other_user = uuid.uuid4()

    # Two entries for the target user, one for somebody else.
    e1 = in_memory_repo.record(
        user_id=user_id,
        cover_letter_id=uuid.uuid4(),
        letter_text="First letter",
        style_summary="first-sentence: First; words=2",
    )
    time.sleep(0.005)
    e2 = in_memory_repo.record(
        user_id=user_id,
        cover_letter_id=uuid.uuid4(),
        letter_text="Second letter",
        style_summary="first-sentence: Second; words=2",
    )
    in_memory_repo.record(
        user_id=other_user,
        cover_letter_id=uuid.uuid4(),
        letter_text="Someone else",
        style_summary="first-sentence: Someone; words=2",
    )

    listed = in_memory_repo.list_for_user(user_id)

    assert [e.id for e in listed] == [e2.id, e1.id]
    assert all(e.user_id == user_id for e in listed)


def test_in_memory_list_for_user_respects_limit(
    in_memory_repo: InMemoryStyleMemoryRepository,
) -> None:
    """``list_for_user(limit=N)`` must return at most N entries."""
    user_id = uuid.uuid4()
    for _ in range(5):
        in_memory_repo.record(
            user_id=user_id,
            cover_letter_id=uuid.uuid4(),
            letter_text="x",
            style_summary="first-sentence: x; words=1",
        )

    listed = in_memory_repo.list_for_user(user_id, limit=3)
    assert len(listed) == 3


def test_in_memory_list_for_user_returns_empty_for_unknown_user(
    in_memory_repo: InMemoryStyleMemoryRepository,
) -> None:
    """``list_for_user`` must return an empty list for a user with no entries."""
    assert in_memory_repo.list_for_user(uuid.uuid4()) == []


def test_in_memory_get_aggregated_returns_none_when_empty(
    in_memory_repo: InMemoryStyleMemoryRepository,
) -> None:
    """``get_aggregated`` must return ``None`` when no entries exist for the user."""
    assert in_memory_repo.get_aggregated(uuid.uuid4()) is None


def test_in_memory_get_aggregated_concatenates_recent_summaries(
    in_memory_repo: InMemoryStyleMemoryRepository,
) -> None:
    """``get_aggregated`` must join the most recent entries' summaries."""
    user_id = uuid.uuid4()
    in_memory_repo.record(
        user_id=user_id,
        cover_letter_id=uuid.uuid4(),
        letter_text="older",
        style_summary="SUMMARY-A",
    )
    in_memory_repo.record(
        user_id=user_id,
        cover_letter_id=uuid.uuid4(),
        letter_text="newer",
        style_summary="SUMMARY-B",
    )

    aggregated = in_memory_repo.get_aggregated(user_id)
    assert aggregated is not None
    # Both summaries must be present, newest first.
    assert aggregated.index("SUMMARY-B") < aggregated.index("SUMMARY-A")


# ---------------------------------------------------------------------------
# SQL repository
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
    # Import the model modules so SQLAlchemy knows about the tables.
    # ``cover_letter_drafts`` is the FK target for
    # ``style_memory_entries.cover_letter_id``; without it the FK cannot
    # be resolved at table-creation time.
    from apply_pilot.features.cover_letter import models as _cover_letter_models  # noqa: F401
    from apply_pilot.features.writing_style_memory import models  # noqa: F401

    Base.metadata.create_all(bind=eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> Iterator[sessionmaker[Session]]:
    factory = sessionmaker(bind=engine, class_=Session, autocommit=False, autoflush=False)
    yield factory


@pytest.fixture
def sql_repo(
    session_factory: sessionmaker[Session],
) -> SqlStyleMemoryRepository:
    return SqlStyleMemoryRepository(session_factory=session_factory)


def test_sql_record_and_list_round_trip(
    sql_repo: SqlStyleMemoryRepository,
) -> None:
    user_id = uuid.uuid4()
    cover_letter_id = uuid.uuid4()

    created = sql_repo.record(
        user_id=user_id,
        cover_letter_id=cover_letter_id,
        letter_text="Hello, I am writing to express my interest.",
        style_summary="first-sentence: Hello; words=8",
    )

    assert created.id is not None
    assert created.created_at is not None

    listed = sql_repo.list_for_user(user_id)
    assert len(listed) == 1
    assert listed[0].id == created.id
    assert listed[0].letter_text == "Hello, I am writing to express my interest."
    assert listed[0].style_summary == "first-sentence: Hello; words=8"


def test_sql_list_for_user_returns_newest_first(
    sql_repo: SqlStyleMemoryRepository,
) -> None:
    """``list_for_user`` must return entries newest-first.

    The two records are separated by a small sleep so the
    server-side ``created_at`` differs by at least one resolution
    unit; sqlite's ``CURRENT_TIMESTAMP`` only has second-precision
    but the in-memory store's primary key (UUID) is monotonically
    increasing, so the tiebreaker still ranks the second insert
    first.
    """
    import time

    user_id = uuid.uuid4()
    e1 = sql_repo.record(
        user_id=user_id,
        cover_letter_id=uuid.uuid4(),
        letter_text="older",
        style_summary="A",
    )
    time.sleep(1.1)
    e2 = sql_repo.record(
        user_id=user_id,
        cover_letter_id=uuid.uuid4(),
        letter_text="newer",
        style_summary="B",
    )

    listed = sql_repo.list_for_user(user_id)
    # e2 (the later record) must come first.
    assert listed[0].id == e2.id
    assert listed[1].id == e1.id


def test_sql_list_for_user_isolates_users(
    sql_repo: SqlStyleMemoryRepository,
) -> None:
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    sql_repo.record(
        user_id=user_a,
        cover_letter_id=uuid.uuid4(),
        letter_text="a",
        style_summary="A",
    )
    sql_repo.record(
        user_id=user_b,
        cover_letter_id=uuid.uuid4(),
        letter_text="b",
        style_summary="B",
    )

    assert [e.letter_text for e in sql_repo.list_for_user(user_a)] == ["a"]
    assert [e.letter_text for e in sql_repo.list_for_user(user_b)] == ["b"]


def test_sql_get_aggregated_returns_none_for_empty(
    sql_repo: SqlStyleMemoryRepository,
) -> None:
    assert sql_repo.get_aggregated(uuid.uuid4()) is None


def test_sql_get_aggregated_concatenates_recent_summaries(
    sql_repo: SqlStyleMemoryRepository,
) -> None:
    """``get_aggregated`` must join the most recent entries' summaries.

    The two records are separated by a small sleep so the
    server-side ``created_at`` differs; the tiebreaker (UUID lex
    order) is not enough to guarantee the latest record wins on
    sqlite's second-precision timestamp.
    """
    import time

    user_id = uuid.uuid4()
    sql_repo.record(
        user_id=user_id,
        cover_letter_id=uuid.uuid4(),
        letter_text="older",
        style_summary="SUMMARY-A",
    )
    time.sleep(1.1)
    sql_repo.record(
        user_id=user_id,
        cover_letter_id=uuid.uuid4(),
        letter_text="newer",
        style_summary="SUMMARY-B",
    )

    aggregated = sql_repo.get_aggregated(user_id)
    assert aggregated is not None
    assert aggregated.index("SUMMARY-B") < aggregated.index("SUMMARY-A")


def test_sql_persists_via_session_factory(
    sql_repo: SqlStyleMemoryRepository,
    session_factory: sessionmaker[Session],
) -> None:
    """The SQL repo must be queryable through a fresh session from the factory."""
    user_id = uuid.uuid4()
    sql_repo.record(
        user_id=user_id,
        cover_letter_id=uuid.uuid4(),
        letter_text="Hi",
        style_summary="first-sentence: Hi; words=1",
    )

    with session_factory() as session:
        rows = session.query(StyleMemoryEntryModel).all()
    assert len(rows) == 1
    assert rows[0].letter_text == "Hi"
