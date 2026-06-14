"""Integration tests: verify audit events ARE logged during API calls.

These tests stand up a FastAPI app with both the auth and resumes
routers mounted, wire a real sqlite-in-memory database, and check
that audit events are persisted when register, login, and resume_upload
endpoints succeed.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from job_apply.db import Base, get_db
from job_apply.features.audit import models as _audit_models  # noqa: F401
from job_apply.features.resumes import models as _resumes_models  # noqa: F401
from job_apply.features.resumes.api import router as resumes_router
from job_apply.features.users import models as _users_models  # noqa: F401
from job_apply.features.users.api import router as auth_router


@pytest.fixture
def engine() -> Iterator[Engine]:
    """Build a fresh in-memory sqlite engine per test, with all tables created."""
    eng = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def app(engine: Engine) -> Iterator[FastAPI]:
    """Build a FastAPI app wired to the in-memory engine."""
    factory = sessionmaker(bind=engine, class_=Session, autocommit=False, autoflush=False)

    def _override_get_db() -> Iterator[Session]:
        session = factory()
        try:
            yield session
        finally:
            session.close()

    application = FastAPI()
    application.include_router(auth_router)
    application.include_router(resumes_router)
    application.dependency_overrides[get_db] = _override_get_db

    try:
        yield application
    finally:
        application.dependency_overrides.clear()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """TestClient bound to the per-test FastAPI app."""
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Helper: query audit_logs directly
# ---------------------------------------------------------------------------


def _count_audit_logs_by_type(engine: Engine, event_type: str) -> int:
    """Count audit_logs rows for a given event_type."""
    with engine.connect() as conn:
        result = conn.execute(
            text("SELECT COUNT(*) FROM audit_logs WHERE event_type = :etype"),
            {"etype": event_type},
        )
        return result.scalar_one()


def _count_audit_logs_by_user(engine: Engine, user_id: uuid.UUID) -> int:
    """Count audit_logs rows for a given user_id."""
    with engine.connect() as conn:
        result = conn.execute(
            text("SELECT COUNT(*) FROM audit_logs WHERE user_id = :uid"),
            {"uid": str(user_id)},
        )
        return result.scalar_one()


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------


def test_register_endpoint_logs_audit_event(client: TestClient, engine: Engine) -> None:
    """A successful POST /auth/register must create a 'register' audit log."""
    response = client.post(
        "/auth/register",
        json={"email": "alice@example.com", "password": "hunter2!!"},
    )
    assert response.status_code == 201
    user_id = uuid.UUID(response.json()["id"])

    assert _count_audit_logs_by_type(engine, "register") == 1
    assert _count_audit_logs_by_user(engine, user_id) == 1


def test_register_endpoint_duplicate_does_not_log_audit(client: TestClient, engine: Engine) -> None:
    """A failed registration (409) must NOT create an audit log."""
    client.post("/auth/register", json={"email": "bob@example.com", "password": "hunter2!!"})
    response = client.post(
        "/auth/register",
        json={"email": "bob@example.com", "password": "different-pw"},
    )
    assert response.status_code == 409

    # Still only the first successful registration
    assert _count_audit_logs_by_type(engine, "register") == 1


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


def test_login_endpoint_logs_audit_event(client: TestClient, engine: Engine) -> None:
    """A successful POST /auth/login must create a 'login' audit log."""
    client.post("/auth/register", json={"email": "carol@example.com", "password": "hunter2!!"})
    response = client.post(
        "/auth/login",
        json={"email": "carol@example.com", "password": "hunter2!!"},
    )
    assert response.status_code == 200
    user_id = uuid.UUID(response.json()["user"]["id"])

    assert _count_audit_logs_by_type(engine, "login") == 1
    assert _count_audit_logs_by_user(engine, user_id) >= 1  # register + login


def test_login_endpoint_failure_does_not_log_audit(client: TestClient, engine: Engine) -> None:
    """A failed POST /auth/login (401) must NOT create an audit log."""
    client.post("/auth/register", json={"email": "dan@example.com", "password": "hunter2!!"})
    response = client.post(
        "/auth/login",
        json={"email": "dan@example.com", "password": "WRONG-PW"},
    )
    assert response.status_code == 401

    # Only register, no login
    assert _count_audit_logs_by_type(engine, "login") == 0


# ---------------------------------------------------------------------------
# Resume upload
# ---------------------------------------------------------------------------


def _multipart_body(filename: str, content_type: str, payload: bytes) -> tuple[bytes, str]:
    """Build a minimal multipart/form-data body with a single file part."""
    boundary = "----ApplyPilotTestBoundary12345"
    crlf = b"\r\n"
    body = (
        b"--"
        + boundary.encode()
        + crlf
        + b'Content-Disposition: form-data; name="file"; filename="'
        + filename.encode()
        + b'"'
        + crlf
        + b"Content-Type: "
        + content_type.encode()
        + crlf
        + crlf
        + payload
        + crlf
        + b"--"
        + boundary.encode()
        + b"--"
        + crlf
    )
    content_type_header = f"multipart/form-data; boundary={boundary}"
    return body, content_type_header


def _ensure_stub_user_exists(engine: Engine) -> None:
    """Insert the stub user row so the resumes FK can resolve."""
    from datetime import UTC, datetime

    stub_id = "00000000-0000-0000-0000-000000000001"
    with engine.connect() as conn:
        # Check if already exists
        result = conn.execute(text("SELECT COUNT(*) FROM users WHERE id = :uid"), {"uid": stub_id})
        if result.scalar_one() == 0:
            conn.execute(
                text(
                    "INSERT INTO users (id, email, hashed_password, is_active, created_at) "
                    "VALUES (:id, :email, :pw, :active, :now)"
                ),
                {
                    "id": stub_id,
                    "email": "stub@example.com",
                    "pw": "hashed",
                    "active": True,
                    "now": datetime.now(UTC),
                },
            )
            conn.commit()


def test_resume_upload_logs_audit_event(client: TestClient, engine: Engine) -> None:
    """A successful POST /resumes must create a 'resume_upload' audit log."""
    _ensure_stub_user_exists(engine)

    body, content_type = _multipart_body(
        filename="resume.txt",
        content_type="text/plain",
        payload=b"John Doe\nSenior Engineer\n",
    )

    response = client.post(
        "/resumes",
        content=body,
        headers={"Content-Type": content_type},
    )
    assert response.status_code == 201, response.text

    assert _count_audit_logs_by_type(engine, "resume_upload") == 1
    stub_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    assert _count_audit_logs_by_user(engine, stub_id) == 1
