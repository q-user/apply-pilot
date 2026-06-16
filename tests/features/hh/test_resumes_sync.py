"""TDD tests for the hh.ru resume metadata sync (M2, issue #21).

These tests cover the public surface of :mod:`job_apply.features.hh.resumes`:

* :class:`HhResumesClient` Protocol — the narrow contract every
  collaborator depends on, with both the in-memory and the production
  HTTP implementation.
* :class:`HhResumesSyncService` — the orchestrator that pulls metadata
  from hh.ru and upserts it into the :class:`HhResumeLink` table.
* :class:`HhResumeLinkRepository` — in-memory and SQL implementations.
* The ``POST /hh/resumes/sync`` and ``GET /hh/resumes`` HTTP endpoints.

The HTTP client tests use :class:`httpx.MockTransport` so no real
network call ever leaves the test process.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

# Generate a valid Fernet key for the encryptor dependency used by the
# /hh/credentials tests we share fixtures with. Real credentials are not
# exercised by these tests, but the encryptor is constructed lazily by
# the dependency graph and would crash without a key.
os.environ.setdefault("APP_HH_ENCRYPTION_KEY", Fernet.generate_key().decode())

from job_apply.db import Base, get_db  # noqa: E402
from job_apply.features.hh.api import router as hh_router  # noqa: E402
from job_apply.features.hh.encryption import CredentialEncryptor  # noqa: E402
from job_apply.features.hh.resumes import (  # noqa: E402
    HhHttpResumesClient,
    HhResumeLink,
    HhResumeLinkRepository,
    HhResumesClient,
    HhResumesSyncService,
    InMemoryHhResumeLinkRepository,
    InMemoryHhResumesClient,
    SqlHhResumeLinkRepository,
)
from job_apply.features.hh.schemas import InternalCredentials  # noqa: E402
from job_apply.features.users.api import router as auth_router  # noqa: E402

_TEST_ENCRYPTOR = CredentialEncryptor(key=Fernet.generate_key())


def _install_test_encryptor() -> None:
    """Patch :func:`_get_encryptor` to return a stable test encryptor.

    The real factory reads ``APP_HH_ENCRYPTION_KEY`` at request time. We
    override it so tests do not have to plumb the env var through every
    request.
    """
    from job_apply.features.hh import api as hh_api

    hh_api._get_encryptor = lambda: _TEST_ENCRYPTOR  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _hh_resume(
    resume_id: str,
    *,
    title: str = "Python Developer",
    updated_at: str = "2026-06-01T10:00:00+0300",
) -> dict[str, Any]:
    """Build a minimal realistic hh.ru ``/resumes/mine`` item payload."""
    return {
        "id": resume_id,
        "title": title,
        "updated_at": updated_at,
    }


def _resume_mine_response(*items: dict[str, Any]) -> dict[str, Any]:
    """Build a top-level ``GET /resumes/mine`` response body."""
    return {"items": list(items)}


# ---------------------------------------------------------------------------
# In-memory client
# ---------------------------------------------------------------------------


class TestInMemoryHhResumesClient:
    def test_returns_preloaded_resumes(self) -> None:
        """Preloaded fixtures are returned verbatim, in insertion order."""
        items = [_hh_resume("r1"), _hh_resume("r2", title="Go Dev")]
        client: HhResumesClient = InMemoryHhResumesClient(fixtures=[items])

        result = asyncio.run(client.list_user_resumes())

        assert result == items

    def test_get_resume_returns_matching_fixture(self) -> None:
        """``get_resume`` returns the preloaded item with the matching id."""
        items = [_hh_resume("r1"), _hh_resume("r2", title="Go Dev")]
        client = InMemoryHhResumesClient(fixtures=[items])

        result = asyncio.run(client.get_resume("r2"))

        assert result["id"] == "r2"
        assert result["title"] == "Go Dev"

    def test_get_resume_unknown_id_raises(self) -> None:
        """Unknown id raises a not-found error."""
        client = InMemoryHhResumesClient(fixtures=[[]])

        with pytest.raises(Exception, match="not found"):
            asyncio.run(client.get_resume("missing"))

    def test_default_fixtures_empty(self) -> None:
        """No fixtures → empty list (matches hh.ru's zero-resumes case)."""
        client: HhResumesClient = InMemoryHhResumesClient()

        result = asyncio.run(client.list_user_resumes())

        assert result == []


# ---------------------------------------------------------------------------
# HTTP client (httpx.MockTransport)
# ---------------------------------------------------------------------------


def _capturing_transport(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    """Build a MockTransport that captures the requests it received."""
    captured: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    return httpx.MockTransport(_handler), captured


class TestHhHttpResumesClient:
    @pytest.mark.asyncio
    async def test_list_user_resumes_calls_hh_api(self) -> None:
        """``list_user_resumes`` hits ``GET /resumes/mine`` with a Bearer token."""
        token = "hh-access-token-123"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_resume_mine_response(_hh_resume("r1")))

        transport, captured = _capturing_transport(handler)
        async with httpx.AsyncClient(transport=transport) as http:
            client = HhHttpResumesClient(
                client=http,
                base_url="https://api.hh.ru",
                token_provider=lambda: token,
            )
            items = await client.list_user_resumes()

        assert items == [
            {"id": "r1", "title": "Python Developer", "updated_at": "2026-06-01T10:00:00+0300"}
        ]
        assert len(captured) == 1
        request = captured[0]
        assert request.method == "GET"
        assert str(request.url) == "https://api.hh.ru/resumes/mine"
        assert request.headers["authorization"] == f"Bearer {token}"

    @pytest.mark.asyncio
    async def test_list_user_resumes_parses_response(self) -> None:
        """The ``items`` array from the response body is returned in order."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=_resume_mine_response(
                    _hh_resume("r1", title="Python"),
                    _hh_resume("r2", title="Go", updated_at="2026-05-20T12:00:00+0300"),
                ),
            )

        transport, _ = _capturing_transport(handler)
        async with httpx.AsyncClient(transport=transport) as http:
            client = HhHttpResumesClient(
                client=http,
                base_url="https://api.hh.ru",
                token_provider=lambda: "tok",
            )
            items = await client.list_user_resumes()

        assert [item["id"] for item in items] == ["r1", "r2"]
        assert [item["title"] for item in items] == ["Python", "Go"]

    @pytest.mark.asyncio
    async def test_list_user_resumes_handles_empty_response(self) -> None:
        """An empty ``items`` array returns ``[]``."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_resume_mine_response())

        transport, _ = _capturing_transport(handler)
        async with httpx.AsyncClient(transport=transport) as http:
            client = HhHttpResumesClient(
                client=http,
                base_url="https://api.hh.ru",
                token_provider=lambda: "tok",
            )
            items = await client.list_user_resumes()

        assert items == []

    @pytest.mark.asyncio
    async def test_get_resume_uses_id_in_path(self) -> None:
        """``get_resume`` issues ``GET /resumes/{id}`` and returns the body."""

        def handler(request: httpx.Request) -> httpx.Response:
            assert str(request.url) == "https://api.hh.ru/resumes/abc-123"
            return httpx.Response(200, json={"id": "abc-123", "title": "Senior Dev"})

        transport, _ = _capturing_transport(handler)
        async with httpx.AsyncClient(transport=transport) as http:
            client = HhHttpResumesClient(
                client=http,
                base_url="https://api.hh.ru",
                token_provider=lambda: "tok",
            )
            result = await client.get_resume("abc-123")

        assert result["id"] == "abc-123"


# ---------------------------------------------------------------------------
# In-memory repository
# ---------------------------------------------------------------------------


class TestInMemoryRepository:
    def test_upsert_creates_new_link(self) -> None:
        """``upsert`` on an empty repo inserts a new link."""
        repo = InMemoryHhResumeLinkRepository()
        user_id = uuid.uuid4()
        link = HhResumeLink(
            user_id=user_id,
            hh_resume_id="hh-1",
            title="Python",
            updated_at_hh=datetime(2026, 6, 1, tzinfo=UTC),
        )

        saved = repo.upsert(link)

        assert saved.id is not None
        assert saved.user_id == user_id
        assert repo.list_by_user(user_id) == [saved]

    def test_upsert_updates_existing_link(self) -> None:
        """``upsert`` matches on ``(user_id, hh_resume_id)`` and updates fields."""
        repo = InMemoryHhResumeLinkRepository()
        user_id = uuid.uuid4()
        first = repo.upsert(
            HhResumeLink(
                user_id=user_id,
                hh_resume_id="hh-1",
                title="Old",
                updated_at_hh=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )

        updated = repo.upsert(
            HhResumeLink(
                user_id=user_id,
                hh_resume_id="hh-1",
                title="New",
                updated_at_hh=datetime(2026, 6, 1, tzinfo=UTC),
            )
        )

        assert updated.id == first.id  # same row
        assert updated.title == "New"
        assert repo.list_by_user(user_id) == [updated]

    def test_list_by_user_returns_only_user_links(self) -> None:
        """Links belonging to other users are not returned."""
        repo = InMemoryHhResumeLinkRepository()
        u1, u2 = uuid.uuid4(), uuid.uuid4()
        link_u1 = repo.upsert(
            HhResumeLink(
                user_id=u1,
                hh_resume_id="hh-1",
                title="t",
                updated_at_hh=datetime.now(UTC),
            )
        )
        repo.upsert(
            HhResumeLink(
                user_id=u2,
                hh_resume_id="hh-1",
                title="t",
                updated_at_hh=datetime.now(UTC),
            )
        )

        assert repo.list_by_user(u1) == [link_u1]
        assert len(repo.list_by_user(u2)) == 1


# ---------------------------------------------------------------------------
# SQL repository
# ---------------------------------------------------------------------------


@pytest.fixture
def engine() -> Iterator[Engine]:
    """Fresh in-memory sqlite engine per test, with all tables created."""
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
def session_factory(engine: Engine) -> Callable[[], Session]:
    """Return a session factory bound to the in-memory engine."""
    return sessionmaker(bind=engine, class_=Session, autocommit=False, autoflush=False)


@pytest.fixture
def user_id(session_factory: Callable[[], Session]) -> uuid.UUID:
    """Insert and return a real user row so the FK is satisfied."""
    from job_apply.features.users.models import User

    sess = session_factory()
    try:
        user = User(
            email=f"hh-resumes-{uuid.uuid4()}@example.com",
            hashed_password="x",  # tests do not exercise auth
        )
        sess.add(user)
        sess.commit()
        sess.refresh(user)
        return user.id
    finally:
        sess.close()


class TestSqlRepository:
    def test_repository_sql_upserts_and_lists(
        self, session_factory: Callable[[], Session], user_id: uuid.UUID
    ) -> None:
        """Round-trip via SQL: upsert + list_by_user returns the persisted row."""
        sess = session_factory()
        try:
            repo: HhResumeLinkRepository = SqlHhResumeLinkRepository(session=sess)

            first = repo.upsert(
                HhResumeLink(
                    user_id=user_id,
                    hh_resume_id="hh-1",
                    title="Python Dev",
                    updated_at_hh=datetime(2026, 6, 1, tzinfo=UTC),
                )
            )

            assert first.id is not None
            rows = repo.list_by_user(user_id)
            assert len(rows) == 1
            assert rows[0].hh_resume_id == "hh-1"
            assert rows[0].title == "Python Dev"

            # Update the same row by (user_id, hh_resume_id).
            second = repo.upsert(
                HhResumeLink(
                    user_id=user_id,
                    hh_resume_id="hh-1",
                    title="Senior Python Dev",
                    updated_at_hh=datetime(2026, 6, 15, tzinfo=UTC),
                )
            )

            assert second.id == first.id
            rows = repo.list_by_user(user_id)
            assert len(rows) == 1
            assert rows[0].title == "Senior Python Dev"
        finally:
            sess.close()


# ---------------------------------------------------------------------------
# HhResumesSyncService
# ---------------------------------------------------------------------------


class _CredentialRepoStub:
    """Stub repository that stores user_id → plaintext-token pairs."""

    def __init__(self) -> None:
        self.tokens: dict[uuid.UUID, str] = {}


class _StubHHCredentialService:
    """Bare-bones stand-in for :class:`HHCredentialService` in service tests.

    The real service touches the database, so we skip it and return a
    pre-configured access token directly. The shape of
    :meth:`get_credentials` mirrors the real one so production wiring can
    be exercised unchanged.
    """

    def __init__(self, repo: _CredentialRepoStub, user_id: uuid.UUID, token: str) -> None:
        self._repo = repo
        self._user_id = user_id
        self._token = token

    def get_credentials(self, user_id: uuid.UUID) -> InternalCredentials:
        if user_id not in self._repo.tokens:
            raise RuntimeError(f"no credentials stub for user {user_id!s}")
        return InternalCredentials(
            user_id=user_id,
            access_token=self._repo.tokens[user_id],
            refresh_token=None,
            token_type="bearer",
            expires_at=None,
        )


class TestHhResumesSyncService:
    @pytest.mark.asyncio
    async def test_sync_metadata_upserts_links(self) -> None:
        """``sync_metadata`` persists every resume from hh as a link row."""
        items = [_hh_resume("hh-1", title="Python"), _hh_resume("hh-2", title="Go")]
        client = InMemoryHhResumesClient(fixtures=[items])
        link_repo = InMemoryHhResumeLinkRepository()
        user_id = uuid.uuid4()
        credential_repo = _CredentialRepoStub()
        credential_repo.tokens[user_id] = "tok"
        service = HhResumesSyncService(
            resumes_client=client,
            credential_service=_StubHHCredentialService(credential_repo, user_id, "tok"),
            link_repo=link_repo,
            user_id=user_id,
        )

        result = await service.sync_metadata()

        assert [link.hh_resume_id for link in result] == ["hh-1", "hh-2"]
        assert [link.title for link in link_repo.list_by_user(user_id)] == ["Python", "Go"]

    @pytest.mark.asyncio
    async def test_sync_metadata_handles_empty_response(self) -> None:
        """An empty ``items`` list returns ``[]`` and persists nothing."""
        client = InMemoryHhResumesClient(fixtures=[[]])
        link_repo = InMemoryHhResumeLinkRepository()
        user_id = uuid.uuid4()
        credential_repo = _CredentialRepoStub()
        credential_repo.tokens[user_id] = "tok"

        service = HhResumesSyncService(
            resumes_client=client,
            credential_service=_StubHHCredentialService(credential_repo, user_id, "tok"),
            link_repo=link_repo,
            user_id=user_id,
        )

        result = await service.sync_metadata()

        assert result == []
        assert link_repo.list_by_user(user_id) == []

    @pytest.mark.asyncio
    async def test_sync_metadata_records_updated_at_hh(self) -> None:
        """``updated_at_hh`` is parsed from the hh payload and persisted."""
        items = [_hh_resume("hh-1", updated_at="2026-06-15T10:00:00+0300")]
        client = InMemoryHhResumesClient(fixtures=[items])
        link_repo = InMemoryHhResumeLinkRepository()
        user_id = uuid.uuid4()
        credential_repo = _CredentialRepoStub()
        credential_repo.tokens[user_id] = "tok"
        service = HhResumesSyncService(
            resumes_client=client,
            credential_service=_StubHHCredentialService(credential_repo, user_id, "tok"),
            link_repo=link_repo,
            user_id=user_id,
        )

        result = await service.sync_metadata()

        assert len(result) == 1
        expected = datetime.fromisoformat("2026-06-15T10:00:00+0300").astimezone(UTC)
        assert result[0].updated_at_hh == expected

    @pytest.mark.asyncio
    async def test_sync_metadata_updates_existing_link(self) -> None:
        """A re-sync updates the existing link instead of duplicating it."""
        client = InMemoryHhResumesClient(fixtures=[[_hh_resume("hh-1", title="v1")]])
        link_repo = InMemoryHhResumeLinkRepository()
        user_id = uuid.uuid4()
        credential_repo = _CredentialRepoStub()
        credential_repo.tokens[user_id] = "tok"
        service = HhResumesSyncService(
            resumes_client=client,
            credential_service=_StubHHCredentialService(credential_repo, user_id, "tok"),
            link_repo=link_repo,
            user_id=user_id,
        )

        first = (await service.sync_metadata())[0]
        client.replace_fixtures([[_hh_resume("hh-1", title="v2")]])
        second = (await service.sync_metadata())[0]

        assert second.id == first.id
        assert second.title == "v2"
        assert len(link_repo.list_by_user(user_id)) == 1


# ---------------------------------------------------------------------------
# API tests
# ---------------------------------------------------------------------------


@pytest.fixture
def app_with_hh_resumes(engine: Engine) -> Iterator[FastAPI]:
    """FastAPI app with the resumes routes registered on a fresh sqlite."""
    factory = sessionmaker(bind=engine, class_=Session, autocommit=False, autoflush=False)

    def _override_get_db() -> Iterator[Session]:
        session = factory()
        try:
            yield session
        finally:
            session.close()

    application = FastAPI()
    application.include_router(auth_router)
    application.include_router(hh_router)
    application.dependency_overrides[get_db] = _override_get_db
    _install_test_encryptor()

    try:
        yield application
    finally:
        application.dependency_overrides.clear()


@pytest.fixture
def client_with_hh_resumes(app_with_hh_resumes: FastAPI) -> Iterator[TestClient]:
    with TestClient(app_with_hh_resumes) as c:
        yield c


def _register_and_login(client: TestClient) -> tuple[str, uuid.UUID]:
    """Register a user, log in, and return ``(access_token, user_id)``."""
    email = f"resumes-sync-{uuid.uuid4().hex[:8]}@example.com"
    resp = client.post(
        "/auth/register",
        json={"email": email, "password": "hunter2!!"},
    )
    assert resp.status_code == 201
    user_id = uuid.UUID(resp.json()["id"])
    resp = client.post(
        "/auth/login",
        json={"email": email, "password": "hunter2!!"},
    )
    assert resp.status_code == 200
    return resp.json()["access_token"], user_id


class TestHhResumesApi:
    def test_api_sync_endpoint(
        self,
        client_with_hh_resumes: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``POST /hh/resumes/sync`` calls the service and returns the links."""
        token, user_id = _register_and_login(client_with_hh_resumes)

        captured: list[uuid.UUID] = []

        async def fake_sync_metadata(self: Any) -> list[HhResumeLink]:
            captured.append(self._user_id)
            return [
                HhResumeLink(
                    id=uuid.uuid4(),
                    user_id=self._user_id,
                    hh_resume_id="hh-1",
                    title="Python Dev",
                    updated_at_hh=datetime(2026, 6, 1, tzinfo=UTC),
                    created_at=datetime.now(UTC),
                )
            ]

        monkeypatch.setattr(
            HhResumesSyncService,
            "sync_metadata",
            fake_sync_metadata,
        )

        resp = client_with_hh_resumes.post(
            "/hh/resumes/sync",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["items"]) == 1
        assert body["items"][0]["hh_resume_id"] == "hh-1"
        assert body["items"][0]["title"] == "Python Dev"
        assert captured == [user_id]

    def test_api_list_endpoint(
        self,
        client_with_hh_resumes: TestClient,
        engine: Engine,
    ) -> None:
        """``GET /hh/resumes`` returns the caller's links."""
        token, user_id = _register_and_login(client_with_hh_resumes)

        # Seed the table directly via the SQL repository.
        factory = sessionmaker(bind=engine, class_=Session, autocommit=False, autoflush=False)
        sess = factory()
        try:
            repo = SqlHhResumeLinkRepository(session=sess)
            repo.upsert(
                HhResumeLink(
                    user_id=user_id,
                    hh_resume_id="hh-1",
                    title="Python Dev",
                    updated_at_hh=datetime(2026, 6, 1, tzinfo=UTC),
                )
            )
            repo.upsert(
                HhResumeLink(
                    user_id=user_id,
                    hh_resume_id="hh-2",
                    title="Go Dev",
                    updated_at_hh=datetime(2026, 6, 2, tzinfo=UTC),
                )
            )
        finally:
            sess.close()

        resp = client_with_hh_resumes.get(
            "/hh/resumes",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert resp.status_code == 200
        body = resp.json()
        ids = sorted(item["hh_resume_id"] for item in body["items"])
        assert ids == ["hh-1", "hh-2"]

    def test_api_sync_requires_auth(self, client_with_hh_resumes: TestClient) -> None:
        """``POST /hh/resumes/sync`` without a bearer token returns 401."""
        resp = client_with_hh_resumes.post("/hh/resumes/sync")
        assert resp.status_code == 401

    def test_api_list_requires_auth(self, client_with_hh_resumes: TestClient) -> None:
        """``GET /hh/resumes`` without a bearer token returns 401."""
        resp = client_with_hh_resumes.get("/hh/resumes")
        assert resp.status_code == 401
