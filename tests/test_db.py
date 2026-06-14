"""Tests for :mod:`job_apply.db` and the Alembic wiring.

These tests are written for the M0 baseline slice (issue #7). They
exercise the public, DI-friendly surface — URL strings in, engine /
sessionmaker objects out — without patching module-level singletons.
The only mock-style escape hatch in the suite is the import of
:mod:`alembic.env` inside :func:`test_alembic_target_metadata_matches_base`,
where the test inspects the file's source as text rather than running
its migration runner.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import MetaData, text
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from job_apply.db import (
    Base,
    SessionLocal,
    async_session_factory,
    create_async_engine,
    create_engine,
    session_factory,
)

# ---------------------------------------------------------------------------
# 1. Engine construction from a URL string.
# ---------------------------------------------------------------------------


def test_engine_creation_from_url() -> None:
    """``create_engine`` builds a sync engine from a URL; ``engine.connect()``
    succeeds against an in-memory SQLite database."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        # ``connect()`` opens (and immediately releases) a real connection
        # — this is the contract the project relies on for the rest of
        # the test suite.
        with engine.connect() as connection:
            result = connection.execute(text("SELECT 1")).scalar()
            assert result == 1
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 2. Session factory yields a working session.
# ---------------------------------------------------------------------------


def test_session_factory_yields_session() -> None:
    """``SessionLocal()`` (a ``sessionmaker``) yields a ``Session`` that
    can execute a trivial ``SELECT 1`` via the context-manager API."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    SessionLocal = session_factory(engine)
    try:
        with SessionLocal() as session:
            assert isinstance(session, Session)
            result = session.execute(text("SELECT 1")).scalar()
            assert result == 1
    finally:
        engine.dispose()


def test_module_level_session_local_is_a_sessionmaker() -> None:
    """The module-level :data:`SessionLocal` is a ``sessionmaker`` instance
    bound to the configured engine — this is the singleton FastAPI route
    handlers consume via ``Depends(get_db)``."""
    assert isinstance(SessionLocal, sessionmaker)


# ---------------------------------------------------------------------------
# 3. Async engine / session factory.
# ---------------------------------------------------------------------------


def test_async_engine_creation() -> None:
    """``create_async_engine`` builds an :class:`AsyncEngine` and
    ``async_session_factory`` yields an :class:`AsyncSession` that can
    execute a trivial ``SELECT 1``."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        assert isinstance(engine, AsyncEngine)
        assert "aiosqlite" in engine.url.drivername

        SessionLocal = async_session_factory(engine)
        assert isinstance(SessionLocal, async_sessionmaker)

        async def _select_one() -> int:
            async with SessionLocal() as session:
                assert isinstance(session, AsyncSession)
                result = await session.execute(text("SELECT 1"))
                return int(result.scalar())

        assert asyncio.run(_select_one()) == 1
    finally:
        asyncio.run(engine.dispose())


def test_create_async_engine_rejects_sync_only_driver() -> None:
    """``create_async_engine`` is contractually async-only — a sync DSN
    raises :class:`InvalidRequestError` from SQLAlchemy itself. Pin the
    behaviour so a refactor doesn't silently start accepting sync URLs."""
    with pytest.raises(InvalidRequestError):
        create_async_engine("sqlite+pysqlite:///:memory:")


# ---------------------------------------------------------------------------
# 4. Base is the declarative base every model inherits from.
# ---------------------------------------------------------------------------


def test_base_metadata_is_declarative_base() -> None:
    """``Base.metadata`` is a :class:`MetaData` and ``Base`` is a
    :class:`DeclarativeBase` subclass — the contract the M0 foundation
    promises to every future vertical slice."""
    assert isinstance(Base.metadata, MetaData)
    assert isinstance(Base, type)
    assert issubclass(Base, DeclarativeBase)


# ---------------------------------------------------------------------------
# 5. Alembic baseline revision exists and applies cleanly.
# ---------------------------------------------------------------------------


REPO_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
ALEMBIC_VERSIONS = REPO_ROOT / "alembic" / "versions"
ALEMBIC_ENV_PY = REPO_ROOT / "alembic" / "env.py"
BASELINE_REVISION_FILE = ALEMBIC_VERSIONS / "0001_baseline.py"


def test_alembic_baseline_revision_exists() -> None:
    """The M0 baseline revision file is present and is the *only*
    migration in the project. ``alembic upgrade head`` against a temp
    SQLite URL is a no-op success — Alembic still records the head in
    the ``alembic_version`` table but applies no DDL."""
    assert BASELINE_REVISION_FILE.exists(), (
        f"Expected baseline revision at {BASELINE_REVISION_FILE}"
    )

    # No other migrations are allowed in M0.
    migration_files = sorted(p.name for p in ALEMBIC_VERSIONS.glob("*.py"))
    assert migration_files == ["0001_baseline.py"], (
        f"M0 baseline must be the only migration at head; found: {migration_files}"
    )

    # Programmatic ``alembic upgrade head`` against a fresh on-disk
    # SQLite database — the empty baseline is a no-op that still
    # succeeds and records the head revision.
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))

    db_path = REPO_ROOT / ".pytest-tmp-alembic.db"
    cfg.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{db_path.resolve()}")
    try:
        command.upgrade(cfg, "head")
        # A second upgrade is still a no-op (idempotent).
        command.upgrade(cfg, "head")
    finally:
        if db_path.exists():
            db_path.unlink()


# ---------------------------------------------------------------------------
# 6. Alembic env.py wires ``target_metadata`` to ``Base.metadata``.
# ---------------------------------------------------------------------------


def test_alembic_target_metadata_matches_base() -> None:
    """``alembic/env.py`` imports :data:`Base` from :mod:`job_apply.db`
    and assigns ``target_metadata = Base.metadata`` — this is what makes
    ``alembic revision --autogenerate`` see every future model."""
    contents = ALEMBIC_ENV_PY.read_text(encoding="utf-8")
    assert "from job_apply.db import Base" in contents
    assert "target_metadata = Base.metadata" in contents


# ---------------------------------------------------------------------------
# Sanity check: importing the package doesn't fail, and the module
# exposes the documented symbols. This guards against accidental
# refactors that drop a public name from ``__all__``.
# ---------------------------------------------------------------------------


def test_db_module_public_api() -> None:
    import job_apply.db as db_module

    expected = {
        "Base",
        "SessionLocal",
        "async_engine",
        "async_session_maker",
        "async_session_factory",
        "create_async_engine",
        "create_engine",
        "engine",
        "get_async_db",
        "get_db",
        "session_factory",
    }
    missing = expected - set(dir(db_module))
    assert not missing, f"job_apply.db lost public symbols: {sorted(missing)}"


# Iterator alias to keep the linter happy when future tests want a
# typed ``with`` expression (matches the shape ``get_db`` returns).
_ = Iterator[Session]
