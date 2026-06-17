"""TDD tests for the admin/integrations slice (M6, issue #57).

The slice exposes a read-only ``GET /admin/integrations`` endpoint that
returns the current health of every external integration (hh OAuth, LLM,
database, ...) and a ``POST /admin/integrations/refresh`` endpoint that
manually triggers a one-shot refresh via the ``IntegrationStatusWorker``.

A long-running ``IntegrationStatusWorker`` (a :class:`BaseProcess` subclass)
periodically runs every :class:`IntegrationChecker` and updates the shared
:class:`InMemoryIntegrationStatusStore`.

Conventions
-----------

* Tests use the in-memory store and a fake :class:`IntegrationChecker` for
  the worker tests. The real :class:`HhOAuthChecker` and :class:`LlmChecker`
  tests inject :class:`httpx.MockTransport` so no real network traffic
  is generated. The :class:`DatabaseChecker` test uses a fake ping.
* No ``Mock`` anywhere — every test asserts observable behaviour
  (store contents, API response body, call counts) on real fakes.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine

from job_apply.features.admin.integrations import (
    DatabaseChecker,
    HhOAuthChecker,
    InMemoryIntegrationStatusStore,
    IntegrationStatus,
    IntegrationStatusStore,
    IntegrationStatusWorker,
    LlmChecker,
)
from job_apply.features.hh.oauth import HhHttpOAuthClient
from job_apply.features.scoring.llm import HttpLLMClient, LLMSettings

# ---------------------------------------------------------------------------
# Fake checker used by the worker + API tests
# ---------------------------------------------------------------------------


class _FakeChecker:
    """Programmable :class:`IntegrationChecker` for worker and API tests.

    The fake exposes a :meth:`set_health` hook so each test can change the
    status the next :meth:`check` call returns without re-wiring the
    worker. Call counts are tracked on :attr:`call_count` so tests can
    assert that the worker actually ran every checker the right number
    of times.
    """

    def __init__(
        self,
        name: str,
        *,
        status: str = "healthy",
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.name = name
        self._status = status
        self._error = error
        self._metadata = metadata
        self.call_count = 0

    def set_health(
        self,
        *,
        status: str = "healthy",
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._status = status
        self._error = error
        self._metadata = metadata

    async def check(self) -> IntegrationStatus:
        self.call_count += 1
        return IntegrationStatus(
            name=self.name,
            status=self._status,
            last_checked_at=datetime.now(UTC),
            error=self._error,
            metadata=self._metadata,
        )


# ---------------------------------------------------------------------------
# Pure-data tests
# ---------------------------------------------------------------------------


def test_integration_status_dataclass() -> None:
    """The :class:`IntegrationStatus` dataclass must round-trip every field."""
    last_checked = datetime(2026, 6, 17, 12, 0, 0, tzinfo=UTC)
    status = IntegrationStatus(
        name="hh",
        status="healthy",
        last_checked_at=last_checked,
        error=None,
        metadata={"latency_ms": 42},
    )
    assert status.name == "hh"
    assert status.status == "healthy"
    assert status.last_checked_at == last_checked
    assert status.error is None
    assert status.metadata == {"latency_ms": 42}

    # The dataclass must be immutable — assignment to a field is a TypeError.
    with pytest.raises((AttributeError, TypeError)):
        status.status = "unhealthy"  # type: ignore[misc]


def test_in_memory_store_get_all_returns_empty() -> None:
    """A fresh store must return an empty list from :meth:`get_all`."""
    store = InMemoryIntegrationStatusStore()
    assert store.get_all() == []


def test_in_memory_store_update_persists() -> None:
    """``update(name, status)`` must make the status visible via :meth:`get_all`."""
    store = InMemoryIntegrationStatusStore()
    status = IntegrationStatus(
        name="llm",
        status="degraded",
        last_checked_at=datetime.now(UTC),
        error="timeout",
        metadata=None,
    )
    store.update("llm", status)
    all_statuses = store.get_all()
    assert len(all_statuses) == 1
    assert all_statuses[0].name == "llm"
    assert all_statuses[0].status == "degraded"
    assert all_statuses[0].error == "timeout"

    # A second update for the same name must replace the entry, not append.
    updated = IntegrationStatus(
        name="llm",
        status="healthy",
        last_checked_at=datetime.now(UTC),
        error=None,
        metadata=None,
    )
    store.update("llm", updated)
    all_statuses = store.get_all()
    assert len(all_statuses) == 1
    assert all_statuses[0].status == "healthy"


# ---------------------------------------------------------------------------
# Real-checker tests (httpx.MockTransport)
# ---------------------------------------------------------------------------


def _hh_oauth_handler(status_code: int) -> Callable[[httpx.Request], httpx.Response]:
    """Return a transport handler that always replies with ``status_code``.

    A 200 response returns a valid token payload (the happy path: hh.ru
    accepts our synthetic refresh token). Any other status code returns
    an OAuth error body, which the client maps onto a non-2xx response
    via :class:`OAuthExchangeError`.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        # The hh.ru token endpoint accepts POST; we don't care about the
        # request body, only the response shape.
        if status_code == 200:
            return httpx.Response(
                200,
                json={
                    "access_token": "fake-access-token",
                    "refresh_token": "fake-refresh-token",
                    "expires_in": 3600,
                    "token_type": "bearer",
                },
            )
        return httpx.Response(status_code, json={"error": "invalid_grant"})

    return handler


