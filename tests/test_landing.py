"""Tests for the static HTML landing page served at GET /.

The landing page is the minimal frontend shell (M6, issue #55). It must
render without any external dependencies and link to the existing API
documentation, health, and dashboard surfaces.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from apply_pilot.app import create_app


def test_landing_page_returns_200() -> None:
    """GET / should respond with HTTP 200 and a text/html body."""
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


def test_landing_page_contains_applypilot_branding() -> None:
    """The landing page should display the ApplyPilot brand name."""
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/")

    body = response.text
    assert "ApplyPilot" in body
    # A short marketing-style description should also be present.
    assert "job" in body.lower()


def test_landing_page_links_to_docs() -> None:
    """The landing page should link to /docs (Swagger UI) and /redoc."""
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/")

    body = response.text
    assert 'href="/docs"' in body
    assert 'href="/redoc"' in body


def test_landing_page_links_to_health() -> None:
    """The landing page should link to /admin/health."""
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/")

    assert 'href="/admin/health"' in response.text
    # The dashboard surface (for authenticated users) should also be linked.
    assert 'href="/dashboard"' in response.text
