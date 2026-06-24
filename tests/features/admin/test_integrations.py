"""TDD tests for the admin/integrations slice (M6, issue #57).

The slice exposes a read-only ``GET /admin/integrations`` endpoint that
returns the current health of every external integration (LLM, database,
...) and a ``POST /admin/integrations/refresh`` endpoint that manually
triggers a one-shot refresh via the ``IntegrationStatusWorker``.

A long-running ``IntegrationStatusWorker`` (a :class:`BaseProcess` subclass)
periodically runs every :class:`IntegrationChecker` and updates the shared
:class:`InMemoryIntegrationStatusStore`.

Conventions
-----------

* Tests use the in-memory store and a fake :class:`IntegrationChecker` for
  the worker tests. The real :class:`LlmChecker` tests inject
  :class:`httpx.MockTransport` so no real network traffic is generated.
  The :class:`DatabaseChecker` test uses a fake ping.
* No ``Mock`` anywhere — every test asserts observable behaviour
  (store contents, API response body, call counts) on real fakes.

Note (M10): the hh.ru OAuth checker has been removed. Apply is delegated
to a separate headless-browser tool (see issue #206).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine

from apply_pilot.features.admin.integrations import (
    DatabaseChecker,
    InMemoryIntegrationStatusStore,
    IntegrationStatus,
    IntegrationStatusWorker,
    LlmChecker,
)
from apply_pilot.features.scoring.llm import HttpLLMClient, LLMSettings

# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


def _llm_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})


class _CountingChecker:
    """A trivial :class:`IntegrationChecker` whose ``check`` increments a counter.

    Used to assert the worker calls every registered checker once per
    iteration. The status returned is :data:`STATUS_HEALTHY` so the
    store accumulates predictable values.
    """

    def __init__(self, name: str) -> None:
        self._name = name
        self.call_count = 0

    @property
    def name(self) -> str:
        return self._name

    async def check(self) -> IntegrationStatus:
        self.call_count += 1
        return IntegrationStatus(
            name=self._name,
            status="healthy",
            last_checked_at=datetime.now(UTC),
            error=None,
            metadata={"call_count": self.call_count},
        )


# ---------------------------------------------------------------------------
# Value object + in-memory store
# ---------------------------------------------------------------------------


def test_integration_status_dataclass() -> None:
    """IntegrationStatus should be a frozen, kw-only-friendly value object."""
    ts = datetime.now(UTC)
    status = IntegrationStatus(
        name="llm",
        status="healthy",
        last_checked_at=ts,
        error=None,
        metadata={"latency_ms": 42},
    )
    assert status.name == "llm"
    assert status.status == "healthy"
    assert status.last_checked_at == ts
    assert status.error is None
    assert status.metadata == {"latency_ms": 42}


def test_in_memory_store_get_all_returns_empty() -> None:
    """Empty store returns an empty list (not None)."""
    store = InMemoryIntegrationStatusStore()
    assert store.get_all() == []


def test_in_memory_store_update_persists() -> None:
    """Updating the store replaces any prior entry under the same name."""
    store = InMemoryIntegrationStatusStore()
    ts = datetime.now(UTC)
    first = IntegrationStatus(
        name="llm",
        status="healthy",
        last_checked_at=ts,
        error=None,
        metadata=None,
    )
    store.update("llm", first)
    assert store.get_all() == [first]

    second = IntegrationStatus(
        name="llm",
        status="degraded",
        last_checked_at=ts,
        error="slow",
        metadata=None,
    )
    store.update("llm", second)
    assert store.get_all() == [second]


def test_in_memory_store_get_all_is_sorted_by_name() -> None:
    """The store returns statuses sorted by name for stable API output."""
    store = InMemoryIntegrationStatusStore()
    ts = datetime.now(UTC)
    for n in ("z", "a", "m"):
        store.update(
            n,
            IntegrationStatus(
                name=n,
                status="healthy",
                last_checked_at=ts,
                error=None,
                metadata=None,
            ),
        )
    assert [s.name for s in store.get_all()] == ["a", "m", "z"]


# ---------------------------------------------------------------------------
# LlmChecker
# ---------------------------------------------------------------------------


def test_llm_checker_returns_status() -> None:
    """A 2xx response with non-empty content is healthy."""
    settings = LLMSettings(api_key="test", base_url="https://llm.example.com/v1", model="m")
    client = HttpLLMClient(settings, transport=httpx.MockTransport(_llm_handler))
    checker = LlmChecker(client=client)

    status = asyncio.run(checker.check())
    assert status.name == "llm"
    assert status.status == "healthy"
    assert status.error is None
    assert status.metadata is not None
    assert "latency_ms" in status.metadata
    assert status.metadata["response_chars"] == len("ok")


def test_llm_checker_reports_degraded_on_empty_content() -> None:
    """A 2xx response with empty content is degraded, not healthy."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})

    settings = LLMSettings(api_key="test", base_url="https://llm.example.com/v1", model="m")
    client = HttpLLMClient(settings, transport=httpx.MockTransport(handler))
    checker = LlmChecker(client=client)

    status = asyncio.run(checker.check())
    assert status.status == "degraded"
    assert status.error is not None and "empty" in status.error


