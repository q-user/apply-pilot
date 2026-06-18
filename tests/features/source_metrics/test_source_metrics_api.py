"""HTTP tests for the ``GET /admin/sources/metrics`` endpoint.

The endpoint is mounted at ``/admin/sources/metrics`` and returns a
list of :class:`SourceMetricRead` DTOs for the requested source.
The repository is wired through FastAPI ``dependency_overrides`` so
the tests inject the in-memory fake and exercise the full request
lifecycle without touching the real database.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apply_pilot.app import create_app
from apply_pilot.config import get_admin_auth_required
from apply_pilot.features.source_metrics.api import get_source_metric_repository
from apply_pilot.features.source_metrics.models import (
    SourceMetricEvent,
    SourceMetricEventKind,
)
from apply_pilot.features.source_metrics.repository import (
    InMemorySourceMetricRepository,
)


@pytest.fixture
def repo() -> Iterator[InMemorySourceMetricRepository]:
    """Fresh in-memory repository per test."""
    yield InMemorySourceMetricRepository()


@pytest.fixture
def app(repo: InMemorySourceMetricRepository) -> Iterator[FastAPI]:
    """Build a :class:`FastAPI` app with the metrics repository stubbed.

    The real :func:`create_app` factory is used so the slice is wired
    exactly as in production; only the
    :func:`get_source_metric_repository` dependency is overridden.
    The admin auth gate is disabled for this slice's pre-issue-#145
    tests; the auth-required code path is covered by the dedicated
    :mod:`tests.features.admin.test_admin_api` suite.
    """
    application = create_app()
    application.dependency_overrides[get_source_metric_repository] = lambda: repo
    application.dependency_overrides[get_admin_auth_required] = lambda: False
    try:
        yield application
    finally:
        application.dependency_overrides.clear()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """TestClient bound to the per-test FastAPI app."""
    with TestClient(app) as c:
        yield c


def _seed(
    repo: InMemorySourceMetricRepository,
    *,
    source: str = "hh",
    count: int = 1,
    duration_ms: int = 10,
    timestamp: datetime | None = None,
) -> SourceMetricEvent:
    """Persist one FETCH event and return it."""
    event = SourceMetricEvent(
        source_name=source,
        kind=SourceMetricEventKind.FETCH,
        count=count,
        duration_ms=duration_ms,
        timestamp=timestamp or datetime.now(UTC),
    )
    repo.record(event)
    return event


def test_endpoint_returns_200_with_empty_list(client: TestClient) -> None:
    """An empty repository returns ``[]`` with status 200."""
    response = client.get("/admin/sources/metrics", params={"source": "hh"})

    assert response.status_code == 200
    assert response.json() == []


def test_endpoint_returns_seeded_events(
    client: TestClient, repo: InMemorySourceMetricRepository
) -> None:
    """Seeded events appear in the JSON response with the documented fields."""
    base = datetime(2026, 6, 17, 12, 0, 0, tzinfo=UTC)
    _seed(repo, source="hh", count=10, duration_ms=250, timestamp=base)
    _seed(repo, source="hh", count=5, duration_ms=180, timestamp=base + timedelta(minutes=5))

    response = client.get("/admin/sources/metrics", params={"source": "hh"})

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 2
    assert {item["source_name"] for item in body} == {"hh"}
    assert {item["kind"] for item in body} == {"fetch"}
    assert {item["count"] for item in body} == {10, 5}
    assert {item["duration_ms"] for item in body} == {250, 180}


def test_endpoint_filters_by_source(
    client: TestClient, repo: InMemorySourceMetricRepository
) -> None:
    """The ``source`` query parameter filters the response to one source."""
    _seed(repo, source="hh")
    _seed(repo, source="habr")

    response = client.get("/admin/sources/metrics", params={"source": "habr"})

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["source_name"] == "habr"


def test_endpoint_filters_by_since(
    client: TestClient, repo: InMemorySourceMetricRepository
) -> None:
    """The ``since`` query parameter applies a strict lower bound on ``timestamp``."""
    base = datetime(2026, 6, 17, 12, 0, 0, tzinfo=UTC)
    _seed(repo, timestamp=base - timedelta(days=2))
    _seed(repo, timestamp=base - timedelta(hours=12))
    _seed(repo, timestamp=base)

    response = client.get(
        "/admin/sources/metrics",
        params={"source": "hh", "since": (base - timedelta(hours=1)).isoformat()},
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    # The remaining event is the one at ``base``.
    assert body[0]["timestamp"].startswith(base.isoformat()[:10])


def test_endpoint_filters_by_until(
    client: TestClient, repo: InMemorySourceMetricRepository
) -> None:
    """The ``until`` query parameter applies an inclusive upper bound on ``timestamp``."""
    base = datetime(2026, 6, 17, 12, 0, 0, tzinfo=UTC)
    _seed(repo, timestamp=base)
    _seed(repo, timestamp=base + timedelta(days=2))

    response = client.get(
        "/admin/sources/metrics",
        params={"source": "hh", "until": base.isoformat()},
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["timestamp"].startswith(base.isoformat()[:10])


def test_endpoint_requires_source_parameter(client: TestClient) -> None:
    """Omitting ``source`` must be a 422 validation error."""
    response = client.get("/admin/sources/metrics")

    assert response.status_code == 422
