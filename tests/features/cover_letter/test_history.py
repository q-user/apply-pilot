"""History & regeneration behaviour for the ``cover_letter`` slice (M3, issue #32).

These tests exercise the version-history surface of
:class:`CoverLetterService` and the corresponding ``/cover-letters/*``
HTTP endpoints. The service tests use an in-memory repository and a
fake generator (no network, no LLM, no DB) so they are fast and
hermetic; the API test wires the real FastAPI router to a sqlite
in-memory engine to validate the integration.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from job_apply.db import Base, get_db
from job_apply.features.cover_letter.generator import CoverLetterGenerator
from job_apply.features.cover_letter.repository import (
    CoverLetterDraftRepository,
    InMemoryCoverLetterDraftRepository,
)
from job_apply.features.cover_letter.service import CoverLetterService
from job_apply.features.users.security import default_token_store

# ---------------------------------------------------------------------------
# Fakes & fixtures
# ---------------------------------------------------------------------------


class FakeGenerator(CoverLetterGenerator):
    """A deterministic generator that stamps the call count into the body.

    Real generation is not in scope for this slice — issue #31 owns the
    LLM-backed :class:`CoverLetterGenerator`. These tests just need a
    stable placeholder so we can assert that successive calls produce
    distinct drafts and the bookkeeping is right.
    """

    def __init__(self) -> None:
        self.call_count = 0
        self.last_style: str | None = None
        self.last_user_comment: str | None = None

    def generate(
        self,
        match_id: uuid.UUID,
        *,
        style: str | None = None,
        user_comment: str | None = None,
    ) -> str:
        self.call_count += 1
        self.last_style = style
        self.last_user_comment = user_comment
        return (
            f"Generated cover letter for match {match_id} "
            f"(call #{self.call_count}, style={style!r})."
        )


@pytest.fixture
def fake_generator() -> FakeGenerator:
    return FakeGenerator()


@pytest.fixture
def user_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def repo() -> InMemoryCoverLetterDraftRepository:
    return InMemoryCoverLetterDraftRepository()


@pytest.fixture
def service(
    repo: InMemoryCoverLetterDraftRepository,
    fake_generator: FakeGenerator,
) -> CoverLetterService:
    return CoverLetterService(repo, fake_generator)


@pytest.fixture
def match_id() -> uuid.UUID:
    return uuid.uuid4()


# ---------------------------------------------------------------------------
# Version 1 (first draft)
# ---------------------------------------------------------------------------


def test_first_draft_is_version_1(
    service: CoverLetterService,
    repo: CoverLetterDraftRepository,
    user_id: uuid.UUID,
    match_id: uuid.UUID,
) -> None:
    """The first call to ``generate_for_match`` produces a single version-1 draft.

    A match with no history must yield exactly one draft, with
    ``version == 1`` and no parent / no replacement pointer.
    """
    draft = service.generate_for_match(match_id, user_id=user_id)

    assert draft.match_id == match_id
    assert draft.version == 1
    assert draft.parent_draft_id is None
    assert draft.replaced_by_id is None
    assert draft.text  # non-empty
    # Exactly one draft exists in the repo for this match.
    history = repo.list_by_match(match_id)
    assert len(history) == 1
    assert history[0].id == draft.id


# ---------------------------------------------------------------------------
# Regeneration produces a new version
# ---------------------------------------------------------------------------


def test_regenerate_creates_version_2_with_parent(
    service: CoverLetterService,
    repo: CoverLetterDraftRepository,
    user_id: uuid.UUID,
    match_id: uuid.UUID,
) -> None:
    """Regenerating creates version 2 and points it at version 1."""
    v1 = service.generate_for_match(match_id, user_id=user_id)
    v2 = service.regenerate_for_match(match_id, user_id=user_id, user_comment="more punchy")

    assert v2.version == 2
    assert v2.parent_draft_id == v1.id
    # The version-1 draft must now know it was replaced.
    refreshed_v1 = repo.get_by_id(v1.id)
    assert refreshed_v1 is not None
    assert refreshed_v1.replaced_by_id == v2.id


# ---------------------------------------------------------------------------
# Latest-for-match lookup
# ---------------------------------------------------------------------------


def test_get_latest_for_match_returns_highest_version(
    service: CoverLetterService,
    user_id: uuid.UUID,
    match_id: uuid.UUID,
) -> None:
    """``get_latest_for_match`` returns the highest ``version`` row."""
    service.generate_for_match(match_id, user_id=user_id)
    service.regenerate_for_match(match_id, user_id=user_id)
    third = service.regenerate_for_match(match_id, user_id=user_id)

    latest = service.get_latest_for_match(match_id, user_id=user_id)
    assert latest is not None
    assert latest.id == third.id
    assert latest.version == 3


def test_get_latest_for_match_returns_none_when_no_history(
    service: CoverLetterService,
    user_id: uuid.UUID,
    match_id: uuid.UUID,
) -> None:
    """A match with no drafts must yield ``None`` (not raise)."""
    assert service.get_latest_for_match(match_id, user_id=user_id) is None


# ---------------------------------------------------------------------------
# History listing
# ---------------------------------------------------------------------------


def test_get_history_returns_all_versions_newest_first(
    service: CoverLetterService,
    user_id: uuid.UUID,
    match_id: uuid.UUID,
) -> None:
    """``get_history_for_match`` returns every draft, newest version first."""
    v1 = service.generate_for_match(match_id, user_id=user_id)
    v2 = service.regenerate_for_match(match_id, user_id=user_id)
    v3 = service.regenerate_for_match(match_id, user_id=user_id)

    history = service.get_history_for_match(match_id, user_id=user_id)
    assert [d.version for d in history] == [3, 2, 1]
    assert [d.id for d in history] == [v3.id, v2.id, v1.id]


def test_get_history_for_empty_match_is_empty_list(
    service: CoverLetterService,
    user_id: uuid.UUID,
    match_id: uuid.UUID,
) -> None:
    """A match with no history must yield an empty list, not ``None``."""
    assert service.get_history_for_match(match_id, user_id=user_id) == []


# ---------------------------------------------------------------------------
# Replaced-by pointer
# ---------------------------------------------------------------------------


def test_previous_version_has_replaced_by_pointer(
    service: CoverLetterService,
    repo: CoverLetterDraftRepository,
    user_id: uuid.UUID,
    match_id: uuid.UUID,
) -> None:
    """After a regeneration, the previous draft's ``replaced_by_id`` points at the new one."""
    first = service.generate_for_match(match_id, user_id=user_id)
    second = service.regenerate_for_match(match_id, user_id=user_id, user_comment="less formal")
    third = service.regenerate_for_match(match_id, user_id=user_id, user_comment="add metrics")

    # The chain forms a doubly-linked list: prev.replaced_by == next and
    # next.parent_draft_id == prev. We assert both directions for the
    # three middle/edge cases.
    fresh_first = repo.get_by_id(first.id)
    fresh_second = repo.get_by_id(second.id)
    fresh_third = repo.get_by_id(third.id)
    assert fresh_first is not None
    assert fresh_second is not None
    assert fresh_third is not None

    # first: replaced by second, no further replacement, no parent
    assert fresh_first.parent_draft_id is None
    assert fresh_first.replaced_by_id == fresh_second.id

    # second: parent = first, replaced by third
    assert fresh_second.parent_draft_id == fresh_first.id
    assert fresh_second.replaced_by_id == fresh_third.id

    # third: parent = second, no replacement (it's the latest)
    assert fresh_third.parent_draft_id == fresh_second.id
    assert fresh_third.replaced_by_id is None


