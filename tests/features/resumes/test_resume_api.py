"""HTTP-level tests for the resumes API.

These tests stand up a FastAPI app with the resumes router mounted, wire
a real sqlite-in-memory database, and exercise the routes through
:class:`fastapi.testclient.TestClient`. They are the only tests in the
slice that go through the multipart parser and the HTTP error mapping.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Column, DateTime, String, create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from job_apply.db import Base, get_db
from job_apply.features.resumes.api import StubAuthDep
from job_apply.features.resumes.api import router as resumes_router
from job_apply.features.resumes.models import Resume


class _StubUserForTest(Base):
    """Test-only stub of the auth slice's ``User`` table.

    The real ``User`` model is owned by the auth slice (issue #11) and
    does not exist on ``origin/main`` at the time the resumes slice is
    being built. To exercise the resumes routes against a real SQLAlchemy
    session we need *some* ``users`` table to exist so the FK on
    ``resumes.user_id`` resolves. This stub is intentionally never
    imported by production code; it lives only in the test module.
    """

    __tablename__ = "users"

    id = Column(String(length=36), primary_key=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine():
    """Single-connection sqlite in-memory engine for the whole test session."""
    eng = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # Create the stub users table first so the FK on resumes.user_id
    # can resolve at create-table time.
    Base.metadata.create_all(eng, tables=[_StubUserForTest.__table__, Resume.__table__])
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session_factory(engine):
    """Build a sessionmaker bound to the test engine."""
    return sessionmaker(bind=engine, class_=Session, autocommit=False, autoflush=False)


@pytest.fixture
def app(session_factory):
    """Build a minimal FastAPI app with the resumes router and a fake DB dependency."""
    app = FastAPI()
    app.include_router(resumes_router)

    def _get_db_override() -> Session:
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _get_db_override
    return app


@pytest.fixture
def stub_user_id() -> uuid.UUID:
    """The user id the StubAuthDep will return."""
    return uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
def client(app) -> TestClient:
    """TestClient bound to the test app."""
    return TestClient(app)


# ---------------------------------------------------------------------------
# multipart body construction
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


# ---------------------------------------------------------------------------
# /resumes POST
# ---------------------------------------------------------------------------


def test_api_upload_txt_resume_creates_record(client: TestClient) -> None:
    """POST /resumes with a .txt body returns 201 and the DTO."""
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
    data = response.json()
    assert data["filename"] == "resume.txt"
    assert data["content_type"] == "text/plain"
    assert data["size"] == len(b"John Doe\nSenior Engineer\n")
    assert data["plain_text"] == "John Doe\nSenior Engineer\n"
    assert data["user_id"] == "00000000-0000-0000-0000-000000000001"


def test_api_upload_pdf_resume_returns_501(client: TestClient) -> None:
    """POST /resumes with a PDF body returns 501 NotImplementedError."""
    body, content_type = _multipart_body(
        filename="resume.pdf",
        content_type="application/pdf",
        payload=b"%PDF-1.4\n",
    )

    response = client.post(
        "/resumes",
        content=body,
        headers={"Content-Type": content_type},
    )

    assert response.status_code == 501
    assert "application/pdf" in response.json()["detail"]


def test_api_upload_oversized_file_returns_422(client: TestClient) -> None:
    """POST /resumes with a >10MB body returns 422 ValidationError."""
    payload = b"x" * (10 * 1024 * 1024 + 1)
    body, content_type = _multipart_body(
        filename="huge.txt",
        content_type="text/plain",
        payload=payload,
    )

    response = client.post(
        "/resumes",
        content=body,
        headers={"Content-Type": content_type},
    )

    assert response.status_code == 422
    assert "exceeds" in response.json()["detail"]


def test_api_upload_unsupported_content_type_returns_422(client: TestClient) -> None:
    """POST /resumes with an unsupported content type returns 422."""
    body, content_type = _multipart_body(
        filename="x.bin",
        content_type="application/octet-stream",
        payload=b"\x00\x01",
    )

    response = client.post(
        "/resumes",
        content=body,
        headers={"Content-Type": content_type},
    )

    assert response.status_code == 422
    assert "not supported" in response.json()["detail"]


def test_api_upload_without_multipart_returns_415(client: TestClient) -> None:
    """POST /resumes with a non-multipart body returns 415."""
    response = client.post(
        "/resumes",
        content=b"raw",
        headers={"Content-Type": "text/plain"},
    )

    assert response.status_code == 415


# ---------------------------------------------------------------------------
# /resumes GET (list)
# ---------------------------------------------------------------------------


def test_api_list_resumes_returns_only_stub_user_resumes(
    client: TestClient, app: FastAPI, session_factory
) -> None:
    """After uploading, GET /resumes returns the current user's resumes."""
    body, content_type = _multipart_body(
        filename="a.txt",
        content_type="text/plain",
        payload=b"hello",
    )
    create = client.post("/resumes", content=body, headers={"Content-Type": content_type})
    assert create.status_code == 201

    response = client.get("/resumes")

    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["filename"] == "a.txt"
    # The stub user id must match the one returned in the body of the upload.
    assert items[0]["user_id"] == "00000000-0000-0000-0000-000000000001"


def test_api_list_resumes_empty_for_new_user(client: TestClient) -> None:
    """An empty list is returned for a user with no resumes."""
    response = client.get("/resumes")
    assert response.status_code == 200
    assert response.json() == {"items": []}


# ---------------------------------------------------------------------------
# /resumes/{id} GET
# ---------------------------------------------------------------------------


def test_api_get_resume_returns_record(client: TestClient) -> None:
    """After upload, GET /resumes/{id} returns the matching record."""
    body, content_type = _multipart_body(
        filename="x.txt",
        content_type="text/plain",
        payload=b"data",
    )
    create = client.post("/resumes", content=body, headers={"Content-Type": content_type})
    resume_id = create.json()["id"]

    response = client.get(f"/resumes/{resume_id}")

    assert response.status_code == 200
    assert response.json()["id"] == resume_id
    assert response.json()["filename"] == "x.txt"


def test_api_get_resume_unknown_id_returns_404(client: TestClient) -> None:
    """GET /resumes/<random-uuid> returns 404."""
    response = client.get(f"/resumes/{uuid.uuid4()}")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Stub auth dep
# ---------------------------------------------------------------------------


def test_stub_auth_dep_returns_deterministic_uuid() -> None:
    """The stub auth dep returns the hard-coded UUID every time."""
    # Sanity check: the type alias is the public contract the auth slice
    # will replace; make sure it is a real dependency alias.
    assert StubAuthDep is not None
    # And the underlying function returns the expected UUID.
    from job_apply.features.resumes.api import _stub_current_user

    assert _stub_current_user() == uuid.UUID("00000000-0000-0000-0000-000000000001")
