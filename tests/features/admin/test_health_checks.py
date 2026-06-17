"""Unit tests for the health probe classes (M6, issue #56).

The :mod:`apply_pilot.features.admin.health` module owns four concrete
probes:

* :class:`DatabaseHealthCheck` — ``SELECT 1`` via SQLAlchemy
* :class:`RedisHealthCheck` — ``PING`` via the async Redis client
* :class:`LlmHealthCheck` — ``APP_LLM_API_KEY`` presence in env
* :class:`MigrationsHealthCheck` — ``SELECT version_num`` from
  ``alembic_version``

These tests exercise the probes in isolation — no FastAPI app, no
real network. The fakes are intentionally tiny so a regression
shows up as a single failure with a clear cause.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from sqlalchemy import StaticPool, create_engine, text
from sqlalchemy.engine import Engine

from apply_pilot.features.admin.health import (
    DatabaseHealthCheck,
    HealthStatus,
    LlmHealthCheck,
    MigrationsHealthCheck,
    RedisHealthCheck,
)

# ---------------------------------------------------------------------------
# DatabaseHealthCheck
# ---------------------------------------------------------------------------


def _in_memory_engine() -> Engine:
    """Build a sqlite in-memory engine that supports ``SELECT 1``."""
    return create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


@pytest.mark.asyncio
async def test_database_health_check_healthy_when_select_one_succeeds() -> None:
    """A working engine must return ``healthy`` with a ``ping ok`` detail."""
    eng = _in_memory_engine()
    check = DatabaseHealthCheck(engine=eng)

    result = await check.run()

    assert result.name == "database"
    assert result.status is HealthStatus.HEALTHY
    assert result.detail == "ping ok"


@pytest.mark.asyncio
async def test_database_health_check_unhealthy_when_engine_raises() -> None:
    """An engine whose ``connect`` raises must surface as ``unhealthy``."""

    class _BrokenEngine:
        def connect(self) -> None:
            raise RuntimeError("simulated engine failure")

    check = DatabaseHealthCheck(engine=_BrokenEngine())  # type: ignore[arg-type]
    result = await check.run()

    assert result.name == "database"
    assert result.status is HealthStatus.UNHEALTHY
    assert "simulated engine failure" in result.detail


# ---------------------------------------------------------------------------
# MigrationsHealthCheck
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_migrations_health_check_healthy_with_alembic_version_row() -> None:
    """When the ``alembic_version`` table has a row, status is ``healthy``."""
    eng = _in_memory_engine()
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        conn.execute(text("INSERT INTO alembic_version (version_num) VALUES ('head_rev_001')"))

    check = MigrationsHealthCheck(engine=eng)
    result = await check.run()

    assert result.status is HealthStatus.HEALTHY
    assert result.detail == "head=head_rev_001"


@pytest.mark.asyncio
async def test_migrations_health_check_unknown_when_table_missing() -> None:
    """When the ``alembic_version`` table does not exist, status is ``unknown``."""
    eng = _in_memory_engine()
    check = MigrationsHealthCheck(engine=eng)

    result = await check.run()

    assert result.status is HealthStatus.UNKNOWN
    assert "alembic_version" in result.detail


# ---------------------------------------------------------------------------
# LlmHealthCheck
# ---------------------------------------------------------------------------


@contextmanager
def _env(key: str, value: str | None) -> Iterator[None]:
    """Set/unset *key* in ``os.environ`` for the duration of the block."""
    sentinel = object()
    old = os.environ.get(key, sentinel)  # type: ignore[var-annotated]
    if value is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = value
    try:
        yield
    finally:
        if old is sentinel:
            os.environ.pop(key, None)
        else:
            os.environ[key] = old  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_llm_health_check_healthy_when_api_key_set() -> None:
    """``APP_LLM_API_KEY`` set -> ``healthy`` with the model name in the detail."""
    with _env("APP_LLM_API_KEY", "sk-test"):
        check = LlmHealthCheck()
        result = await check.run()

    assert result.name == "llm"
    assert result.status is HealthStatus.HEALTHY
    assert "model=" in result.detail


@pytest.mark.asyncio
async def test_llm_health_check_unhealthy_when_api_key_missing() -> None:
    """``APP_LLM_API_KEY`` unset -> ``unhealthy`` with an explicit message."""
    with _env("APP_LLM_API_KEY", None):
        check = LlmHealthCheck()
        result = await check.run()

    assert result.name == "llm"
    assert result.status is HealthStatus.UNHEALTHY
    assert "APP_LLM_API_KEY" in result.detail


# ---------------------------------------------------------------------------
# RedisHealthCheck
# ---------------------------------------------------------------------------
#
# The real probe would need a running Redis server. To stay in-process
# we exercise the failure path with an obviously-invalid URL; the
# happy path is covered by the API tests with the dependency override.


@pytest.mark.asyncio
async def test_redis_health_check_unhealthy_when_unreachable() -> None:
    """An unreachable Redis must surface as ``unhealthy`` with the error detail."""
    from apply_pilot.config import Settings

    bad_settings = Settings(
        database_url="sqlite:///:memory:",
        redis_url="redis://127.0.0.1:1",  # nothing listens on port 1
        redis_db=0,
        redis_password=None,
    )
    check = RedisHealthCheck(settings=bad_settings)
    result = await check.run()

    assert result.name == "redis"
    assert result.status is HealthStatus.UNHEALTHY
    assert "ping failed" in result.detail