def test_hh_oauth_checker_returns_status() -> None:
    """The checker must report ``healthy`` for 200 and ``unhealthy`` for 401."""
    # Healthy path: 200 OK.
    healthy_client = HhHttpOAuthClient(
        client_id="cid",
        client_secret="secret",
        redirect_uri="https://example.com/cb",
        transport=httpx.MockTransport(_hh_oauth_handler(200)),
    )
    healthy_checker = HhOAuthChecker(client=healthy_client)
    healthy_status = asyncio.run(healthy_checker.check())
    assert healthy_status.name == "hh"
    assert healthy_status.status == "healthy"
    assert healthy_status.error is None

    # Unhealthy path: 401 (auth failure).
    unhealthy_client = HhHttpOAuthClient(
        client_id="cid",
        client_secret="secret",
        redirect_uri="https://example.com/cb",
        transport=httpx.MockTransport(_hh_oauth_handler(401)),
    )
    unhealthy_checker = HhOAuthChecker(client=unhealthy_client)
    unhealthy_status = asyncio.run(unhealthy_checker.check())
    assert unhealthy_status.name == "hh"
    assert unhealthy_status.status == "unhealthy"
    assert unhealthy_status.error is not None
    assert "401" in unhealthy_status.error


def test_llm_checker_returns_status() -> None:
    """The LLM checker must report ``healthy`` on a valid 2xx response."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        # The checker should send a chat-completions payload — capture
        # the request body so the test verifies the call shape.
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}]},
        )

    client = HttpLLMClient(
        LLMSettings(api_key="test-key", base_url="https://llm.example.com/v1", model="m"),
        transport=httpx.MockTransport(handler),
    )
    checker = LlmChecker(client=client)
    status = asyncio.run(checker.check())
    assert status.name == "llm"
    assert status.status == "healthy"
    assert status.error is None
    # The checker used the configured base URL and a chat-completions body.
    assert captured["url"].endswith("/chat/completions")
    assert captured["body"]["model"] == "m"


def test_database_checker_returns_status() -> None:
    """A live sqlite engine must report ``healthy``; an unreachable one ``unhealthy``."""
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
    # connect attempt fails fast with an :class:`OperationalError`
    # (a :class:`SQLAlchemyError` subclass) — no network and no
    # ``psycopg`` driver required.
    bad_engine = create_engine(
        "sqlite:///nonexistent_dir_for_health_check/nonexistent.db",
        future=True,
    )
    bad_checker = DatabaseChecker(engine=bad_engine)
    unhealthy_status = asyncio.run(bad_checker.check())
    assert unhealthy_status.name == "database"
    assert unhealthy_status.status == "unhealthy"
    assert unhealthy_status.error is not None


# ---------------------------------------------------------------------------
# Worker tests
# ---------------------------------------------------------------------------


def test_worker_run_once_calls_all_checkers() -> None:
    """``run_once`` must invoke every checker exactly once and update the store."""
    store: IntegrationStatusStore = InMemoryIntegrationStatusStore()
    hh = _FakeChecker("hh", status="healthy")
    llm = _FakeChecker("llm", status="degraded", error="slow")
    worker = IntegrationStatusWorker(
        store=store,
        checkers=[hh, llm],
        refresh_interval_seconds=60.0,
    )
    asyncio.run(worker.run_once())

    assert hh.call_count == 1
    assert llm.call_count == 1

    statuses = {s.name: s for s in store.get_all()}
    assert statuses["hh"].status == "healthy"
    assert statuses["llm"].status == "degraded"
    assert statuses["llm"].error == "slow"


def test_worker_run_loops_with_interval() -> None:
    """``run`` must call every checker repeatedly until shutdown."""
    store: IntegrationStatusStore = InMemoryIntegrationStatusStore()
    hh = _FakeChecker("hh")
    llm = _FakeChecker("llm")
    worker = IntegrationStatusWorker(
        store=store,
        checkers=[hh, llm],
        refresh_interval_seconds=0.05,
        name="integration-status-test",
    )

    async def drive() -> None:
        task = asyncio.create_task(worker.run())
        # Give the worker time to run a few iterations.
        await asyncio.sleep(0.2)
        worker.request_shutdown()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(drive())

    # The exact count is timing-dependent; we just need "more than one"
    # to prove the loop is running.
    assert hh.call_count >= 2
    assert llm.call_count >= 2


def test_worker_handles_graceful_shutdown() -> None:
    """The worker must exit cleanly when ``request_shutdown`` is called."""
    store: IntegrationStatusStore = InMemoryIntegrationStatusStore()
    checker = _FakeChecker("hh")
    worker = IntegrationStatusWorker(
        store=store,
        checkers=[checker],
        refresh_interval_seconds=0.01,
        name="integration-status-shutdown",
    )

    async def drive() -> int:
        task = asyncio.create_task(worker.run())
        # Let one iteration finish, then shut down.
        await asyncio.sleep(0.05)
        worker.request_shutdown()
        return await asyncio.wait_for(task, timeout=2.0)

    exit_code = asyncio.run(drive())
    assert exit_code == 0
    assert checker.call_count >= 1
    # The store must be populated with a final healthy status from the
    # last iteration before shutdown.
    statuses = {s.name: s for s in store.get_all()}
    assert statuses["hh"].status == "healthy"


# ---------------------------------------------------------------------------
# API tests
# ---------------------------------------------------------------------------


@pytest.fixture
def hh_oauth_checker() -> HhOAuthChecker:
    """A :class:`HhOAuthChecker` backed by an httpx mock that returns 200."""
    return HhOAuthChecker(
        client=HhHttpOAuthClient(
            client_id="cid",
            client_secret="secret",
            redirect_uri="https://example.com/cb",
            transport=httpx.MockTransport(_hh_oauth_handler(200)),
        )
    )


@pytest.fixture
def llm_checker() -> LlmChecker:
    """A :class:`LlmChecker` backed by an httpx mock that returns 200."""
    return LlmChecker(
        client=HttpLLMClient(
            LLMSettings(api_key="test-key", base_url="https://llm.example.com/v1", model="m"),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, json={"choices": [{"message": {"content": "ok"}}]}
                )
            ),
        )
    )


@pytest.fixture
def store() -> InMemoryIntegrationStatusStore:
    return InMemoryIntegrationStatusStore()


@pytest.fixture
def app(
    store: InMemoryIntegrationStatusStore,
    hh_oauth_checker: HhOAuthChecker,
    llm_checker: LlmChecker,
) -> Iterator[FastAPI]:
    """A minimal FastAPI app with the admin router and overridden DI."""
    from job_apply.features.admin.api import (
        get_integration_status_store,
        get_integration_status_worker,
        router,
    )

    worker = IntegrationStatusWorker(
        store=store,
        checkers=[hh_oauth_checker, llm_checker],
        refresh_interval_seconds=60.0,
        name="integration-status-api",
    )

    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[get_integration_status_store] = lambda: store
    application.dependency_overrides[get_integration_status_worker] = lambda: worker
    try:
        yield application
    finally:
        application.dependency_overrides.clear()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def test_api_integration_status_endpoint(
    client: TestClient,
    store: InMemoryIntegrationStatusStore,
) -> None:
    """``GET /admin/integrations`` must return the current store contents."""
    # Pre-seed the store so the endpoint has something to return.
    store.update(
        "hh",
        IntegrationStatus(
            name="hh",
            status="healthy",
            last_checked_at=datetime.now(UTC),
            error=None,
            metadata=None,
        ),
    )
    store.update(
        "llm",
        IntegrationStatus(
            name="llm",
            status="unhealthy",
            last_checked_at=datetime.now(UTC),
            error="500 from upstream",
            metadata=None,
        ),
    )

    response = client.get("/admin/integrations")
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    assert len(body) == 2
    by_name = {item["name"]: item for item in body}
    assert by_name["hh"]["status"] == "healthy"
    assert by_name["llm"]["status"] == "unhealthy"
    assert by_name["llm"]["error"] == "500 from upstream"


def test_api_refresh_endpoint(
    client: TestClient,
    store: InMemoryIntegrationStatusStore,
    hh_oauth_checker: HhOAuthChecker,
    llm_checker: LlmChecker,
) -> None:
    """``POST /admin/integrations/refresh`` must run every checker once and return them."""
    response = client.post("/admin/integrations/refresh")
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    by_name = {item["name"]: item for item in body}
    assert "hh" in by_name
    assert "llm" in by_name
    # The checkers return healthy in this fixture.
    assert by_name["hh"]["status"] == "healthy"
    assert by_name["llm"]["status"] == "healthy"

    # The store must reflect the same data the endpoint returned.
    stored = {s.name: s for s in store.get_all()}
    assert stored["hh"].status == "healthy"
    assert stored["llm"].status == "healthy"
