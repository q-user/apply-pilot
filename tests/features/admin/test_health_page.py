"""HTTP tests for the admin health page (M6, issue #56).

The route is mounted at ``GET /admin/health`` and renders a thin HTML
view of four system health facts:

* database reachable (ping / ``SELECT 1``)
* redis reachable (ping)
* LLM provider configured (env presence)
* current Alembic head (read from ``alembic_version``)

The health probes are wired through FastAPI ``dependency_overrides``
so the tests can substitute fakes and exercise the full request
lifecycle without touching real infrastructure. The page is allowed
to be naive: each fact renders as a ``healthy / unhealthy / unknown``
label with the underlying error (if any) next to it.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from job_apply.app import create_app
from job_apply.features.admin.health import (
    HealthCheck,
    HealthCheckResult,
    HealthStatus,
    get_health_checks,
)

# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


def _ok(name: str, *, detail: str = "ok") -> HealthCheckResult:
    """Build a healthy :class:`HealthCheckResult` for *name*."""
    return HealthCheckResult(
        name=name,
        status=HealthStatus.HEALTHY,
        detail=detail,
    )


def _down(name: str, *, detail: str = "boom") -> HealthCheckResult:
    """Build an unhealthy :class:`HealthCheckResult` for *name*."""
    return HealthCheckResult(
        name=name,
        status=HealthStatus.UNHEALTHY,
        detail=detail,
    )


class _FakeHealthChecks:
    """In-memory :class:`HealthChecks` fake used by the API tests.

    The four probe results are stored as plain attributes so each test
    can mutate them individually without rebuilding the object. The
    `as_dependencies` helper returns a list of pairs matching the
    :func:`get_health_checks` dependency contract.
    """

    def __init__(
        self,
        *,
        database: HealthCheckResult | None = None,
        redis: HealthCheckResult | None = None,
        llm: HealthCheckResult | None = None,
        migrations: HealthCheckResult | None = None,
    ) -> None:
        self.database = database or _ok("database", detail="ok")
        self.redis = redis or _ok("redis", detail="ok")
        self.llm = llm or _ok("llm", detail="ok")
        self.migrations = migrations or _ok("migrations", detail="head=baseline")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_checks() -> _FakeHealthChecks:
    """Return a default-healthy :class:`_FakeHealthChecks`."""
    return _FakeHealthChecks()


@pytest.fixture
def app(fake_checks: _FakeHealthChecks) -> Iterator[FastAPI]:
    """Build a :class:`FastAPI` app with the admin health probes stubbed.

    We use the real :func:`create_app` factory so the slice is wired
    exactly as in production, then override the
    :func:`get_health_checks` dependency to return the fake list.
    """
    application = create_app()

    def _override_checks() -> list[HealthCheck]:
        return [
            _StubHealthCheck("database", fake_checks.database),
            _StubHealthCheck("redis", fake_checks.redis),
            _StubHealthCheck("llm", fake_checks.llm),
            _StubHealthCheck("migrations", fake_checks.migrations),
        ]

    application.dependency_overrides[get_health_checks] = _override_checks
    try:
        yield application
    finally:
        application.dependency_overrides.clear()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Inline stub type — kept here so the test file is the source of truth.
# ---------------------------------------------------------------------------


class _StubHealthCheck:
    """Minimal :class:`HealthCheck` stub for tests.

    Returns a pre-baked :class:`HealthCheckResult` from :meth:`run`.
    The slice's :class:`HealthCheck` is a Protocol; the stub satisfies
    it structurally without importing internal helpers.
    """

    def __init__(self, name: str, result: HealthCheckResult) -> None:
        self._name = name
        self._result = result

    @property
    def name(self) -> str:
        return self._name

    async def run(self) -> HealthCheckResult:
        return self._result


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_admin_health_page_returns_200_html(client: TestClient) -> None:
    """``GET /admin/health`` must return 200 with a ``text/html`` body."""
    response = client.get("/admin/health")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


def test_admin_health_page_lists_every_probe(client: TestClient) -> None:
    """All four probe names must appear as headings on the page."""
    response = client.get("/admin/health")
    body = response.text

    for probe in ("Database", "Redis", "LLM", "Migrations"):
        assert probe in body, f"probe {probe!r} missing from /admin/health body"


def test_admin_health_page_renders_healthy_label(
    client: TestClient, fake_checks: _FakeHealthChecks
) -> None:
    """A healthy probe renders its status label and detail."""
    fake_checks.llm = _ok("llm", detail="model=gpt-4o-mini")
    response = client.get("/admin/health")
    body = response.text

    assert "healthy" in body.lower()
    assert "model=gpt-4o-mini" in body


def test_admin_health_page_renders_unhealthy_label(
    client: TestClient, fake_checks: _FakeHealthChecks
) -> None:
    """An unhealthy probe shows the status and the error detail."""
    fake_checks.redis = _down("redis", detail="connection refused")
    response = client.get("/admin/health")
    body = response.text.lower()

    assert "unhealthy" in body
    assert "connection refused" in body


def test_admin_health_page_does_not_500_when_a_probe_fails(
    client: TestClient, fake_checks: _FakeHealthChecks
) -> None:
    """A failing probe must not take the page down; the page still serves 200."""
    fake_checks.database = _down("database", detail="engine is closed")
    response = client.get("/admin/health")

    assert response.status_code == 200
    assert "engine is closed" in response.text


def test_admin_health_page_contains_link_back_to_landing(
    client: TestClient,
) -> None:
    """The page should offer a way back to the landing page (``/``)."""
    response = client.get("/admin/health")
    assert 'href="/"' in response.text
