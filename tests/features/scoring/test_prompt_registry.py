"""TDD tests for the :class:`PromptVersionRegistry` contract.

Two implementations are exercised:

* :class:`InMemoryPromptVersionRegistry` — dict-backed fake for tests.
* :class:`SqlPromptVersionRegistry` — SQLAlchemy-backed production
  implementation.

Both implementations must:

* register a new :class:`PromptVersion` and return it unchanged;
* look up the *active* version by prompt name;
* look up a specific ``(name, version)`` pair;
* list every version for a name (or all prompts when ``name=None``);
* ensure only one version per name is active — :meth:`set_active`
  deactivates the previously active version atomically.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from apply_pilot.db import Base
from apply_pilot.features.scoring import models as _scoring_models  # noqa: F401
from apply_pilot.features.scoring.models import PromptVersionRow
from apply_pilot.features.scoring.registry import (
    InMemoryPromptVersionRegistry,
    PromptVersion,
    SqlPromptVersionRegistry,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _prompt(
    name: str = "vacancy_scoring",
    version: str = "1.0.0",
    template: str = "Score this vacancy: {{vacancy}}",
    *,
    is_active: bool = True,
) -> PromptVersion:
    """Build a fully-populated :class:`PromptVersion`."""
    return PromptVersion(
        name=name,
        version=version,
        template=template,
        is_active=is_active,
        created_at=datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------
# PromptVersion value object
# ---------------------------------------------------------------------------


def test_prompt_version_is_frozen() -> None:
    """A :class:`PromptVersion` is immutable — ``frozen=True``."""
    prompt = _prompt()

    with pytest.raises((AttributeError, Exception)):  # FrozenInstanceError
        prompt.version = "2.0.0"  # type: ignore[misc]


def test_prompt_version_carries_all_fields() -> None:
    """All five public fields are accessible on the dataclass."""
    prompt = _prompt(
        name="cover_letter",
        version="1.2.0-rc.1",
        template="Write a letter: {{vacancy}}",
        is_active=False,
    )

    assert prompt.name == "cover_letter"
    assert prompt.version == "1.2.0-rc.1"
    assert prompt.template == "Write a letter: {{vacancy}}"
    assert prompt.is_active is False
    assert prompt.created_at == datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# In-memory registry
# ---------------------------------------------------------------------------


@pytest.fixture
def in_memory() -> InMemoryPromptVersionRegistry:
    return InMemoryPromptVersionRegistry()


def test_in_memory_register_returns_prompt(in_memory: InMemoryPromptVersionRegistry) -> None:
    """``register`` must return the same prompt that was passed in."""
    prompt = _prompt()

    result = in_memory.register(prompt)

    assert result is prompt
    assert result.name == "vacancy_scoring"
    assert result.version == "1.0.0"


def test_in_memory_get_active_returns_active_version(
    in_memory: InMemoryPromptVersionRegistry,
) -> None:
    """``get_active`` returns the version that was registered with ``is_active=True``."""
    in_memory.register(_prompt(version="1.0.0", is_active=False))
    active = in_memory.register(_prompt(version="1.1.0", is_active=True))

    fetched = in_memory.get_active("vacancy_scoring")

    assert fetched is not None
    assert fetched.version == "1.1.0"
    assert fetched.version == active.version


def test_in_memory_get_active_returns_none_for_unknown_name(
    in_memory: InMemoryPromptVersionRegistry,
) -> None:
    """``get_active`` for an unregistered name must return ``None``."""
    assert in_memory.get_active("never_registered") is None


def test_in_memory_get_active_returns_none_when_no_active(
    in_memory: InMemoryPromptVersionRegistry,
) -> None:
    """A name with only inactive versions returns ``None`` from ``get_active``."""
    in_memory.register(_prompt(version="1.0.0", is_active=False))

    assert in_memory.get_active("vacancy_scoring") is None


def test_in_memory_get_specific_version(in_memory: InMemoryPromptVersionRegistry) -> None:
    """``get`` returns the row matching ``(name, version)``."""
    in_memory.register(_prompt(version="1.0.0"))
    target = in_memory.register(_prompt(version="1.1.0"))

    fetched = in_memory.get("vacancy_scoring", "1.1.0")

    assert fetched is not None
    assert fetched.version == target.version
    assert fetched.template == target.template


def test_in_memory_get_returns_none_for_unknown_pair(
    in_memory: InMemoryPromptVersionRegistry,
) -> None:
    """``get`` for a non-existent ``(name, version)`` returns ``None``."""
    in_memory.register(_prompt(version="1.0.0"))

    assert in_memory.get("vacancy_scoring", "9.9.9") is None
    assert in_memory.get("never_registered", "1.0.0") is None


def test_in_memory_list_all_filters_by_name(
    in_memory: InMemoryPromptVersionRegistry,
) -> None:
    """``list_all(name=...)`` returns only versions for the given name."""
    in_memory.register(_prompt(name="vacancy_scoring", version="1.0.0"))
    in_memory.register(_prompt(name="vacancy_scoring", version="1.1.0"))
    in_memory.register(_prompt(name="cover_letter", version="1.0.0"))

    only_scoring = in_memory.list_all(name="vacancy_scoring")

    assert {p.version for p in only_scoring} == {"1.0.0", "1.1.0"}


def test_in_memory_list_all_returns_every_prompt_when_name_is_none(
    in_memory: InMemoryPromptVersionRegistry,
) -> None:
    """``list_all(name=None)`` returns every registered prompt version."""
    in_memory.register(_prompt(name="vacancy_scoring", version="1.0.0"))
    in_memory.register(_prompt(name="cover_letter", version="1.0.0"))
    in_memory.register(_prompt(name="cover_letter", version="1.1.0"))

    everything = in_memory.list_all()

    assert {p.name for p in everything} == {"vacancy_scoring", "cover_letter"}
    assert len(everything) == 3


def test_in_memory_set_active_deactivates_previous(
    in_memory: InMemoryPromptVersionRegistry,
) -> None:
    """Calling ``set_active`` flips the bit so only one version is active."""
    in_memory.register(_prompt(version="1.0.0", is_active=True))
    in_memory.register(_prompt(version="1.1.0", is_active=False))

    result = in_memory.set_active("vacancy_scoring", "1.1.0")

    assert result.is_active is True
    assert result.version == "1.1.0"
    previous = in_memory.get("vacancy_scoring", "1.0.0")
    assert previous is not None
    assert previous.is_active is False


def test_in_memory_set_active_raises_for_unknown_version(
    in_memory: InMemoryPromptVersionRegistry,
) -> None:
    """``set_active`` for a version that does not exist must raise."""
    in_memory.register(_prompt(version="1.0.0", is_active=True))

    with pytest.raises(ValueError, match="not found"):
        in_memory.set_active("vacancy_scoring", "9.9.9")


# ---------------------------------------------------------------------------
# SQL registry
# ---------------------------------------------------------------------------


@pytest.fixture
def engine() -> Iterator[Engine]:
    """Yield a fresh in-memory sqlite engine with the ``prompt_versions`` table.

    Only the :class:`PromptVersionRow` table is created — the registry tests
    do not depend on any other slice's schema, and scoping ``create_all`` to
    a single table makes the fixture robust against cross-test model
    registration. Other test modules in the scoring directory import
    ``search_profiles`` / ``vacancies`` etc. (via ``service.py`` /
    ``llm.py``); those models live in ``Base.metadata`` once any test file
    is collected, and creating them here would fail their FK constraints
    (``search_profiles`` references ``users``) without dragging the whole
    schema in.
    """
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=eng, tables=[PromptVersionRow.__table__])
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> Iterator[sessionmaker[Session]]:
    factory = sessionmaker(bind=engine, class_=Session, autocommit=False, autoflush=False)
    yield factory


@pytest.fixture
def sql_repo(session_factory: sessionmaker[Session]) -> SqlPromptVersionRegistry:
    return SqlPromptVersionRegistry(session_factory=session_factory)


def test_sql_register_persists_row(sql_repo: SqlPromptVersionRegistry) -> None:
    """A registered prompt version must round-trip through the SQL repo."""
    prompt = _prompt(version="1.0.0", template="Score: {{v}}")

    result = sql_repo.register(prompt)

    assert result.name == "vacancy_scoring"
    assert result.version == "1.0.0"
    assert result.template == "Score: {{v}}"
    assert result.is_active is True

    fetched = sql_repo.get("vacancy_scoring", "1.0.0")
    assert fetched is not None
    assert fetched.template == "Score: {{v}}"


def test_sql_get_active_returns_active_version(
    sql_repo: SqlPromptVersionRegistry,
) -> None:
    sql_repo.register(_prompt(version="1.0.0", is_active=False))
    sql_repo.register(_prompt(version="1.1.0", is_active=True))

    fetched = sql_repo.get_active("vacancy_scoring")

    assert fetched is not None
    assert fetched.version == "1.1.0"


def test_sql_set_active_deactivates_previous(
    sql_repo: SqlPromptVersionRegistry,
) -> None:
    """After ``set_active`` only the new version must be active in the DB."""
    sql_repo.register(_prompt(version="1.0.0", is_active=True))
    sql_repo.register(_prompt(version="1.1.0", is_active=False))

    result = sql_repo.set_active("vacancy_scoring", "1.1.0")

    assert result.is_active is True
    refreshed = sql_repo.get("vacancy_scoring", "1.0.0")
    assert refreshed is not None
    assert refreshed.is_active is False


def test_sql_unique_active_per_name_at_db_level(
    sql_repo: SqlPromptVersionRegistry,
) -> None:
    """The DB-level partial unique index must reject two active versions."""
    import sqlalchemy.exc

    sql_repo.register(_prompt(version="1.0.0", is_active=True))
    session_factory = sql_repo._session_factory  # noqa: SLF001
    with session_factory() as session:
        from apply_pilot.features.scoring.models import PromptVersionRow

        row = PromptVersionRow(
            id=uuid.uuid4(),
            name="vacancy_scoring",
            version="1.1.0",
            template="Bypass",
            is_active=True,
        )
        session.add(row)
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            session.commit()
        session.rollback()


def test_sql_unique_name_version_pair_at_db_level(
    sql_repo: SqlPromptVersionRegistry,
) -> None:
    """Re-registering the same ``(name, version)`` must raise IntegrityError."""
    import sqlalchemy.exc

    sql_repo.register(_prompt(version="1.0.0"))
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        sql_repo.register(_prompt(version="1.0.0", is_active=False))
