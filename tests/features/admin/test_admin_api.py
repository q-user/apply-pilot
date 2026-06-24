"""HTTP tests for the admin auth gate + probe-error sanitization (issue #145).

This module covers the two complementary fixes introduced for issue #145:

* **Probe-error sanitization** — when an admin health probe raises, the
  rendered HTML row must not leak the raw exception ``str(exc)`` (which
  may carry a SQLAlchemy DSN, redis URI, bearer token, ``password=`` /
  ``api_key=`` value, or stack frame). The page should expose the
  exception class name plus a sanitized version of the message; the
  original is still emitted to the application log.

* **Bearer-token auth gate** — every M6/M8 admin endpoint must reject
  anonymous requests with ``401`` when ``APP_ADMIN_REQUIRE_AUTH`` is
  enabled (the default). The flag is wired through
  :func:`apply_pilot.config.get_admin_auth_required`; tests override
  the dependency to flip the flag without touching the environment.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import Session, sessionmaker

from apply_pilot.config import get_admin_auth_required
from apply_pilot.db import Base, get_db
from apply_pilot.features.admin.api import (
    get_integration_status_store,
    get_integration_status_worker,
)
from apply_pilot.features.admin.api import (
    router as admin_router,
)
from apply_pilot.features.admin.health import (
    HealthCheckResult,
    get_health_checks,
)
from apply_pilot.features.admin.integrations import (
    HhOAuthChecker,
    InMemoryIntegrationStatusStore,
    IntegrationStatus,
    IntegrationStatusWorker,
    LlmChecker,
)
from apply_pilot.features.audit import models as _audit_models  # noqa: F401
from apply_pilot.features.audit import repository as _audit_repository  # noqa: F401
from apply_pilot.features.hh.oauth import HhHttpOAuthClient
from apply_pilot.features.matches import models as _match_models  # noqa: F401
from apply_pilot.features.scoring_ab import models as _scoring_ab_models  # noqa: F401
from apply_pilot.features.scoring_ab.api import router as scoring_ab_router
from apply_pilot.features.scoring_ab.experiments import (
    InMemoryScoringExperimentRepository,
    ScoringExperiment,
    ScoringVariant,
)
from apply_pilot.features.scoring_review.api import router as scoring_review_router
from apply_pilot.features.scoring_review.repository import (
    InMemoryScoringReviewQueue,
)
from apply_pilot.features.source_metrics.api import router as source_metrics_router
from apply_pilot.features.source_metrics.models import SourceMetricEventKind
from apply_pilot.features.source_metrics.repository import (
    InMemorySourceMetricRepository,
    SourceMetricEvent,
)
from apply_pilot.features.users.security import issue_token

# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


def _hh_oauth_handler(status_code: int) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
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


def _llm_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})


class _RaisingHealthCheck:
    """A :class:`HealthCheck` stub that always raises the given exception."""

    def __init__(self, name: str, exc: BaseException) -> None:
        self._name = name
        self._exc = exc

    @property
    def name(self) -> str:
        return self._name

    async def run(self) -> HealthCheckResult:
        raise self._exc


# ---------------------------------------------------------------------------
# Engine / DB session fixtures (SQLite in-memory, shared across routers)
# ---------------------------------------------------------------------------


@pytest.fixture
def engine() -> Iterator[Any]:
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
def session_factory(engine: Any) -> Any:
    return sessionmaker(bind=engine, class_=Session, autocommit=False, autoflush=False)


@pytest.fixture
def override_get_db(session_factory: Any) -> Any:
    def _override() -> Iterator[Session]:
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    return _override


# ---------------------------------------------------------------------------
# Per-endpoint fake repositories
# ---------------------------------------------------------------------------


@pytest.fixture
def integration_store() -> InMemoryIntegrationStatusStore:
    store = InMemoryIntegrationStatusStore()
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
    return store


@pytest.fixture
def integration_worker(
    integration_store: InMemoryIntegrationStatusStore,
) -> IntegrationStatusWorker:
    hh = HhOAuthChecker(
        client=HhHttpOAuthClient(
            client_id="cid",
            client_secret="secret",
            redirect_uri="https://example.com/cb",
            transport=httpx.MockTransport(_hh_oauth_handler(200)),
        )
    )
    llm = LlmChecker(
        client=__import__(
            "apply_pilot.features.scoring.llm", fromlist=["HttpLLMClient", "LLMSettings"]
        ).HttpLLMClient(
            __import__(
                "apply_pilot.features.scoring.llm", fromlist=["HttpLLMClient", "LLMSettings"]
            ).LLMSettings(api_key="test-key", base_url="https://llm.example.com/v1", model="m"),
            transport=httpx.MockTransport(_llm_handler),
        )
    )
    return IntegrationStatusWorker(
        store=integration_store,
        checkers=[hh, llm],
        refresh_interval_seconds=60.0,
        name="issue-145-worker",
    )


@pytest.fixture
def experiment_repo() -> InMemoryScoringExperimentRepository:
    repo = InMemoryScoringExperimentRepository()
    variant = ScoringVariant(name="v1", prompt_version="p1", weight=1.0)
    repo.add(
        ScoringExperiment(
            id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
            name="baseline",
            prompt_name="cover_letter",
            active=True,
            created_at=datetime.now(UTC),
            variants=[variant],
        )
    )
    return repo


@pytest.fixture
def review_queue() -> InMemoryScoringReviewQueue:
    return InMemoryScoringReviewQueue(
        match_repo=__import__(
            "apply_pilot.features.matches.repository", fromlist=["InMemoryVacancyMatchRepository"]
        ).InMemoryVacancyMatchRepository(),
        profile_repo=__import__(
            "apply_pilot.features.search_profiles.repository",
            fromlist=["InMemorySearchProfileRepository"],
        ).InMemorySearchProfileRepository(),
    )


@pytest.fixture
def source_metric_repo() -> InMemorySourceMetricRepository:
    repo = InMemorySourceMetricRepository()
    repo.record(
        SourceMetricEvent(
            id=uuid.uuid4(),
            source_name="hh",
            kind=SourceMetricEventKind.FETCH,
            count=1,
            duration_ms=10,
            timestamp=datetime.now(UTC),
            metadata={},
        )
    )
    return repo


# ---------------------------------------------------------------------------
# Auth fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_user_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def admin_token(admin_user_id: str) -> str:
    # The :func:`admin_token` fixture is consumed by tests that
    # ultimately feed the bearer header into a ``_admin_override`` that
    # resolves the token through a per-app store injected via
    # ``app.dependency_overrides[get_token_store]`` (issue #209). The
    # ``make_app`` fixture seeds that store and re-issues the token
    # through it; the tests below then read the resulting token off
    # the application. Keeping the original ``issue_token`` call here
    # would write to the module-level default store, which the
    # override does NOT consult, so the test would 401.
    return issue_token(admin_user_id, ttl_seconds=300)


@pytest.fixture
def make_app(
    integration_store: InMemoryIntegrationStatusStore,
    integration_worker: IntegrationStatusWorker,
    experiment_repo: InMemoryScoringExperimentRepository,
    review_queue: InMemoryScoringReviewQueue,
    source_metric_repo: InMemorySourceMetricRepository,
    override_get_db: Any,
) -> Any:
    """Factory: build a :class:`FastAPI` app with all admin routers + DI.

    The ``auth_required`` keyword controls the auth gate without
    touching the environment, so a single fixture can power both the
    "anonymous gets 401" and the "bearer token works" tests.
    """

    def _factory(*, auth_required: bool) -> FastAPI:
        application = FastAPI()
        application.include_router(admin_router)
        application.include_router(scoring_ab_router)
        application.include_router(scoring_review_router)
        application.include_router(source_metrics_router)

        application.dependency_overrides[get_db] = override_get_db
        application.dependency_overrides[get_integration_status_store] = lambda: integration_store
        application.dependency_overrides[get_integration_status_worker] = lambda: integration_worker
        application.dependency_overrides[get_admin_auth_required] = lambda: auth_required

        # Issue #171 tightened the admin auth gate: ``require_admin_user``
        # now also looks up the user record and checks ``is_admin``. The
        # tests in this module use a synthetic UUID that has no backing
        # ``User`` row, so the strict lookup would 401/403. We override
        # the dependency to skip the user-record lookup while still
        # honouring the ``APP_ADMIN_REQUIRE_AUTH`` flag and the bearer
        # scheme:
        #
        # * ``auth_required=False`` (the default in the legacy tests)
        #   returns ``"anonymous"`` without touching the token store —
        #   the same behaviour the pre-#171 gate had.
        # * ``auth_required=True`` requires a valid bearer token; the
        #   synthetic id flows through unchanged.
        #
        # The dedicated :mod:`tests.features.admin.test_admin_auth_gate`
        # suite exercises the production gate (token + ``is_admin``)
        # end-to-end.
        from fastapi import Depends
        from fastapi.security import HTTPAuthorizationCredentials

        from apply_pilot.features.admin._auth import (
            _bearer_scheme,
            get_token_store,
            require_admin_user,
        )
        from apply_pilot.features.users.security import (
            InMemoryTokenStore,
            TokenStore,
        )

        # Issue #209: plug a fresh in-memory token store through the new
        # ``get_token_store`` dependency. The default (process-wide)
        # store is shared across tests, which is fine for most suites,
        # but this module needs a deterministic token it can issue via
        # the :func:`issue_token` helper without leaking state.
        class _LocalTokenStore:
            def __init__(self) -> None:
                self._inner = InMemoryTokenStore()

            def issue(self, user_id: str, ttl_seconds: int) -> str:
                return self._inner.issue(user_id, ttl_seconds=ttl_seconds)

            def resolve(self, token: str) -> str:
                return self._inner.resolve(token)

            def revoke(self, token: str) -> None:
                self._inner.revoke(token)

        token_store: TokenStore = _LocalTokenStore()
        application.dependency_overrides[get_token_store] = lambda: token_store

        def _admin_override(
            auth_required: bool = Depends(get_admin_auth_required),  # noqa: B008
            credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),  # noqa: B008
        ) -> str:
            if not auth_required:
                return "anonymous"
            if credentials is None:
                from fastapi import HTTPException, status

                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail={
                        "code": "authentication_required",
                        "message": "bearer token is required",
                    },
                )
            try:
                # The token must still resolve — this preserves the
                # 401 ``invalid_token`` contract for the
                # ``test_admin_endpoints_reject_invalid_token`` test.
                # We use the override-bound store, not the module-level
                # default (issue #209).
                token_store.resolve(credentials.credentials)
            except Exception as exc:  # InvalidTokenError or anything from the stub
                from fastapi import HTTPException, status

                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail={
                        "code": "invalid_token",
                        "message": "the supplied token is invalid or expired",
                    },
                ) from exc
            return admin_user_id

        application.dependency_overrides[require_admin_user] = _admin_override
        # The default dependencies for the other admin routers read
        # from a real DB session; the scoring_ab / source_metrics slices
        # both expose a public ``get_*_repository`` factory we can
        # override to skip SQLAlchemy.
        from apply_pilot.features.scoring_ab.api import get_experiment_repo
        from apply_pilot.features.scoring_review.api import get_scoring_review_service
        from apply_pilot.features.source_metrics.api import get_source_metric_repository

        async def _review_service_stub() -> Any:  # pragma: no cover - only used in passing tests
            return None

        application.dependency_overrides[get_experiment_repo] = lambda: experiment_repo
        application.dependency_overrides[get_source_metric_repository] = lambda: source_metric_repo
        application.dependency_overrides[get_scoring_review_service] = _review_service_stub

        # Issue #209: the test fixtures issue a token via
        # :func:`issue_token` (which writes to the module-level default
        # store), but the override above consults the per-app local
        # store only. Re-issue the same ``admin_user_id`` through the
        # local store and stash the resulting token on the application
        # so the test can read it via ``app.state.admin_token``.
        local_token = token_store.issue(admin_user_id, ttl_seconds=300)
        application.state.admin_token = local_token
        return application

    return _factory


# ---------------------------------------------------------------------------
# (1) Probe-error sanitization
# ---------------------------------------------------------------------------


def test_health_page_sanitizes_sqlalchemy_connection_string(
    make_app: Any,
) -> None:
    """A SQLAlchemy error carrying a DSN must not appear in the rendered HTML."""
    leaky = (
        "OperationalError: (psycopg2.OperationalError) could not connect to server: "
        "postgresql://admin:supersecret@db.internal.example.com:5432/applypilot?sslmode=require"
    )
    application = make_app(auth_required=False)
    application.dependency_overrides[get_health_checks] = lambda: [
        _RaisingHealthCheck("database", Exception(leaky)),
    ]
    with TestClient(application) as client:
        body = client.get("/admin/health").text

    assert "postgresql://" not in body
    assert "supersecret" not in body
    assert "db.internal.example.com" not in body
    # The exception class name *is* expected to be visible so operators
    # can still see *what* went wrong.
    assert "Exception" in body
    # And the redaction marker replaces the secret bits.
    assert "[REDACTED]" in body


def test_health_page_sanitizes_redis_uri_and_bearer_token(make_app: Any) -> None:
    """A redis error carrying a URI and a Bearer token must be redacted."""
    leaky = (
        "redis.exceptions.ConnectionError: Error 111 connecting to "
        "redis://:hunter2@cache.internal.example.com:6379/0; "
        "received auth header 'Bearer abcdefghijklmnop.qrstuvwxyz'"
    )
    application = make_app(auth_required=False)
    application.dependency_overrides[get_health_checks] = lambda: [
        _RaisingHealthCheck("redis", Exception(leaky)),
    ]
    with TestClient(application) as client:
        body = client.get("/admin/health").text

    assert "redis://" not in body
    assert "hunter2" not in body
    assert "cache.internal.example.com" not in body
    assert "abcdefghijklmnop" not in body
    assert "[REDACTED]" in body


def test_health_page_sanitizes_password_kv(make_app: Any) -> None:
    """Plain ``password=...`` / ``api_key=...`` assignments are scrubbed."""
    leaky = (
        "LLM client error: 401 unauthorized (api_key=topsecret-key-12345, "
        "password=hunter2, token=ghp_abcdef0123456789)"
    )
    application = make_app(auth_required=False)
    application.dependency_overrides[get_health_checks] = lambda: [
        _RaisingHealthCheck("llm", Exception(leaky)),
    ]
    with TestClient(application) as client:
        body = client.get("/admin/health").text

    assert "topsecret-key-12345" not in body
    assert "hunter2" not in body
    assert "ghp_abcdef0123456789" not in body
    assert "[REDACTED]" in body


def test_health_page_sanitize_helper_directly() -> None:
    """Direct unit test of the redaction helper (no FastAPI)."""
    from apply_pilot.features.admin.api import _sanitize_error_message

    sanitized = _sanitize_error_message(
        "postgresql://user:pass@host:5432/db; password=foo; Bearer abc.def-ghi",
    )
    assert "postgresql://" not in sanitized
    assert "pass" not in sanitized
    assert "host" not in sanitized or "[REDACTED]" in sanitized
    assert "abc.def-ghi" not in sanitized
    assert "[REDACTED]" in sanitized


# ---------------------------------------------------------------------------
# (2) Bearer-token auth gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method, path, body",
    [
        ("GET", "/admin/integrations", None),
        ("POST", "/admin/integrations/refresh", None),
        ("GET", "/admin/health", None),
        ("GET", "/admin/scoring/experiments", None),
        (
            "GET",
            "/admin/scoring/experiments/baseline/outcomes",
            None,
        ),
        ("GET", "/admin/sources/metrics?source=hh", None),
        ("GET", "/admin/scoring-review/queue", None),
        (
            "POST",
            "/admin/scoring-review/00000000-0000-0000-0000-000000000000/note",
            {"note": "looks fine"},
        ),
    ],
)
def test_admin_endpoints_reject_anonymous_when_auth_required(
    make_app: Any,
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    """Every admin endpoint must return 401 with no Authorization header."""
    application = make_app(auth_required=True)
    with TestClient(application) as client:
        response = client.request(method, path, json=body)
        assert response.status_code == 401, response.text
        # Stable JSON shape, same as the cover-letter-style router.
        detail = response.json()["detail"]
        assert detail["code"] == "authentication_required"


def test_admin_endpoints_reject_invalid_token_when_auth_required(
    make_app: Any,
) -> None:
    """A garbage Authorization header must yield 401 ``invalid_token``."""
    application = make_app(auth_required=True)
    with TestClient(application) as client:
        response = client.get(
            "/admin/integrations",
            headers={"Authorization": "Bearer not-a-real-token"},
        )
        assert response.status_code == 401
        assert response.json()["detail"]["code"] == "invalid_token"


@pytest.mark.parametrize(
    "method, path, body, expected_status, body_assert",
    [
        ("GET", "/admin/integrations", None, 200, None),
        ("POST", "/admin/integrations/refresh", None, 200, None),
        ("GET", "/admin/scoring/experiments", None, 200, None),
        (
            "GET",
            "/admin/scoring/experiments/baseline/outcomes",
            None,
            200,
            "experiment",
        ),
        ("GET", "/admin/sources/metrics?source=hh", None, 200, None),
    ],
)
def test_admin_endpoints_accept_valid_token_when_auth_required(
    make_app: Any,
    method: str,
    path: str,
    body: dict[str, Any] | None,
    expected_status: int,
    body_assert: str | None,
) -> None:
    """A valid bearer token must let the request through to the handler."""
    application = make_app(auth_required=True)
    # Issue #209: the bearer token must be issued through the per-app
    # ``get_token_store`` override (the legacy ``admin_token`` fixture
    # writes to the module-level default store, which the override does
    # NOT consult). The factory stashes the right token on
    # ``app.state.admin_token``.
    local_token = application.state.admin_token
    with TestClient(application) as client:
        response = client.request(
            method,
            path,
            json=body,
            headers={"Authorization": f"Bearer {local_token}"},
        )
        assert response.status_code == expected_status, response.text
        if body_assert is not None:
            payload = response.json()
            assert body_assert in payload


@pytest.mark.parametrize(
    "method, path, body",
    [
        ("GET", "/admin/integrations", None),
        ("POST", "/admin/integrations/refresh", None),
        ("GET", "/admin/health", None),
        ("GET", "/admin/scoring/experiments", None),
        (
            "GET",
            "/admin/scoring/experiments/baseline/outcomes",
            None,
        ),
        ("GET", "/admin/sources/metrics?source=hh", None),
    ],
)
def test_admin_endpoints_accept_anonymous_when_auth_disabled(
    make_app: Any,
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    """With the auth flag off, anonymous requests succeed."""
    application = make_app(auth_required=False)
    with TestClient(application) as client:
        response = client.request(method, path, json=body)
        # Every endpoint above returns 2xx on the happy path; the
        # health page is the only HTML response in this list, so we
        # do not inspect the body.
        assert response.status_code < 400, response.text
