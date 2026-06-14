"""Database primitives for SQLAlchemy 2.x.

The module exposes:

- :class:`Base` — the declarative base every ORM model inherits from.
  ``Base.metadata`` is the single source of truth that Alembic's
  ``env.py`` points at.
- :func:`create_engine` / :func:`create_async_engine` — pure factories
  that turn a SQLAlchemy URL string into an :class:`~sqlalchemy.Engine`
  or :class:`~sqlalchemy.ext.asyncio.AsyncEngine`.
- :func:`session_factory` / :func:`async_session_factory` — pure
  factories that turn an engine into a sessionmaker.
- Module-level :data:`engine`, :data:`SessionLocal`,
  :data:`async_engine`, :data:`async_session_maker` — singletons bound
  to :func:`job_apply.config.get_settings` for convenient FastAPI
  dependency wiring.
- :func:`get_db` / :func:`get_async_db` — generator dependencies
  suitable for ``Depends()`` in FastAPI route handlers.

The factory functions take primitives (URL strings, engine objects)
so tests can wire up dependency-injected engines without patching
module-level singletons.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
from typing import Any

from sqlalchemy import Engine, MetaData
from sqlalchemy import create_engine as _sa_create_engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)
from sqlalchemy.ext.asyncio import (
    create_async_engine as _sa_create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from job_apply.config import DatabaseSettings, Settings, get_settings


class Base(DeclarativeBase):
    """Declarative base shared by every ORM model in the project.

    ``Base.metadata`` is what Alembic's ``env.py`` sets as
    ``target_metadata`` — keep this in sync if you ever rename or
    re-parent the base class.
    """


def create_engine(url: str, **kwargs: Any) -> Engine:
    """Build a sync SQLAlchemy :class:`~sqlalchemy.Engine` from a URL.

    ``kwargs`` are forwarded to SQLAlchemy's :func:`create_engine`; common
    values include ``pool_size``, ``pool_pre_ping`` and ``echo``.
    """
    return _sa_create_engine(url, future=True, **kwargs)


def create_async_engine(url: str, **kwargs: Any) -> AsyncEngine:
    """Build an async SQLAlchemy :class:`AsyncEngine` from a URL.

    ``url`` must use an async driver (``sqlite+aiosqlite``,
    ``postgresql+asyncpg``, ``postgresql+psycopg``). A sync-only driver
    (e.g. ``sqlite+pysqlite``) raises :class:`sqlalchemy.exc.InvalidRequestError`
    — by design in SQLAlchemy itself.
    """
    return _sa_create_async_engine(url, **kwargs)


def session_factory(
    engine: Engine,
    *,
    expire_on_commit: bool = False,
    **kwargs: Any,
) -> sessionmaker[Session]:
    """Build a sync :class:`~sqlalchemy.orm.sessionmaker` bound to ``engine``."""
    return sessionmaker(
        bind=engine,
        class_=Session,
        autocommit=False,
        autoflush=False,
        expire_on_commit=expire_on_commit,
        **kwargs,
    )


def async_session_factory(
    engine: AsyncEngine,
    *,
    expire_on_commit: bool = False,
    **kwargs: Any,
) -> async_sessionmaker[AsyncSession]:
    """Build an :class:`async_sessionmaker` bound to ``engine``."""
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=expire_on_commit,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Module-level singletons bound to ``get_settings()`` for app-level DI.
#
# Tests should construct their own engines / sessionmakers via the
# factory functions above — this avoids hidden coupling to environment
# state and the async driver's compatibility with the configured DSN.
# ---------------------------------------------------------------------------

settings: Settings = get_settings()
engine: Engine = create_engine(
    settings.database.dsn,
    pool_size=settings.database.pool_size,
    pool_pre_ping=settings.database.pool_pre_ping,
    echo=settings.database.echo,
)
SessionLocal: sessionmaker[Session] = session_factory(engine)


# Async singletons: SQLAlchemy refuses to build an ``AsyncEngine`` against
# a sync driver, and the dev default DSN (``sqlite+pysqlite:///./app.db``)
# is sync. We attempt eager construction and degrade to ``None`` so this
# module can be imported in any environment; production deployments
# should set ``DB_DSN=postgresql+asyncpg://...`` (or similar) and the
# globals below will resolve to real instances.
async_engine: AsyncEngine | None
async_session_maker: async_sessionmaker[AsyncSession] | None
try:
    _async_engine = create_async_engine(
        settings.database.dsn,
        pool_size=settings.database.pool_size,
        pool_pre_ping=settings.database.pool_pre_ping,
        echo=settings.database.echo,
    )
    async_engine = _async_engine
    async_session_maker = async_session_factory(_async_engine)
except Exception:  # noqa: BLE001 — degraded mode is intentional
    async_engine = None
    async_session_maker = None


def get_db() -> Generator[Session]:
    """FastAPI dependency that yields a sync :class:`Session` and closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


async def get_async_db() -> AsyncGenerator[AsyncSession]:
    """FastAPI dependency that yields an :class:`AsyncSession` and closes it.

    Raises :class:`RuntimeError` if the process is configured with a
    sync-only DSN (the dev default). Configure ``DB_DSN`` with an async
    driver (``postgresql+asyncpg://...``) to enable.
    """
    if async_session_maker is None:
        raise RuntimeError(
            "async_session_maker is not configured: the active DB_DSN uses "
            "a sync driver. Set DB_DSN to an async DSN such as "
            "'postgresql+asyncpg://...' or 'sqlite+aiosqlite:///...', or call "
            "'create_async_engine(...)' directly with an async DSN."
        )
    async with async_session_maker() as session:
        yield session


__all__ = [
    "AsyncEngine",
    "AsyncSession",
    "Base",
    "DatabaseSettings",
    "MetaData",
    "SessionLocal",
    "Settings",
    "async_engine",
    "async_session_maker",
    "async_session_factory",
    "create_async_engine",
    "create_engine",
    "engine",
    "get_async_db",
    "get_db",
    "get_settings",
    "session_factory",
    "sessionmaker",
]
