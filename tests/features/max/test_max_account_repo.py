"""Tests for the MAX account repositories.

Covers both the in-memory implementation
(:class:`~apply_pilot.features.max.repository.InMemoryMaxAccountRepository`)
and the SQLAlchemy implementation
(:class:`~apply_pilot.features.max.repository.SqlAlchemyMaxAccountRepository`).

The in-memory repo is exercised with a plain ``MaxAccount`` row; the
SQL repo is exercised against an in-memory sqlite engine using the
``StaticPool`` pattern from
:mod:`tests.features.users.test_admin_promotion` so each test gets a
fresh database without touching the network or a real Postgres
instance.

Both repos expose the same surface (``create``, ``list_all``,
``find_by_max_user_id``, ``find_by_user_id``,
``find_by_external_user_id``) and share the same uniqueness invariants
(``user_id`` is ``UNIQUE``, ``max_user_id`` is ``UNIQUE``); the
behavioural tests below assert both invariants on each implementation
so a future divergence is caught by the suite.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from apply_pilot.db import Base
from apply_pilot.features.max import models as _max_models  # noqa: F401  (register MaxAccount)
from apply_pilot.features.max.models import MaxAccount
from apply_pilot.features.max.repository import (
    InMemoryMaxAccountRepository,
    SqlAlchemyMaxAccountRepository,
    _DuplicateMaxAccountError,
)

# ---------------------------------------------------------------------------
# SQL fixture (mirrors ``tests/features/users/test_admin_promotion.py``)
# ---------------------------------------------------------------------------


@pytest.fixture
def engine() -> Iterator[Engine]:
    """Build a fresh in-memory sqlite engine and create the MAX tables on it."""
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
def session_factory(engine: Engine) -> Iterator[Session]:
    """Yield a short-lived :class:`Session` bound to *engine*."""
    factory = sessionmaker(bind=engine, class_=Session, autocommit=False, autoflush=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()


# ---------------------------------------------------------------------------
# InMemoryMaxAccountRepository
# ---------------------------------------------------------------------------


def test_inmemory_create_returns_maxaccount_with_linked_at() -> None:
    """``create`` returns a :class:`MaxAccount` whose ``linked_at`` is set."""
    repo = InMemoryMaxAccountRepository()
    user_id = uuid.uuid4()

    account = repo.create(user_id=user_id, max_user_id=100, username="alice")

    assert isinstance(account, MaxAccount)
    assert account.user_id == user_id
    assert account.max_user_id == 100
    assert account.username == "alice"
    assert account.linked_at is not None
    assert account.id is not None


def test_inmemory_create_duplicate_user_id_raises() -> None:
    """A second ``create`` for the same ``user_id`` is rejected."""
    repo = InMemoryMaxAccountRepository()
    user_id = uuid.uuid4()
    repo.create(user_id=user_id, max_user_id=100)

    with pytest.raises(_DuplicateMaxAccountError):
        repo.create(user_id=user_id, max_user_id=200)


def test_inmemory_create_duplicate_max_user_id_raises() -> None:
    """A second ``create`` for the same ``max_user_id`` is rejected."""
    repo = InMemoryMaxAccountRepository()
    repo.create(user_id=uuid.uuid4(), max_user_id=100)

    with pytest.raises(_DuplicateMaxAccountError):
        repo.create(user_id=uuid.uuid4(), max_user_id=100)


def test_inmemory_list_all_returns_insertion_order() -> None:
    """``list_all`` preserves insertion order so a broadcast is deterministic."""
    repo = InMemoryMaxAccountRepository()
    first = repo.create(user_id=uuid.uuid4(), max_user_id=10)
    second = repo.create(user_id=uuid.uuid4(), max_user_id=20)
    third = repo.create(user_id=uuid.uuid4(), max_user_id=30)

    rows = repo.list_all()

    assert [r.id for r in rows] == [first.id, second.id, third.id]


def test_inmemory_find_by_max_user_id_returns_account() -> None:
    """``find_by_max_user_id`` resolves a linked :class:`MaxAccount`."""
    repo = InMemoryMaxAccountRepository()
    user_id = uuid.uuid4()
    created = repo.create(user_id=user_id, max_user_id=12345)

    found = repo.find_by_max_user_id(12345)

    assert found is not None
    assert found.id == created.id
    assert found.user_id == user_id


def test_inmemory_find_by_max_user_id_returns_none_for_missing() -> None:
    """``find_by_max_user_id`` returns ``None`` when no row matches."""
    repo = InMemoryMaxAccountRepository()
    assert repo.find_by_max_user_id(999_999) is None


def test_inmemory_find_by_user_id_returns_account() -> None:
    """``find_by_user_id`` returns the linked :class:`MaxAccount`."""
    repo = InMemoryMaxAccountRepository()
    user_id = uuid.uuid4()
    created = repo.create(user_id=user_id, max_user_id=77)

    found = repo.find_by_user_id(user_id)

    assert found is not None
    assert found.id == created.id
    assert found.max_user_id == 77


def test_inmemory_find_by_user_id_returns_none_for_missing() -> None:
    """``find_by_user_id`` returns ``None`` for an unlinked user."""
    repo = InMemoryMaxAccountRepository()
    assert repo.find_by_user_id(uuid.uuid4()) is None


def test_inmemory_find_by_external_user_id_is_alias_for_max_user_id() -> None:
    """``find_by_external_user_id`` is a channel-agnostic alias for ``max_user_id``."""
    repo = InMemoryMaxAccountRepository()
    created = repo.create(user_id=uuid.uuid4(), max_user_id=555)

    found = repo.find_by_external_user_id(555)

    assert found is not None
    assert found.id == created.id


def test_inmemory_find_by_external_user_id_returns_none_for_missing() -> None:
    """``find_by_external_user_id`` returns ``None`` when no link exists."""
    repo = InMemoryMaxAccountRepository()
    assert repo.find_by_external_user_id(0) is None


# ---------------------------------------------------------------------------
# SqlAlchemyMaxAccountRepository
# ---------------------------------------------------------------------------


def test_sql_create_persists_row(session_factory: Session) -> None:
    """``create`` inserts a row and refreshes ``linked_at`` from the DB."""
    repo = SqlAlchemyMaxAccountRepository(session=session_factory)
    user_id = uuid.uuid4()

    account = repo.create(user_id=user_id, max_user_id=100, username="bob")

    assert account.id is not None
    assert account.user_id == user_id
    assert account.max_user_id == 100
    assert account.username == "bob"
    assert account.linked_at is not None


def test_sql_create_duplicate_user_id_raises(session_factory: Session) -> None:
    """A second ``create`` for the same ``user_id`` is rejected by the SQL repo."""
    repo = SqlAlchemyMaxAccountRepository(session=session_factory)
    user_id = uuid.uuid4()
    repo.create(user_id=user_id, max_user_id=100)

    with pytest.raises(_DuplicateMaxAccountError):
        repo.create(user_id=user_id, max_user_id=200)


def test_sql_create_duplicate_max_user_id_raises(session_factory: Session) -> None:
    """A second ``create`` for the same ``max_user_id`` is rejected by the SQL repo."""
    from sqlalchemy.exc import IntegrityError

    repo = SqlAlchemyMaxAccountRepository(session=session_factory)
    repo.create(user_id=uuid.uuid4(), max_user_id=100)

    with pytest.raises((_DuplicateMaxAccountError, IntegrityError)):
        repo.create(user_id=uuid.uuid4(), max_user_id=100)


def test_sql_list_all_returns_persisted_rows(session_factory: Session) -> None:
    """``list_all`` returns every persisted row."""
    repo = SqlAlchemyMaxAccountRepository(session=session_factory)
    repo.create(user_id=uuid.uuid4(), max_user_id=1)
    repo.create(user_id=uuid.uuid4(), max_user_id=2)

    rows = repo.list_all()

    assert {r.max_user_id for r in rows} == {1, 2}
    assert len(rows) == 2


def test_sql_find_by_max_user_id_returns_account(session_factory: Session) -> None:
    """``find_by_max_user_id`` round-trips through SQL."""
    repo = SqlAlchemyMaxAccountRepository(session=session_factory)
    user_id = uuid.uuid4()
    created = repo.create(user_id=user_id, max_user_id=200)

    found = repo.find_by_max_user_id(200)

    assert found is not None
    assert found.id == created.id
    assert found.user_id == user_id


def test_sql_find_by_max_user_id_returns_none_for_missing(session_factory: Session) -> None:
    """``find_by_max_user_id`` returns ``None`` when no row matches."""
    repo = SqlAlchemyMaxAccountRepository(session=session_factory)
    assert repo.find_by_max_user_id(999_999) is None


def test_sql_find_by_user_id_returns_account(session_factory: Session) -> None:
    """``find_by_user_id`` round-trips through SQL."""
    repo = SqlAlchemyMaxAccountRepository(session=session_factory)
    user_id = uuid.uuid4()
    created = repo.create(user_id=user_id, max_user_id=300)

    found = repo.find_by_user_id(user_id)

    assert found is not None
    assert found.id == created.id
    assert found.max_user_id == 300


def test_sql_find_by_user_id_returns_none_for_missing(session_factory: Session) -> None:
    """``find_by_user_id`` returns ``None`` for an unlinked user."""
    repo = SqlAlchemyMaxAccountRepository(session=session_factory)
    assert repo.find_by_user_id(uuid.uuid4()) is None


def test_sql_find_by_external_user_id_is_alias(session_factory: Session) -> None:
    """``find_by_external_user_id`` is a channel-agnostic alias for ``max_user_id``."""
    repo = SqlAlchemyMaxAccountRepository(session=session_factory)
    created = repo.create(user_id=uuid.uuid4(), max_user_id=400)

    found = repo.find_by_external_user_id(400)

    assert found is not None
    assert found.id == created.id


def test_sql_constructor_rejects_both_session_and_factory() -> None:
    """Passing both ``session`` and ``session_factory`` raises :class:`ValueError`."""
    eng = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=eng)
    factory = sessionmaker(bind=eng, class_=Session)
    session = factory()
    try:
        with pytest.raises(ValueError, match="either session or session_factory"):
            SqlAlchemyMaxAccountRepository(session=session, session_factory=factory)
    finally:
        session.close()
        eng.dispose()