def test_generator_receives_user_comment_on_regenerate(
    service: CoverLetterService,
    fake_generator: FakeGenerator,
    user_id: uuid.UUID,
    match_id: uuid.UUID,
) -> None:
    """Regeneration forwards the ``user_comment`` to the generator."""
    service.generate_for_match(match_id, user_id=user_id)
    service.regenerate_for_match(match_id, user_id=user_id, user_comment="make it warmer")

    assert fake_generator.last_user_comment == "make it warmer"


# ---------------------------------------------------------------------------
# Generator output flows into the draft
# ---------------------------------------------------------------------------


def test_draft_text_matches_generator_output(
    service: CoverLetterService,
    user_id: uuid.UUID,
    match_id: uuid.UUID,
) -> None:
    """The persisted draft's ``text`` is exactly what the generator returned."""
    draft = service.generate_for_match(match_id, user_id=user_id, style="friendly")
    assert draft.text.startswith("Generated cover letter for match ")


# ---------------------------------------------------------------------------
# API integration test
# ---------------------------------------------------------------------------


def _register_and_login(client: TestClient, email: str, password: str) -> tuple[str, uuid.UUID]:
    resp = client.post("/auth/register", json={"email": email, "password": password})
    assert resp.status_code == 201, resp.json()
    resp = client.post("/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.json()
    token = resp.json()["access_token"]
    user_id = uuid.UUID(default_token_store().resolve(token))
    return token, user_id


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # Register all models so the integration test sees every table it needs.
    from job_apply.features.audit import models as _audit_models  # noqa: F401
    from job_apply.features.cover_letter import models as _cl_models  # noqa: F401
    from job_apply.features.cover_letter_style import models as _cls_models  # noqa: F401
    from job_apply.features.hh import models as _hh_models  # noqa: F401
    from job_apply.features.matches import models as _match_models  # noqa: F401
    from job_apply.features.quick_filter.persistence import (
        FilterDecisionRow as _qf_model,  # noqa: F401
    )
    from job_apply.features.resumes import models as _resume_models  # noqa: F401
    from job_apply.features.scoring import models as _scoring_models  # noqa: F401
    from job_apply.features.search_profiles import models as _sp_models  # noqa: F401
    from job_apply.features.sources import models as _source_models  # noqa: F401
    from job_apply.features.telegram import models as _tg_models  # noqa: F401
    from job_apply.features.users import models as _user_models  # noqa: F401

    Base.metadata.create_all(bind=eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def app(engine: Engine) -> Iterator[FastAPI]:
    from job_apply.features.cover_letter import api as cover_letter_api
    from job_apply.features.cover_letter.api import router as cover_letter_router
    from job_apply.features.cover_letter.repository import (
        SqlCoverLetterDraftRepository,
    )
    from job_apply.features.users.api import router as auth_router

    factory = sessionmaker(bind=engine, class_=Session, autocommit=False, autoflush=False)

    def _override_get_db() -> Iterator[Session]:
        session = factory()
        try:
            yield session
        finally:
            session.close()

    # Replace the cover-letter generator with a deterministic fake so
    # the API test does not call the LLM. We still use the real SQL
    # repository so the round-trip is end-to-end.
    fake_gen = FakeGenerator()

    def _override_cover_letter_service(
        session: Session = Depends(get_db),  # noqa: B008
    ) -> CoverLetterService:
        repo = SqlCoverLetterDraftRepository(session=session)
        return CoverLetterService(repo, fake_gen)

    application = FastAPI()
    application.include_router(auth_router)
    application.include_router(cover_letter_router)
    application.dependency_overrides[get_db] = _override_get_db
    application.dependency_overrides[cover_letter_api.get_cover_letter_service] = (
        _override_cover_letter_service
    )
    try:
        yield application
    finally:
        application.dependency_overrides.clear()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def _seed_match_for_user(session: Session, user_id: uuid.UUID) -> uuid.UUID:
    """Insert a minimal SearchProfile + Vacancy + VacancyMatch for the user."""
    from job_apply.features.matches.models import VacancyMatch
    from job_apply.features.search_profiles.models import SearchProfile
    from job_apply.features.sources.models import Vacancy

    profile = SearchProfile(
        user_id=user_id,
        title="Backend Engineer",
        keywords="python, fastapi",
    )
    session.add(profile)
    session.flush()

    vacancy = Vacancy(
        source="hh",
        source_id="hh-123",
        title="Senior Python Developer",
        employer_name="Acme",
        description="",
        url="https://hh.ru/vacancy/123",
        location="Remote",
        raw_data={"id": "hh-123", "name": "Senior Python Developer"},
        content_hash="hash-123",
    )
    session.add(vacancy)
    session.flush()

    match = VacancyMatch(
        search_profile_id=profile.id,
        vacancy_id=vacancy.id,
    )
    session.add(match)
    session.commit()
    return match.id


def test_api_history_endpoint_returns_versions(
    client: TestClient,
    engine: Engine,
) -> None:
    """The HTTP surface returns the full version history newest-first."""
    token, user_id = _register_and_login(client, "cl-user@example.com", "hunter2!!")

    # Seed a match in the same in-memory engine the app uses.
    factory = sessionmaker(bind=engine, class_=Session, autocommit=False, autoflush=False)
    with factory() as session:
        match_id = _seed_match_for_user(session, user_id)

    headers = {"Authorization": f"Bearer {token}"}

    # First draft.
    resp = client.post(f"/cover-letters/regenerate/{match_id}", headers=headers)
    assert resp.status_code == 200, resp.json()
    body_v1 = resp.json()
    assert body_v1["version"] == 1
    assert body_v1["match_id"] == str(match_id)

    # Second draft via regenerate.
    resp = client.post(f"/cover-letters/regenerate/{match_id}", headers=headers)
    assert resp.status_code == 200, resp.json()
    body_v2 = resp.json()
    assert body_v2["version"] == 2
    assert body_v2["parent_draft_id"] == body_v1["id"]

    # History endpoint returns both, newest first.
    resp = client.get(f"/cover-letters/by-match/{match_id}/history", headers=headers)
    assert resp.status_code == 200, resp.json()
    history = resp.json()
    assert [d["version"] for d in history] == [2, 1]

    # Latest endpoint returns version 2.
    resp = client.get(f"/cover-letters/by-match/{match_id}", headers=headers)
    assert resp.status_code == 200, resp.json()
    latest = resp.json()
    assert latest["version"] == 2
    assert latest["id"] == body_v2["id"]


def test_api_generate_endpoint_creates_first_draft(
    client: TestClient,
    engine: Engine,
) -> None:
    """POSTing to the regenerate endpoint on an empty match creates version 1."""
    token, user_id = _register_and_login(client, "cl-first@example.com", "hunter2!!")

    factory = sessionmaker(bind=engine, class_=Session, autocommit=False, autoflush=False)
    with factory() as session:
        match_id = _seed_match_for_user(session, user_id)

    headers = {"Authorization": f"Bearer {token}"}
    resp = client.post(f"/cover-letters/regenerate/{match_id}", headers=headers)
    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["version"] == 1
    assert body["parent_draft_id"] is None
    assert body["replaced_by_id"] is None