def test_llm_checker_reports_unhealthy_on_5xx() -> None:
    """A 5xx response is unhealthy and surfaces the status code."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "unavailable"})

    settings = LLMSettings(api_key="test", base_url="https://llm.example.com/v1", model="m")
    client = HttpLLMClient(settings, transport=httpx.MockTransport(handler))
    checker = LlmChecker(client=client)

    status = asyncio.run(checker.check())
    assert status.status == "unhealthy"
    assert status.error is not None
    assert "503" in status.error


# ---------------------------------------------------------------------------
# DatabaseChecker
# ---------------------------------------------------------------------------


def test_database_checker_returns_status() -> None:
    """A live sqlite engine is ``healthy``; an unreachable one is ``unhealthy``."""
    engine = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    checker = DatabaseChecker(engine=engine)
    status = asyncio.run(checker.check())
    assert status.name == "database"
    assert status.status == "healthy"
    assert status.error is None

    # An engine that cannot be reached must report ``unhealthy``. We
    # point at a sqlite path inside a non-existent directory so the
    # connect attempt fails fast with an :class:`OperationalError`.
    bad_engine = create_engine(
        "sqlite:///nonexistent_dir_for_health_check/nonexistent.db",
        future=True,
    )
    bad_checker = DatabaseChecker(engine=bad_engine)
    bad_status = asyncio.run(bad_checker.check())
    assert bad_status.name == "database"
    assert bad_status.status == "unhealthy"
    assert bad_status.error is not None


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


def test_worker_run_once_calls_all_checkers() -> None:
    """``run_once`` calls every registered checker once and writes to the store."""
    store = InMemoryIntegrationStatusStore()
    counters = [_CountingChecker("a"), _CountingChecker("b"), _CountingChecker("c")]
    worker = IntegrationStatusWorker(
        store=store,
        checkers=counters,
        refresh_interval_seconds=60.0,
        name="test-worker-once",
    )

    results = asyncio.run(worker.run_once())
    assert [c.call_count for c in counters] == [1, 1, 1]
    assert {r.name for r in results} == {"a", "b", "c"}
    assert {s.name for s in store.get_all()} == {"a", "b", "c"}


def test_worker_run_once_isolates_failing_checkers() -> None:
    """A checker that raises is recorded as unhealthy; the loop survives."""

    class _BoomChecker:
        name = "boom"

        async def check(self) -> IntegrationStatus:
            raise RuntimeError("nope")

    store = InMemoryIntegrationStatusStore()
    healthy = _CountingChecker("ok")
    worker = IntegrationStatusWorker(
        store=store,
        checkers=[_BoomChecker(), healthy],
        refresh_interval_seconds=60.0,
        name="test-worker-boom",
    )

    results = asyncio.run(worker.run_once())
    assert healthy.call_count == 1
    statuses = {s.name: s for s in results}
    assert statuses["boom"].status == "unhealthy"
    assert "nope" in (statuses["boom"].error or "")
    assert statuses["ok"].status == "healthy"


def test_worker_run_loops_with_interval() -> None:
    """``run`` calls ``run_once`` repeatedly until shutdown is requested."""
    store = InMemoryIntegrationStatusStore()
    counter = _CountingChecker("loop")
    worker = IntegrationStatusWorker(
        store=store,
        checkers=[counter],
        refresh_interval_seconds=0.01,
        name="test-worker-loop",
    )

    async def _driver() -> None:
        # Stop the worker after at most 2 iterations so the test is fast.
        async def _stop_soon() -> None:
            await asyncio.sleep(0.05)
            worker.request_shutdown()

        await asyncio.gather(worker.run(), _stop_soon())

    asyncio.run(_driver())
    assert counter.call_count >= 2


def test_worker_handles_graceful_shutdown() -> None:
    """``request_shutdown`` makes :meth:`run` return 0 on the next tick."""
    store = InMemoryIntegrationStatusStore()
    counter = _CountingChecker("shutdown")
    worker = IntegrationStatusWorker(
        store=store,
        checkers=[counter],
        refresh_interval_seconds=0.01,
        name="test-worker-shutdown",
    )

    async def _driver() -> int:
        async def _stop_now() -> None:
            await asyncio.sleep(0.02)
            worker.request_shutdown()

        await asyncio.gather(worker.run(), _stop_now())
        return counter.call_count

    calls = asyncio.run(_driver())
    assert calls >= 1


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------


@pytest.fixture
def llm_checker() -> LlmChecker:
    """A :class:`LlmChecker` backed by an httpx mock that returns 200."""
    settings = LLMSettings(api_key="test-key", base_url="https://llm.example.com/v1", model="m")
    client = HttpLLMClient(settings, transport=httpx.MockTransport(_llm_handler))
    return LlmChecker(client=client)


@pytest.fixture
def integration_worker(
    integration_store: InMemoryIntegrationStatusStore,
    llm_checker: LlmChecker,
) -> IntegrationStatusWorker:
    """Worker under test, with a single :class:`LlmChecker`."""
    return IntegrationStatusWorker(
        store=integration_store,
        checkers=[llm_checker],
        refresh_interval_seconds=60.0,
        name="test-worker",
    )


@pytest.fixture
def integration_store() -> InMemoryIntegrationStatusStore:
    return InMemoryIntegrationStatusStore()


@pytest.fixture
def admin_app(
    integration_store: InMemoryIntegrationStatusStore,
) -> Iterator[FastAPI]:
    """Build a minimal FastAPI app mounting the admin router for endpoint tests.

    The auth gate is bypassed by overriding the
    :func:`require_admin_user` dependency on the FastAPI app (the
    M6/M8 admin router uses it as a drop-in). We could also override
    :func:`get_admin_auth_required` via ``dependency_overrides``, but
    since the M6 fixture exists in isolation (the slice has its own
    test_db / no other routers), we just install a no-op admin user
    resolver.
    """
    from apply_pilot.features.admin import router as admin_router
    from apply_pilot.features.admin._auth import require_admin_user
    from apply_pilot.features.admin.api import (
        get_integration_status_store,
        get_integration_status_worker,
    )

    app = FastAPI()
    app.include_router(admin_router)
    app.state.integration_store = integration_store
    app.dependency_overrides[get_integration_status_store] = lambda: integration_store
    app.dependency_overrides[get_integration_status_worker] = lambda: None
    app.dependency_overrides[require_admin_user] = lambda: "anonymous"
    yield app
    app.dependency_overrides.clear()


def test_api_integration_status_endpoint(
    admin_app: FastAPI,
    integration_store: InMemoryIntegrationStatusStore,
) -> None:
    """``GET /admin/integrations`` returns the current store contents."""
    ts = datetime.now(UTC)
    integration_store.update(
        "llm",
        IntegrationStatus(
            name="llm",
            status="healthy",
            last_checked_at=ts,
            error=None,
            metadata={"latency_ms": 12},
        ),
    )

    with TestClient(admin_app) as client:
        response = client.get("/admin/integrations")
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, list)
    names = {entry["name"] for entry in body}
    assert names == {"llm"}
    llm_entry = next(e for e in body if e["name"] == "llm")
    assert llm_entry["status"] == "healthy"
    assert llm_entry["last_checked_at"].startswith(ts.strftime("%Y-%m-%d"))


def test_api_refresh_endpoint(
    admin_app: FastAPI,
    integration_store: InMemoryIntegrationStatusStore,
    integration_worker: IntegrationStatusWorker,
) -> None:
    """``POST /admin/integrations/refresh`` runs every checker once."""
    from apply_pilot.features.admin.api import get_integration_status_worker

    admin_app.dependency_overrides[get_integration_status_worker] = lambda: integration_worker

    with TestClient(admin_app) as client:
        response = client.post("/admin/integrations/refresh")
    assert response.status_code == 200, response.text
    body = response.json()
    statuses = {entry["name"]: entry for entry in body}
    assert "llm" in statuses
    assert statuses["llm"]["status"] == "healthy"
    # The worker wrote through to the shared store too.
    assert {s.name for s in integration_store.get_all()} == {"llm"}


def test_api_integration_status_endpoint_empty(
    admin_app: FastAPI,
) -> None:
    """Empty store yields a 200 with an empty list."""
    with TestClient(admin_app) as client:
        response = client.get("/admin/integrations")
    assert response.status_code == 200
    assert response.json() == []
