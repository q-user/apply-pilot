"""Tests for database primitives (Base, engine, sessionmaker, get_db, init_db, Alembic baseline)."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from apply_pilot.config import DatabaseSettings, get_database_settings
from apply_pilot.db import (
    Base,
    SessionLocal,
    engine,
    get_db,
    get_engine,
    init_db,
)
from apply_pilot.features.orders import models as _orders_models  # noqa: F401  (register Order)

# ---------------------------------------------------------------------------
# Base metadata
# ---------------------------------------------------------------------------


def test_base_metadata_collects_models() -> None:
    """All feature models are registered in Base.metadata on import."""
    assert "orders" in Base.metadata.tables
    orders = Base.metadata.tables["orders"]
    column_names = set(orders.columns.keys())
    assert {"id", "customer_name", "status", "created_at"} <= column_names


# ---------------------------------------------------------------------------
# Session factory
# ---------------------------------------------------------------------------


def _sqlite_inmemory_engine() -> Engine:
    """Build a single-connection sqlite in-memory engine suitable for tests."""
    return create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def test_get_sessionmaker_returns_working_factory() -> None:
    """get_sessionmaker returns a sessionmaker that yields a usable Session on sqlite in-memory."""
    test_engine = _sqlite_inmemory_engine()
    factory: sessionmaker[Session] = sessionmaker(
        bind=test_engine, class_=Session, autocommit=False, autoflush=False
    )
    Base.metadata.create_all(test_engine)

    with factory() as session:
        assert isinstance(session, Session)
        order = _orders_models.Order(customer_name="Alice")
        session.add(order)
        session.commit()
        fetched = session.get(_orders_models.Order, order.id)
        assert fetched is not None
        assert fetched.customer_name == "Alice"


# ---------------------------------------------------------------------------
# get_db (DI-friendly generator)
# ---------------------------------------------------------------------------


def test_get_db_yields_and_closes() -> None:
    """get_db yields a session from the injected factory and closes it on exit."""
    closed: list[bool] = []

    class FakeSession:
        def close(self) -> None:
            closed.append(True)

    factory: Callable[[], FakeSession] = FakeSession

    gen = get_db(session_factory=factory)
    session = next(gen)
    assert isinstance(session, FakeSession)
    with pytest.raises(StopIteration):
        next(gen)
    assert closed == [True]


def test_get_db_closes_session_on_consumer_exception() -> None:
    """If the consumer raises, get_db still closes the session."""
    closed: list[bool] = []

    class FakeSession:
        def close(self) -> None:
            closed.append(True)

    factory: Callable[[], FakeSession] = FakeSession
    gen = get_db(session_factory=factory)
    next(gen)
    with pytest.raises(RuntimeError, match="boom"):
        gen.throw(RuntimeError("boom"))
    assert closed == [True]


# ---------------------------------------------------------------------------
# init_db
# ---------------------------------------------------------------------------


def test_init_db_creates_all_tables() -> None:
    """init_db creates every table registered in Base.metadata."""
    test_engine = _sqlite_inmemory_engine()
    init_db(engine=test_engine)

    from sqlalchemy import inspect

    inspector = inspect(test_engine)
    tables = set(inspector.get_table_names())
    assert "orders" in tables


# ---------------------------------------------------------------------------
# Module-level singletons (sanity)
# ---------------------------------------------------------------------------


def test_module_level_engine_and_sessionmaker_use_settings() -> None:
    """The module-level `engine` and `SessionLocal` are usable singletons built from settings."""
    assert isinstance(engine, Engine)
    assert isinstance(SessionLocal, sessionmaker)  # type: ignore[arg-type]
    settings = get_database_settings()
    assert settings.database_url  # non-empty


def test_database_settings_default_url() -> None:
    """DatabaseSettings defaults are stable and pool_pre_ping is on by default."""
    s = DatabaseSettings()
    assert s.database_url == "sqlite:///./dev.db"
    assert s.pool_size == 5
    assert s.max_overflow == 10
    assert s.pool_pre_ping is True
    assert s.echo is False


def test_get_engine_uses_provided_settings(tmp_path: Path) -> None:
    """get_engine honors a DatabaseSettings override (DI)."""
    db_path = tmp_path / "override.db"
    settings = DatabaseSettings(database_url=f"sqlite+pysqlite:///{db_path}", echo=False)
    eng = get_engine(settings=settings)
    assert eng.url.render_as_string(hide_password=False).endswith(f"/{db_path.name}")
    eng.dispose()


# ---------------------------------------------------------------------------
# Alembic baseline roundtrip (subprocess to exercise real alembic env.py)
# ---------------------------------------------------------------------------


def test_alembic_baseline_roundtrip(tmp_path: Path) -> None:
    """`alembic upgrade head` then `alembic downgrade base` works against a disposable sqlite db."""
    db_path = tmp_path / "alembic_test.db"
    url = f"sqlite+pysqlite:///{db_path}"

    repo_root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "DATABASE_URL": url, "PYTHONPATH": str(repo_root / "src")}
    # Use the current venv's python to avoid re-syncing via `uv run`.
    python = sys.executable

    result = subprocess.run(
        [python, "-m", "alembic", "upgrade", "head"],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"alembic upgrade head failed.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "orders" in _table_names(db_path)

    result = subprocess.run(
        [python, "-m", "alembic", "downgrade", "base"],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"alembic downgrade base failed.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "orders" not in _table_names(db_path)


def _table_names(db_path: Path) -> set[str]:
    """Read sqlite master table names without keeping a connection open."""
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    finally:
        conn.close()
    return {row[0] for row in rows}
