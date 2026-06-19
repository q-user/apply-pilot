"""HTTP tests for the dashboard HTML page (M6, issue #172).

The dashboard web layer renders ``GET /dashboard`` as a login-gated HTML
page that shows the per-user activity summary from :class:`DashboardService`
plus a "recent apply jobs" list. The page accepts the browser-friendly
session cookie issued by PR #170 (``apply_pilot_session``) as well as the
canonical ``Authorization: Bearer`` header.

The tests follow the existing test_dashboard.py pattern: an in-memory
sqlite engine shared with the FastAPI app, the auth router for token
issuance, and the dashboard router under test. The tests focus on the
HTML response shape -- escaping, status badge classes, and the
unauthenticated redirect -- without re-testing the JSON contract
(that lives in test_dashboard.py).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

# Workaround for a pre-existing circular import between ``users.api`` and
# the worker runtime; pre-loading ``apply_worker`` here resolves the cycle
# when this file is collected in isolation (without xdist's lucky
# distribution). This is a collection-time aid only.
import apply_pilot.features.apply_worker  # noqa: E402,F401  (cycle breaker)
from apply_pilot.db import Base, get_db
from apply_pilot.features.apply_worker.models import ApplyJob, ApplyJobStatus
from apply_pilot.features.users.api import router as auth_router
from apply_pilot.features.users.session import SESSION_COOKIE_NAME

# ---------------------------------------------------------------------------
# Fixtures (mirror test_dashboard.py + test_auth_session.py)
# ---------------------------------------------------------------------------


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
    """Build a FastAPI app wired to the in-memory engine with the dashboard web router."""
    factory = sessionmaker(bind=engine, class_=Session, autocommit=False, autoflush=False)

    def _override_get_db() -> Iterator[Session]:
        session = factory()
        try:
            yield session
        finally:
            session.close()

    application = FastAPI()
    application.include_router(auth_router)
    # Import lazily so a failure to import the slice during the TDD red
    # phase surfaces as a test failure rather than a collection error.
    from apply_pilot.features.dashboard.web import router as dashboard_web_router

    application.include_router(dashboard_web_router)
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


def _register(client: TestClient, *, email: str, password: str = "hunter2!!") -> None:
    """Helper: register a user, asserting 201."""
    response = client.post("/auth/register", json={"email": email, "password": password})
    assert response.status_code == 201, response.text


def _login_token(client: TestClient, *, email: str, password: str = "hunter2!!") -> str:
    """Helper: log in via JSON and return the bearer token."""
    response = client.post(
        "/auth/login",
        json={"email": email, "password": password},
        headers={"Accept": "application/json"},
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _seed_user_and_session(
    client: TestClient, engine: Engine, *, email: str
) -> tuple[str, uuid.UUID]:
    """Register, log in, and return ``(token, user_id)``."""
    _register(client, email=email)
    token = _login_token(client, email=email)
    factory = sessionmaker(bind=engine, class_=Session, autocommit=False, autoflush=False)
    with factory() as session:
        from sqlalchemy import select

        from apply_pilot.features.users.models import User

        row = session.execute(select(User).where(User.email == email.lower())).scalar_one()
        return token, row.id


def _seed_apply_job(
    engine: Engine,
    *,
    user_id: uuid.UUID,
    status: str = ApplyJobStatus.QUEUED.value,
    vacancy_id: uuid.UUID | None = None,
    last_error: str | None = None,
) -> ApplyJob:
    """Insert a single ApplyJob directly via SQL so the test can control its fields."""
    from datetime import UTC, datetime

    from apply_pilot.features.apply_worker.models import compute_idempotency_key

    factory = sessionmaker(bind=engine, class_=Session, autocommit=False, autoflush=False)
    vid = vacancy_id or uuid.uuid4()
    match_id = uuid.uuid4()
    job_id = uuid.uuid4()
    now = datetime.now(UTC)
    with factory() as session:
        job = ApplyJob(
            id=job_id,
            match_id=match_id,
            user_id=user_id,
            vacancy_id=vid,
            status=status,
            idempotency_key=compute_idempotency_key(user_id, vid, match_id),
            last_error=last_error,
            created_at=now,
        )
        session.add(job)
        session.commit()
        session.refresh(job)
    return job


def _seed_vacancy(
    engine: Engine,
    *,
    source: str = "hh",
    source_id: str | None = None,
    title: str = "Test vacancy",
) -> uuid.UUID:
    """Insert a Vacancy row and return its id."""
    factory = sessionmaker(bind=engine, class_=Session, autocommit=False, autoflush=False)
    with factory() as session:
        from apply_pilot.features.sources.models import Vacancy

        vacancy = Vacancy(
            id=uuid.uuid4(),
            source=source,
            source_id=source_id or str(uuid.uuid4()),
            title=title,
            raw_data={"source": source, "source_id": source_id or ""},
        )
        session.add(vacancy)
        session.commit()
        session.refresh(vacancy)
        return vacancy.id


# ---------------------------------------------------------------------------
# Unauthenticated redirect
# ---------------------------------------------------------------------------


def test_dashboard_html_redirects_when_unauthenticated(client: TestClient) -> None:
    """GET /dashboard with no session and Accept: text/html -> 303 to /auth/login."""
    response = client.get(
        "/dashboard",
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login?next=/dashboard"


def test_dashboard_html_redirects_with_cookie_only_when_cookie_invalid(
    client: TestClient,
) -> None:
    """A stale or revoked session cookie still produces a redirect (not a 401).

    The HTML path never returns 401 -- it always bounces the visitor to
    the login form so the browser user never sees a JSON error page.
    """
    response = client.get(
        "/dashboard",
        headers={"Accept": "text/html"},
        cookies={SESSION_COOKIE_NAME: "definitely-not-a-real-token"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login?next=/dashboard"


# ---------------------------------------------------------------------------
# Authenticated render
# ---------------------------------------------------------------------------


def test_dashboard_html_renders_for_authenticated_user(client: TestClient, engine: Engine) -> None:
    """GET /dashboard with a valid session cookie -> 200 HTML containing the dashboard chrome."""
    token, _user_id = _seed_user_and_session(client, engine, email="alice@example.com")

    response = client.get(
        "/dashboard",
        headers={"Accept": "text/html"},
        cookies={SESSION_COOKIE_NAME: token},
    )

    assert response.status_code == 200
    body = response.text
    assert "Dashboard" in body
    assert "Logged in as alice@example.com" in body
    # The "Sign out" form is rendered.
    assert 'action="/auth/logout"' in body
    # The "Back to home" link is rendered.
    assert 'href="/"' in body


def test_dashboard_html_renders_zero_state_for_empty_db(client: TestClient, engine: Engine) -> None:
    """Authenticated, no data -> 200 HTML, all counts zero, empty recent-jobs table."""
    token, _user_id = _seed_user_and_session(client, engine, email="bob@example.com")

    response = client.get(
        "/dashboard",
        headers={"Accept": "text/html"},
        cookies={SESSION_COOKIE_NAME: token},
    )

    assert response.status_code == 200
    body = response.text
    # Counts grid shows zeros for an empty user.
    assert "Matches new today" in body
    assert "Seen" in body
    assert "Scored" in body
    assert "Accepted" in body
    assert "Applied" in body
    # Empty-state copy for the recent-jobs section.
    assert "No recent activity" in body or ">No recent activity<" in body


def test_dashboard_html_renders_recent_apply_jobs(client: TestClient, engine: Engine) -> None:
    """Three seeded ApplyJob rows render as three table rows in the recent-jobs section."""
    token, user_id = _seed_user_and_session(client, engine, email="carol@example.com")
    _seed_apply_job(engine, user_id=user_id, status=ApplyJobStatus.QUEUED.value)
    _seed_apply_job(engine, user_id=user_id, status=ApplyJobStatus.SUCCEEDED.value)
    _seed_apply_job(engine, user_id=user_id, status=ApplyJobStatus.FAILED.value)

    response = client.get(
        "/dashboard",
        headers={"Accept": "text/html"},
        cookies={SESSION_COOKIE_NAME: token},
    )

    assert response.status_code == 200
    body = response.text
    # The recent-jobs table has 3 body rows.
    tbody_match = re.search(r"<tbody[^>]*>(.*?)</tbody>", body, re.DOTALL)
    assert tbody_match is not None, "expected a <tbody> for recent apply jobs"
    tbody = tbody_match.group(1)
    assert len(re.findall(r"<tr[\s>]", tbody)) == 3
    # Each status is rendered with the expected badge text.
    assert ">queued<" in tbody or ">QUEUED<" in tbody
    assert ">succeeded<" in tbody or ">SUCCEEDED<" in tbody
    assert ">failed<" in tbody or ">FAILED<" in tbody


def test_dashboard_html_status_badge_classes(client: TestClient, engine: Engine) -> None:
    """Status badges use the same class conventions as /admin/health."""
    token, user_id = _seed_user_and_session(client, engine, email="dave@example.com")
    _seed_apply_job(engine, user_id=user_id, status=ApplyJobStatus.SUCCEEDED.value)
    _seed_apply_job(engine, user_id=user_id, status=ApplyJobStatus.FAILED.value)
    _seed_apply_job(engine, user_id=user_id, status=ApplyJobStatus.QUEUED.value)

    response = client.get(
        "/dashboard",
        headers={"Accept": "text/html"},
        cookies={SESSION_COOKIE_NAME: token},
    )

    assert response.status_code == 200
    body = response.text
    # Healthy (green) badge for succeeded jobs.
    assert 'class="status healthy">succeeded<' in body or "status healthy" in body
    # Unhealthy (red) badge for failed jobs.
    assert "status unhealthy" in body
    # Neutral (gray) badge for queued jobs.
    assert "status neutral" in body


def test_dashboard_html_escapes_user_input(client: TestClient, engine: Engine) -> None:
    """A user-controlled email containing angle brackets is HTML-escaped in the header.

    The ``EmailAddress`` regex accepts characters that need escaping
    (``<``, ``>``, ``&``, ``"``), so a hostile value that survives
    registration must be neutralised by ``html.escape`` on render.
    """
    hostile_email = "weird<x>@example.io"
    token, _user_id = _seed_user_and_session(client, engine, email=hostile_email)

    response = client.get(
        "/dashboard",
        headers={"Accept": "text/html"},
        cookies={SESSION_COOKIE_NAME: token},
    )

    assert response.status_code == 200
    body = response.text
    # The raw angle brackets must NOT appear unescaped.
    assert "weird<x>@example.io" not in body
    # The escaped form is fine.
    assert "weird&lt;x&gt;@example.io" in body


def test_dashboard_html_escapes_vacancy_id(client: TestClient, engine: Engine) -> None:
    """A Vacancy.source_id containing a <script> tag is HTML-escaped on render.

    The Vacancy model stores ``source_id`` as a free-form string,
    so a hostile value could conceivably land on the dashboard. The
    renderer must escape it.
    """
    token, user_id = _seed_user_and_session(client, engine, email="eve@example.com")
    hostile_source_id = "<script>alert(1)</script>"
    vacancy_id = _seed_vacancy(
        engine, source="hh", source_id=hostile_source_id, title="Hostile vacancy"
    )
    _seed_apply_job(engine, user_id=user_id, vacancy_id=vacancy_id)

    response = client.get(
        "/dashboard",
        headers={"Accept": "text/html"},
        cookies={SESSION_COOKIE_NAME: token},
    )

    assert response.status_code == 200
    body = response.text
    # The raw <script> tag must NOT appear unescaped.
    assert "<script>alert(1)</script>" not in body
    # The escaped form is allowed (the renderer escapes user input).
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body


# ---------------------------------------------------------------------------
# JSON regression -- the existing endpoint must keep working
# ---------------------------------------------------------------------------


def test_dashboard_json_endpoint_still_works(client: TestClient, engine: Engine) -> None:
    """GET /dashboard with Accept: application/json still returns the JSON summary."""
    token, _user_id = _seed_user_and_session(client, engine, email="frank@example.com")

    response = client.get(
        "/dashboard",
        headers={"Accept": "application/json"},
        cookies={SESSION_COOKIE_NAME: token},
    )

    assert response.status_code == 200
    body = response.json()
    # The DashboardSummaryRead wire shape (test_dashboard.py mirrors these).
    assert "matches_total" in body
    assert "matches_by_status" in body
    assert "applications_total" in body
    assert "applications_by_status" in body
    assert "cover_letter_drafts_total" in body
    assert "search_profiles_active" in body
