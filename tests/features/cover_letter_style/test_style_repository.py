"""TDD tests for the ``CoverLetterStyle`` persistence gateway.

Two implementations are exercised:

* :class:`InMemoryCoverLetterStyleRepository` — dict-backed fake.
* :class:`SqlCoverLetterStyleRepository` — SQLAlchemy-backed production
  implementation backed by a sqlite in-memory engine.

Both implementations must satisfy the same Protocol contract: one style
per user (uniqueness on ``user_id``), create / update / delete by user,
and JSON-encoding of ``focus_areas`` / ``avoid_phrases`` so the DB stays
portable across sqlite and PostgreSQL.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from apply_pilot.db import Base
from apply_pilot.features.cover_letter_style.models import CoverLetterStyle
from apply_pilot.features.cover_letter_style.repository import (
    InMemoryCoverLetterStyleRepository,
    SqlCoverLetterStyleRepository,
)

# ---------------------------------------------------------------------------
# In-memory repository
# ---------------------------------------------------------------------------


@pytest.fixture
def in_memory_repo() -> InMemoryCoverLetterStyleRepository:
    return InMemoryCoverLetterStyleRepository()


def test_in_memory_get_by_user_returns_none_for_unknown_user(
    in_memory_repo: InMemoryCoverLetterStyleRepository,
) -> None:
    """Looking up a user without a style must return ``None``."""
    assert in_memory_repo.get_by_user(uuid.uuid4()) is None


def test_in_memory_create_assigns_id_and_timestamps(
    in_memory_repo: InMemoryCoverLetterStyleRepository,
) -> None:
    """Creating a style must auto-assign id and created_at."""
    user_id = uuid.uuid4()
    style = CoverLetterStyle(user_id=user_id, tone="friendly", length="short")

    result = in_memory_repo.create(style)

    assert result.id is not None
    assert result.user_id == user_id
    assert result.tone == "friendly"
    assert result.length == "short"
    assert result.created_at is not None


def test_in_memory_get_by_user_returns_created_style(
    in_memory_repo: InMemoryCoverLetterStyleRepository,
) -> None:
    """The created style must be retrievable by user id."""
    user_id = uuid.uuid4()
    created = in_memory_repo.create(CoverLetterStyle(user_id=user_id))

    fetched = in_memory_repo.get_by_user(user_id)

    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.user_id == user_id


def test_in_memory_default_fields_populated(
    in_memory_repo: InMemoryCoverLetterStyleRepository,
) -> None:
    """Defaults for tone/length/lists must be set if not provided by caller."""
    user_id = uuid.uuid4()

    created = in_memory_repo.create(CoverLetterStyle(user_id=user_id))

    assert created.tone == "professional"
    assert created.length == "medium"
    assert created.focus_areas == []
    assert created.avoid_phrases == []


def test_in_memory_focus_areas_and_avoid_phrases_round_trip(
    in_memory_repo: InMemoryCoverLetterStyleRepository,
) -> None:
    """List[str] round-trips through the repository unchanged."""
    user_id = uuid.uuid4()
    style = CoverLetterStyle(
        user_id=user_id,
        focus_areas=["technical_skills", "teamwork", "results"],
        avoid_phrases=["rockstar", "ninja"],
    )

    created = in_memory_repo.create(style)
    fetched = in_memory_repo.get_by_user(user_id)

    assert fetched is not None
    assert fetched.focus_areas == ["technical_skills", "teamwork", "results"]
    assert fetched.avoid_phrases == ["rockstar", "ninja"]
    assert created.focus_areas == ["technical_skills", "teamwork", "results"]


def test_in_memory_extra_instructions_nullable(
    in_memory_repo: InMemoryCoverLetterStyleRepository,
) -> None:
    """``extra_instructions`` must accept None."""
    user_id = uuid.uuid4()

    created = in_memory_repo.create(CoverLetterStyle(user_id=user_id))

    assert created.extra_instructions is None

    updated = in_memory_repo.update(
        CoverLetterStyle(
            id=created.id,
            user_id=user_id,
            extra_instructions="Use first-person voice",
        )
    )
    assert updated.extra_instructions == "Use first-person voice"


def test_in_memory_update_changes_fields(
    in_memory_repo: InMemoryCoverLetterStyleRepository,
) -> None:
    """An update must replace scalar fields and set ``updated_at``."""
    user_id = uuid.uuid4()
    created = in_memory_repo.create(CoverLetterStyle(user_id=user_id, tone="concise"))

    updated = in_memory_repo.update(
        CoverLetterStyle(
            id=created.id,
            user_id=user_id,
            tone="formal",
            length="long",
            focus_areas=["leadership"],
        )
    )

    assert updated.tone == "formal"
    assert updated.length == "long"
    assert updated.focus_areas == ["leadership"]
    assert updated.updated_at is not None


def test_in_memory_delete_by_user_removes_style(
    in_memory_repo: InMemoryCoverLetterStyleRepository,
) -> None:
    """``delete_by_user`` must remove the style and return True."""
    user_id = uuid.uuid4()
    in_memory_repo.create(CoverLetterStyle(user_id=user_id))

    deleted = in_memory_repo.delete_by_user(user_id)

    assert deleted is True
    assert in_memory_repo.get_by_user(user_id) is None


def test_in_memory_delete_by_user_returns_false_when_absent(
    in_memory_repo: InMemoryCoverLetterStyleRepository,
) -> None:
    """``delete_by_user`` must return False if no style exists for the user."""
    assert in_memory_repo.delete_by_user(uuid.uuid4()) is False


def test_in_memory_create_rejects_duplicate_user(
    in_memory_repo: InMemoryCoverLetterStyleRepository,
) -> None:
    """Two styles for the same user must be impossible (one-style-per-user)."""
    user_id = uuid.uuid4()
    in_memory_repo.create(CoverLetterStyle(user_id=user_id))

    with pytest.raises(ValueError, match="already exists"):
        in_memory_repo.create(CoverLetterStyle(user_id=user_id))


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
    # Import models so SQLAlchemy knows about the table.
    from apply_pilot.features.cover_letter_style import models  # noqa: F401

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
) -> SqlCoverLetterStyleRepository:
    return SqlCoverLetterStyleRepository(session_factory=session_factory)


def test_sql_get_by_user_returns_none_for_unknown_user(
    sql_repo: SqlCoverLetterStyleRepository,
) -> None:
    assert sql_repo.get_by_user(uuid.uuid4()) is None


def test_sql_create_and_get_round_trip(
    sql_repo: SqlCoverLetterStyleRepository,
) -> None:
    user_id = uuid.uuid4()

    created = sql_repo.create(
        CoverLetterStyle(
            user_id=user_id,
            tone="enthusiastic",
            length="long",
            focus_areas=["results", "leadership"],
            avoid_phrases=["hard worker"],
            extra_instructions="Mention cross-team collaboration.",
        )
    )

    assert created.id is not None
    assert created.created_at is not None

    fetched = sql_repo.get_by_user(user_id)

    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.tone == "enthusiastic"
    assert fetched.length == "long"
    assert fetched.focus_areas == ["results", "leadership"]
    assert fetched.avoid_phrases == ["hard worker"]
    assert fetched.extra_instructions == "Mention cross-team collaboration."


def test_sql_stores_lists_as_json_text(
    sql_repo: SqlCoverLetterStyleRepository,
    session_factory: sessionmaker[Session],
) -> None:
    """JSON-encoded TEXT storage keeps the migration portable."""
    user_id = uuid.uuid4()
    sql_repo.create(
        CoverLetterStyle(
            user_id=user_id,
            focus_areas=["a", "b"],
            avoid_phrases=["c"],
        )
    )

    # Inspect the raw column to confirm JSON text is being persisted.
    with session_factory() as session:
        row = session.get(CoverLetterStyle, sql_repo.get_by_user(user_id).id)
        assert row is not None
        assert json.loads(row.focus_areas) == ["a", "b"]
        assert json.loads(row.avoid_phrases) == ["c"]


def test_sql_update_changes_fields(
    sql_repo: SqlCoverLetterStyleRepository,
) -> None:
    user_id = uuid.uuid4()
    created = sql_repo.create(CoverLetterStyle(user_id=user_id, tone="concise"))

    updated = sql_repo.update(
        CoverLetterStyle(
            id=created.id,
            user_id=user_id,
            tone="formal",
            length="long",
            focus_areas=["technical_skills"],
            avoid_phrases=["ninja"],
            extra_instructions="Quantify impact with numbers.",
        )
    )

    assert updated.tone == "formal"
    assert updated.length == "long"
    assert updated.focus_areas == ["technical_skills"]
    assert updated.avoid_phrases == ["ninja"]
    assert updated.extra_instructions == "Quantify impact with numbers."

    fetched = sql_repo.get_by_user(user_id)
    assert fetched is not None
    assert fetched.tone == "formal"
    assert fetched.length == "long"


def test_sql_delete_by_user_removes_style(
    sql_repo: SqlCoverLetterStyleRepository,
) -> None:
    user_id = uuid.uuid4()
    sql_repo.create(CoverLetterStyle(user_id=user_id))

    deleted = sql_repo.delete_by_user(user_id)

    assert deleted is True
    assert sql_repo.get_by_user(user_id) is None


def test_sql_delete_by_user_returns_false_when_absent(
    sql_repo: SqlCoverLetterStyleRepository,
) -> None:
    assert sql_repo.delete_by_user(uuid.uuid4()) is False


def test_sql_unique_user_id_constraint(
    sql_repo: SqlCoverLetterStyleRepository,
) -> None:
    """Two styles for the same user must fail at the DB level."""
    import sqlalchemy.exc

    user_id = uuid.uuid4()
    sql_repo.create(CoverLetterStyle(user_id=user_id))

    with pytest.raises(sqlalchemy.exc.IntegrityError):
        sql_repo.create(CoverLetterStyle(user_id=user_id))
