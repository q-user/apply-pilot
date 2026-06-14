"""Integration tests for the SQLAlchemy-backed UsersRepository.

Exercises the real ORM code path against sqlite in-memory. The slice
contract (insert, fetch by id, fetch by email) must hold against the
same engine the application uses in production.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from job_apply.db import Base
from job_apply.features.users import models as _users_models  # noqa: F401
from job_apply.features.users.repository import SqlAlchemyUsersRepository


@pytest.fixture
def engine() -> Iterator[Engine]:
    """Build a fresh in-memory sqlite engine per test."""
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
def repo(engine: Engine) -> SqlAlchemyUsersRepository:
    """Return a repository bound to a single session over the test engine."""
    factory = sessionmaker(bind=engine, class_=Session, autocommit=False, autoflush=False)
    return SqlAlchemyUsersRepository(session_factory=factory)


def test_create_and_get_by_id(repo: SqlAlchemyUsersRepository) -> None:
    """create() must persist a user and get_by_id must round-trip it."""
    user = repo.create(
        email="alice@example.com",
        hashed_password="hashed-not-plaintext",
        is_active=True,
    )

    fetched = repo.get_by_id(user.id)
    assert fetched is not None
    assert fetched.id == user.id
    assert fetched.email == "alice@example.com"
    assert fetched.hashed_password == "hashed-not-plaintext"
    assert fetched.is_active is True


def test_get_by_email(repo: SqlAlchemyUsersRepository) -> None:
    """get_by_email must return the user with a matching email."""
    repo.create(email="bob@example.com", hashed_password="hashed", is_active=True)

    fetched = repo.get_by_email("bob@example.com")
    assert fetched is not None
    assert fetched.email == "bob@example.com"


def test_get_by_email_returns_none_for_unknown(repo: SqlAlchemyUsersRepository) -> None:
    """get_by_email must return None when no user matches."""
    assert repo.get_by_email("ghost@example.com") is None
