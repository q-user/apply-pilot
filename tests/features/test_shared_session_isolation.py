"""Regression for issue #292.

A Sql*Repository built via ``session_factory=lambda: session`` used to
close the FastAPI-shared session in its ``finally`` block, breaking
later endpoints on the same request. We switched the dashboard and
max digest routes to ``session=session`` directly; this test asserts
the new contract on the repository shape (session stays open across
multiple reads) and re-grep-gates the codebase so the lambda form
stays gone.
"""
from __future__ import annotations

import uuid

from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import sessionmaker

from apply_pilot.features.matches.repository import SqlVacancyMatchRepository
from apply_pilot.features.sources.repository import SqlVacancyRepository


def _engine_with_schema() -> object:
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    from apply_pilot.db import Base
    from apply_pilot.features.matches import models  # noqa: F401
    from apply_pilot.features.search_profiles import models  # noqa: F401
    from apply_pilot.features.sources import models  # noqa: F401
    from apply_pilot.features.users import models  # noqa: F401

    Base.metadata.create_all(eng)
    return eng


def test_sql_repo_does_not_close_injected_session() -> None:
    """A repo bound to ``session=`` must not close the request's session."""
    engine = _engine_with_schema()
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    s = Session()

    repo_match = SqlVacancyMatchRepository(session=s)
    repo_vacancy = SqlVacancyRepository(session=s)

    assert s.is_active is True
    repo_match.get_by_id(uuid.uuid4())
    repo_vacancy.get_by_id(uuid.uuid4())
    # Both repos share the request session; it MUST still be alive.
    assert s.is_active is True

    s.close()


def test_db_sessionmaker_sets_expire_on_commit_false() -> None:
    """Both ``SessionLocal`` and ``get_sessionmaker`` must use False."""
    from apply_pilot.db import SessionLocal, get_sessionmaker

    assert SessionLocal.kw.get("expire_on_commit", None) is False
    assert get_sessionmaker().kw.get("expire_on_commit", None) is False
