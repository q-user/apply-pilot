"""Tests for the FastAPI application factory and /healthz endpoint."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from job_apply.app import create_app


def test_create_app_returns_fastapi_instance() -> None:
    """create_app() should return a FastAPI application instance."""
    app = create_app()

    assert isinstance(app, FastAPI)


def test_healthz_returns_200_ok_with_status_ok() -> None:
    """/healthz should respond with HTTP 200 and the JSON body {"status": "ok"}."""
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_healthz_does_not_touch_db_or_redis() -> None:
    """/healthz must answer without invoking the database or Redis.

    The request is made with no external infrastructure configured; if the
    handler reached out to DB or Redis the test would surface a connection
    error. The route must remain a pure, in-process health probe.
    """
    app = create_app()

    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"status": "ok"}
