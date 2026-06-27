"""Database primitives for SQLAlchemy 2.x.

This module exposes:

* `Base` — declarative base for ORM models in the package.
* `get_engine`, `get_sessionmaker` — DI-friendly factories that accept an
  optional `DatabaseSettings` override.
* `engine`, `SessionLocal` — module-level singletons built from the default
  settings (handy for scripts; prefer DI in tests and FastAPI dependencies).
* `get_db` — FastAPI dependency generator that yields a session and closes it
  on exit. Tests that need to inject a fake session factory use the separate
  :func:`get_db_with_factory` helper and register it via
  ``app.dependency_overrides[get_db]``; the public ``get_db`` signature must
  stay free of ``Callable`` annotations so Pydantic can emit a JSON Schema for
  every Depends parameter.
* `init_db` — convenience stub that creates all tables in `Base.metadata`;
  primarily for sqlite in-memory tests. Production uses Alembic migrations.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from apply_pilot.config import DatabaseSettings, get_database_settings


class Base(DeclarativeBase):
    """Declarative base used by all ORM models in the package."""


def _build_engine(settings: DatabaseSettings) -> Engine:
    """Construct a SQLAlchemy Engine honoring DatabaseSettings.

    Sqlite (especially in-memory) needs special handling so that the same
    in-memory database can be reused across sessions in a single process.
    """
    url = settings.database_url
    kwargs: dict[str, object] = {
        "future": True,
        "echo": settings.echo,
        "pool_pre_ping": settings.pool_pre_ping,
    }

    # SQLite has no connection pool; pool_size/max_overflow are Postgres-only tuning.
    if str(url).startswith("sqlite"):
        kwargs.pop("pool_size", None)
        kwargs.pop("max_overflow", None)
    if url.startswith("sqlite"):
        if ":memory:" in url:
            kwargs["poolclass"] = StaticPool
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(url, **kwargs)


def get_engine(settings: DatabaseSettings | None = None) -> Engine:
    """Return a SQLAlchemy Engine; use the provided settings or the env defaults."""
    return _build_engine(settings or get_database_settings())


def get_sessionmaker(settings: DatabaseSettings | None = None) -> sessionmaker[Session]:
    """Return a sessionmaker bound to an engine built from the given settings."""
    return sessionmaker(
        bind=get_engine(settings),
        class_=Session,
        autocommit=False,
        autoflush=False,
    )


# Module-level singletons (for scripts and existing call sites that import them).
engine: Engine = get_engine()
SessionLocal: sessionmaker[Session] = sessionmaker(
    bind=engine, class_=Session, autocommit=False, autoflush=False
)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: yield a session and close it on exit.

    Production callers use this directly. Tests that need to inject a
    fake session factory should call :func:`get_db_with_factory` and
    pass it to ``app.dependency_overrides[get_db]`` so the
    ``Callable[[], Session]`` parameter never lands in the FastAPI
    signature inspected by Pydantic's OpenAPI generator.
    """
    yield from get_db_with_factory()


def get_db_with_factory(
    session_factory: Callable[[], Session] | None = None,
) -> Iterator[Session]:
    """Like :func:`get_db` but with an overridable session factory.

    The callable is intentionally not a FastAPI dependency: Pydantic v2
    cannot emit a JSON Schema for ``Callable`` annotations, so exposing
    it through ``Depends`` breaks the entire OpenAPI document. Tests
    register this helper via ``app.dependency_overrides[get_db]`` to
    inject a fake factory without touching the production signature.
    """
    factory: Callable[[], Session] = (
        session_factory if session_factory is not None else SessionLocal
    )
    db = factory()
    try:
        yield db
    finally:
        db.close()


def init_db(engine: Engine | None = None) -> None:
    """Create all tables known to `Base.metadata`.

    Primarily used by tests with sqlite in-memory. Production should use
    Alembic migrations instead.
    """
    if engine is None:
        # Resolve the module-level default engine at call time to avoid
        # name-shadowing issues with the parameter.
        engine = globals()["engine"]  # type: ignore[assignment]
    Base.metadata.create_all(engine)
