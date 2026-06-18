"""TDD tests for the ``cover_letter`` vertical slice (M3, issue #31).

The slice covers exactly one use case: given a :class:`VacancyMatch`,
generate the very first :class:`CoverLetterDraft` using the user's
resume and the vacancy / search-profile / style context. The
version-history / regenerate workflow (issue #32) is intentionally out
of scope here — its tests live in the follow-up slice.

Test surface
------------

The 8 test cases fall into two groups:

* Service tests (5) — exercise the use case end-to-end through
  :class:`CoverLetterService`. Dependencies are collaborator-injected
  in-memory fakes, the LLM is :class:`InMemoryLLMClient` with a
  preloaded response, and the draft repository is the in-memory
  implementation. No ``Mock`` is used.
* Repository tests (3) — verify both the in-memory and the
  SQLAlchemy-backed implementations of the
  :class:`CoverLetterDraftRepository` Protocol.

The SQL tests use a sqlite in-memory engine so they exercise the same
SQL surface as the production path (UNIQUE on ``match_id``, FKs, etc.)
without needing a running Postgres.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, cast

import pytest
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from apply_pilot.db import Base
from apply_pilot.features.cover_letter import (
    CoverLetterDraft,
    CoverLetterDraftRepository,
    CoverLetterDraftStatus,
    CoverLetterService,
    InMemoryCoverLetterDraftRepository,
    SqlCoverLetterDraftRepository,
    build_cover_letter_prompt,
)
from apply_pilot.features.cover_letter_style.models import CoverLetterStyle
from apply_pilot.features.matches.models import VacancyMatch
from apply_pilot.features.matches.repository import InMemoryVacancyMatchRepository
from apply_pilot.features.resumes.models import Resume
from apply_pilot.features.scoring.llm import InMemoryLLMClient
from apply_pilot.features.search_profiles.models import SearchProfile
from apply_pilot.features.sources.models import Vacancy
from apply_pilot.features.users.models import User

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeUserRepo:
    """In-memory user repo providing only ``get_by_id``."""

    users: dict[uuid.UUID, User] = field(default_factory=dict)

    def get_by_id(self, user_id: uuid.UUID) -> User | None:
        return self.users.get(user_id)

    def add(self, user: User) -> User:
        self.users[user.id] = user
        return user


@dataclass
class _FakeVacancyRepo:
    """In-memory vacancy repo providing only ``get_by_id``."""

    vacancies: dict[uuid.UUID, Vacancy] = field(default_factory=dict)

    def get_by_id(self, vacancy_id: uuid.UUID) -> Vacancy | None:
        return self.vacancies.get(vacancy_id)

    def add(self, vacancy: Vacancy) -> Vacancy:
        self.vacancies[vacancy.id] = vacancy
        return vacancy


@dataclass
class _FakeSearchProfileRepo:
    """In-memory search-profile repo providing only ``get_by_id``."""

    profiles: dict[uuid.UUID, SearchProfile] = field(default_factory=dict)

    def get_by_id(self, profile_id: uuid.UUID) -> SearchProfile | None:
        return self.profiles.get(profile_id)

    def add(self, profile: SearchProfile) -> SearchProfile:
        self.profiles[profile.id] = profile
        return profile


@dataclass
class _FakeResumeRepo:
    """In-memory resume repo providing only ``get_active_by_user``.

    "Active" for the purposes of this slice means "the most recently
    created resume for the user" — same semantics the production
    :meth:`ResumesRepository.list_for_user` order provides.
    """

    resumes: list[Resume] = field(default_factory=list)

    def get_active_by_user(self, user_id: uuid.UUID) -> Resume | None:
        owned = [r for r in self.resumes if r.user_id == user_id]
        if not owned:
            return None
        # ``list_for_user`` orders by created_at desc, id desc; the
        # in-memory fake mirrors the same tie-breaker for determinism.
        owned.sort(key=lambda r: (r.created_at, r.id), reverse=True)
        return owned[0]

    def add(self, resume: Resume) -> Resume:
        self.resumes.append(resume)
        return resume


@dataclass
class _FakeStyleRepo:
    """In-memory style repo providing only ``get_by_user``."""

    styles: dict[uuid.UUID, CoverLetterStyle] = field(default_factory=dict)

    def get_by_user(self, user_id: uuid.UUID) -> CoverLetterStyle | None:
        return self.styles.get(user_id)

    def add(self, style: CoverLetterStyle) -> CoverLetterStyle:
        self.styles[style.user_id] = style
        return style


# ---------------------------------------------------------------------------
# Domain fixtures
# ---------------------------------------------------------------------------


@dataclass
class _World:
    """A tiny in-memory world wired up for one test."""

    user: User
    profile: SearchProfile
    vacancy: Vacancy
    match: VacancyMatch
    resume: Resume
    style: CoverLetterStyle

    user_repo: _FakeUserRepo
    vacancy_repo: _FakeVacancyRepo
    profile_repo: _FakeSearchProfileRepo
    resume_repo: _FakeResumeRepo
    style_repo: _FakeStyleRepo
    match_repo: InMemoryVacancyMatchRepository
    draft_repo: InMemoryCoverLetterDraftRepository
    llm: InMemoryLLMClient
    service: CoverLetterService


def _make_world(
    *,
    llm_response: str = "Dear hiring manager,\n\nSincerely,\nThe candidate",
    style_payload: dict[str, Any] | None = None,
    resume_text: str = "I am a senior engineer with 10 years of experience.",
) -> _World:
    """Build a fully-wired test world with a single match + draft slot."""
    user = User(
        id=uuid.uuid4(),
        email="alice@example.com",
        hashed_password="x",
        is_active=True,
    )
    profile = SearchProfile(
        id=uuid.uuid4(),
        user_id=user.id,
        title="Senior Python",
        keywords="python, fastapi, sqlalchemy",
        salary_min=200000,
        salary_max=300000,  # matches the column on the model
        location="Remote",
        schedule="remote",
        is_active=True,
    )
    vacancy = Vacancy(
        id=uuid.uuid4(),
        source="hh",
        source_id="1001",
        title="Senior Python Developer",
        description="Looking for a senior Python developer to join our team.",
        employer_name="Acme",
        location="Moscow",
        schedule="remote",
        experience="5+ years",
        skills=["python", "fastapi", "postgres"],
        raw_data={},
    )
    match = VacancyMatch(
        id=uuid.uuid4(),
        search_profile_id=profile.id,
        vacancy_id=vacancy.id,
        status="accepted",
    )
    # Mirror the production ordering signal with a fresh created_at.
    resume = Resume(
        id=uuid.uuid4(),
        user_id=user.id,
        filename="resume.pdf",
        content_type="application/pdf",
        size=1024,
        raw_text=resume_text,
        plain_text=resume_text,
    )
    style_kwargs = style_payload or {
        "tone": "friendly",
        "length": "medium",
        "focus_areas": ["python", "teamwork"],
        "avoid_phrases": ["rockstar", "ninja"],
        "extra_instructions": "Mention the team size.",
    }
    style = CoverLetterStyle(user_id=user.id, **style_kwargs)

    user_repo = _FakeUserRepo()
    user_repo.add(user)
    vacancy_repo = _FakeVacancyRepo()
    vacancy_repo.add(vacancy)
    profile_repo = _FakeSearchProfileRepo()
    profile_repo.add(profile)
    resume_repo = _FakeResumeRepo()
    resume_repo.add(resume)
    style_repo = _FakeStyleRepo()
    style_repo.add(style)

    match_repo = InMemoryVacancyMatchRepository()
    match_repo.create(match)
    draft_repo = InMemoryCoverLetterDraftRepository()
    llm = InMemoryLLMClient(responses={"*": llm_response})

    service = CoverLetterService(
        llm=llm,
        match_repo=match_repo,
        user_repo=user_repo,  # type: ignore[arg-type]
        vacancy_repo=vacancy_repo,  # type: ignore[arg-type]
        profile_repo=profile_repo,  # type: ignore[arg-type]
        resume_repo=resume_repo,  # type: ignore[arg-type]
        style_repo=style_repo,  # type: ignore[arg-type]
        draft_repo=draft_repo,
    )

    return _World(
        user=user,
        profile=profile,
        vacancy=vacancy,
        match=match,
        resume=resume,
        style=style,
        user_repo=user_repo,
        vacancy_repo=vacancy_repo,
        profile_repo=profile_repo,
        resume_repo=resume_repo,
        style_repo=style_repo,
        match_repo=match_repo,
        draft_repo=draft_repo,
        llm=llm,
        service=service,
    )


# ---------------------------------------------------------------------------
# Service tests
# ---------------------------------------------------------------------------


async def test_create_draft_for_new_match(world: _World) -> None:
    """Generating for a never-seen match creates a fresh draft.

    The service must persist exactly one draft, stamp the match id /
    user id, default the status to ``draft``, and return the same row
    it just stored.
    """
    draft = await world.service.generate_for_match(world.match.id)

    assert draft.id is not None
    assert draft.match_id == world.match.id
    assert draft.user_id == world.user.id
    assert draft.status == CoverLetterDraftStatus.DRAFT.value
    assert draft.content == "Dear hiring manager,\n\nSincerely,\nThe candidate"
    assert draft.prompt_version == "cover_letter@1.0.0"
    assert draft.model_used is None

    # Persisted: the in-memory repo has exactly one row, lookup by
    # match returns it.
    assert world.draft_repo.get_by_match(world.match.id) is not None
    by_id = world.draft_repo.get_by_id(draft.id)
    assert by_id is not None
    assert by_id.id == draft.id


async def test_generate_for_match_calls_llm_with_prompt_containing_style(world: _World) -> None:
    """The prompt fed to the LLM must carry every style preference.

    The service composes the prompt from the vacancy, profile, resume
    and style — without the style fields the LLM has no way to honour
    the user's tone / length / focus / avoid preferences.
    """
    captured: dict[str, str] = {}

    def _capture(prompt: str) -> str:
        captured["prompt"] = prompt
        return "letter body"

    world.llm._responses = {"*": _capture}  # type: ignore[attr-defined]

    await world.service.generate_for_match(world.match.id)

    prompt = captured["prompt"]
    assert world.style.tone in prompt
    assert world.style.length in prompt
    for area in world.style.focus_areas:
        assert area in prompt
    for phrase in world.style.avoid_phrases:
        assert phrase in prompt
    if world.style.extra_instructions:
        assert world.style.extra_instructions in prompt


async def test_generate_for_match_uses_user_resume(world: _World) -> None:
    """The resume plain text must be the LLM's input.

    The service looks up the latest resume for the user and passes its
    ``plain_text`` to the prompt builder.
    """
    captured: dict[str, str] = {}

    def _capture(prompt: str) -> str:
        captured["prompt"] = prompt
        return "letter body"

    world.llm._responses = {"*": _capture}  # type: ignore[attr-defined]

    await world.service.generate_for_match(world.match.id)

    assert world.resume.plain_text in captured["prompt"]


async def test_generate_for_match_is_idempotent(world: _World) -> None:
    """Calling ``generate_for_match`` twice must return the same draft row.

    The ``match_id`` UNIQUE constraint backs this contract at the
    storage level; the service upserts the existing row's ``content``
    on the second call rather than creating a duplicate.
    """
    first = await world.service.generate_for_match(world.match.id)

    # Swap the LLM response so the second call would produce a
    # different body — the persisted row must reflect the *new* body
    # but keep the same id.
    world.llm._responses = {"*": "second body"}  # type: ignore[attr-defined]
    second = await world.service.generate_for_match(world.match.id)

    assert first.id == second.id
    assert second.content == "second body"
    # The repo must hold exactly one row for this match.
    assert world.draft_repo.get_by_match(world.match.id) is not None
    listed = list(world.draft_repo.list_by_user(world.user.id))
    assert len(listed) == 1


async def test_generate_for_match_persists_draft(world: _World) -> None:
    """After generation, ``draft_repo.get_by_match`` finds the row.

    The persistence path goes through the injected repository, so this
    test would also fail if the service forgot to call ``create``.
    """
    draft = await world.service.generate_for_match(world.match.id)

    persisted = world.draft_repo.get_by_match(world.match.id)
    assert persisted is not None
    assert persisted.id == draft.id
    assert persisted.user_id == draft.user_id
    assert persisted.content == draft.content
    assert persisted.prompt_version == draft.prompt_version
    # list_by_user must include the new row.
    listed = list(world.draft_repo.list_by_user(world.user.id))
    assert [d.id for d in listed] == [draft.id]


async def test_prompt_includes_vacancy_and_profile_fields(world: _World) -> None:
    """The prompt must surface the vacancy and search-profile context.

    The LLM cannot personalise a cover letter without the title /
    description / employer / location from the vacancy and the title /
    keywords / salary / location from the search profile.
    """
    captured: dict[str, str] = {}

    def _capture(prompt: str) -> str:
        captured["prompt"] = prompt
        return "letter body"

    world.llm._responses = {"*": _capture}  # type: ignore[attr-defined]

    await world.service.generate_for_match(world.match.id)

    prompt = captured["prompt"]
    # Vacancy fields.
    assert world.vacancy.title in prompt
    assert world.vacancy.description in prompt
    assert world.vacancy.employer_name in prompt
    assert world.vacancy.location in prompt
    # Search-profile fields.
    assert world.profile.title in prompt
    assert world.profile.keywords in prompt
    if world.profile.salary_min is not None:
        assert str(world.profile.salary_min) in prompt


def test_build_cover_letter_prompt_isolated_function() -> None:
    """``build_cover_letter_prompt`` works without the LLM or service.

    The prompt builder is a pure function — it must compose a stable
    string from the four inputs (vacancy, profile, resume, style) so
    callers can preview / diff prompts in tests and tooling.
    """
    vacancy = Vacancy(
        id=uuid.uuid4(),
        source="hh",
        source_id="x",
        title="Staff Engineer",
        description="Build things.",
        raw_data={},
    )
    profile = SearchProfile(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        title="Staff Eng",
        keywords="go, kubernetes",
        is_active=True,
    )
    style = CoverLetterStyle(
        user_id=profile.user_id,
        tone="concise",
        length="short",
        focus_areas=["go"],
        avoid_phrases=["guru"],
    )

    prompt = build_cover_letter_prompt(
        vacancy=vacancy,
        profile=profile,
        resume_text="I have written Go for 8 years.",
        style=style,
    )

    assert "Staff Engineer" in prompt
    assert "Build things." in prompt
    assert "Staff Eng" in prompt
    assert "go, kubernetes" in prompt
    assert "concise" in prompt
    assert "short" in prompt
    assert "go" in prompt
    assert "guru" in prompt
    assert "I have written Go for 8 years." in prompt


# ---------------------------------------------------------------------------
# Repository tests
# ---------------------------------------------------------------------------


def test_repository_in_memory_create_get_update() -> None:
    """The in-memory repo honours the Protocol contract.

    Covers: ``create`` assigns id, ``get_by_match`` finds the row by
    match id, ``get_by_id`` round-trips, ``list_by_user`` filters by
    user, and ``update_status`` mutates the status in place.
    """
    repo: CoverLetterDraftRepository = InMemoryCoverLetterDraftRepository()
    user_id = uuid.uuid4()
    match_id = uuid.uuid4()
    draft = CoverLetterDraft(
        match_id=match_id,
        user_id=user_id,
        content="hello",
        prompt_version="cover_letter@1.0.0",
    )

    created = repo.create(draft)
    assert created.id is not None
    assert created.status == CoverLetterDraftStatus.DRAFT.value

    assert repo.get_by_id(created.id) is not None
    assert repo.get_by_match(match_id) is not None
    assert [d.id for d in repo.list_by_user(user_id)] == [created.id]
    # Filtering by status still returns the same row.
    assert [d.id for d in repo.list_by_user(user_id, status="draft")] == [created.id]
    assert repo.list_by_user(user_id, status="archived") == []

    updated = repo.update_status(created.id, CoverLetterDraftStatus.FINAL.value)
    assert updated.status == CoverLetterDraftStatus.FINAL.value
    # The change is visible through the read paths.
    assert repo.get_by_id(created.id).status == CoverLetterDraftStatus.FINAL.value  # type: ignore[union-attr]


@pytest.fixture
def engine() -> Iterator[Engine]:
    """Fresh in-memory sqlite engine per test with all tables created."""
    eng = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # Register the cover_letter model on the declarative base.
    from apply_pilot.features.cover_letter import models  # noqa: F401

    Base.metadata.create_all(bind=eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> Iterator[sessionmaker[Session]]:
    factory = sessionmaker(bind=engine, class_=Session, autocommit=False, autoflush=False)
    yield factory


@pytest.fixture
def sql_repo(
    session_factory: sessionmaker[Session],
) -> SqlCoverLetterDraftRepository:
    return SqlCoverLetterDraftRepository(session_factory=session_factory)


def test_repository_sql_create_get_update(sql_repo: SqlCoverLetterDraftRepository) -> None:
    """The SQL repo honours the same Protocol on a sqlite in-memory db.

    The ``UNIQUE(match_id)`` constraint is the M3 #31 contract; the
    test exercises create / get_by_id / get_by_match / list_by_user /
    update_status in a single transaction to catch any drift between
    the in-memory and the SQL surface.
    """
    user_id = uuid.uuid4()
    match_id = uuid.uuid4()
    draft = CoverLetterDraft(
        match_id=match_id,
        user_id=user_id,
        content="sql body",
        prompt_version="cover_letter@1.0.0",
    )

    created = sql_repo.create(draft)
    assert created.id is not None
    assert created.status == CoverLetterDraftStatus.DRAFT.value

    fetched_by_id = sql_repo.get_by_id(created.id)
    assert fetched_by_id is not None
    assert fetched_by_id.id == created.id
    assert fetched_by_id.content == "sql body"

    fetched_by_match = sql_repo.get_by_match(match_id)
    assert fetched_by_match is not None
    assert fetched_by_match.id == created.id

    listed = list(sql_repo.list_by_user(user_id))
    assert [d.id for d in listed] == [created.id]
    assert list(sql_repo.list_by_user(user_id, status="draft"))[0].id == created.id
    assert sql_repo.list_by_user(user_id, status="archived") == []

    updated = sql_repo.update_status(created.id, CoverLetterDraftStatus.FINAL.value)
    assert updated.status == CoverLetterDraftStatus.FINAL.value
    assert sql_repo.get_by_id(created.id).status == CoverLetterDraftStatus.FINAL.value  # type: ignore[union-attr]


def test_repository_sql_update_content(sql_repo: SqlCoverLetterDraftRepository) -> None:
    """``update_content`` mutates the row in place (issue #144).

    The SQL repo's ``get_by_match`` returns a detached instance, so the
    pre-fix service code's direct attribute writes on that instance
    were silently lost when its session closed. ``update_content``
    must therefore re-fetch the row in its own session, mutate, and
    commit. This test exercises the repo in isolation against a real
    sqlite in-memory engine.
    """
    user_id = uuid.uuid4()
    match_id = uuid.uuid4()
    created = sql_repo.create(
        CoverLetterDraft(
            match_id=match_id,
            user_id=user_id,
            content="first body",
            prompt_version="cover_letter@1.0.0",
            model_used="m0",
        )
    )

    updated = sql_repo.update_content(
        match_id=match_id,
        content="second body",
        prompt_version="cover_letter@1.0.1",
        model_used="m1",
    )

    assert updated is not None
    assert updated.id == created.id
    assert updated.content == "second body"
    assert updated.prompt_version == "cover_letter@1.0.1"
    assert updated.model_used == "m1"
    assert updated.updated_at is not None

    # The change is durable — a fresh ``get_by_match`` reads the new
    # content, not the original.
    fetched = sql_repo.get_by_match(match_id)
    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.content == "second body"
    assert fetched.prompt_version == "cover_letter@1.0.1"
    assert fetched.model_used == "m1"
    # No second row was inserted.
    listed = sql_repo.list_by_user(user_id)
    assert [d.id for d in listed] == [fetched.id]

    # Missing match → ``None``, not an error.
    assert sql_repo.update_content(uuid.uuid4(), "x", "cover_letter@1.0.0", None) is None


def test_repository_in_memory_update_content() -> None:
    """The in-memory ``update_content`` mirrors the SQL contract."""
    repo: CoverLetterDraftRepository = InMemoryCoverLetterDraftRepository()
    user_id = uuid.uuid4()
    match_id = uuid.uuid4()
    created = repo.create(
        CoverLetterDraft(
            match_id=match_id,
            user_id=user_id,
            content="first body",
            prompt_version="cover_letter@1.0.0",
        )
    )

    updated = repo.update_content(
        match_id=match_id,
        content="second body",
        prompt_version="cover_letter@1.0.1",
        model_used="m1",
    )

    assert updated is not None
    assert updated.id == created.id
    assert updated.content == "second body"
    assert repo.get_by_match(match_id).content == "second body"  # type: ignore[union-attr]
    assert repo.update_content(uuid.uuid4(), "x", "cover_letter@1.0.0", None) is None


# ---------------------------------------------------------------------------
# Service regression — issue #144
# ---------------------------------------------------------------------------


def _make_sql_world(
    *,
    llm_response: str = "Dear hiring manager,\n\nSincerely,\nThe candidate",
) -> _World:
    """Build a ``_World`` whose ``draft_repo`` is a real SQL repository.

    Mirrors ``_make_world`` but uses the sqlite-backed
    :class:`SqlCoverLetterDraftRepository` so the test exercises the
    detached-session code path that issue #144 was hiding.
    """
    user = User(
        id=uuid.uuid4(),
        email="sql@example.com",
        hashed_password="x",
        is_active=True,
    )
    profile = SearchProfile(
        id=uuid.uuid4(),
        user_id=user.id,
        title="Senior Python",
        keywords="python, fastapi",
        is_active=True,
    )
    vacancy = Vacancy(
        id=uuid.uuid4(),
        source="hh",
        source_id="1001",
        title="Senior Python Developer",
        description="Looking for a senior Python developer to join our team.",
        employer_name="Acme",
        location="Moscow",
        schedule="remote",
        experience="5+ years",
        skills=["python"],
        raw_data={},
    )
    match = VacancyMatch(
        id=uuid.uuid4(),
        search_profile_id=profile.id,
        vacancy_id=vacancy.id,
        status="accepted",
    )
    resume = Resume(
        id=uuid.uuid4(),
        user_id=user.id,
        filename="resume.pdf",
        content_type="application/pdf",
        size=1024,
        raw_text="I am a senior engineer.",
        plain_text="I am a senior engineer.",
    )
    style = CoverLetterStyle(
        user_id=user.id,
        tone="friendly",
        length="medium",
        focus_areas=["python"],
        avoid_phrases=[],
    )

    user_repo = _FakeUserRepo()
    user_repo.add(user)
    vacancy_repo = _FakeVacancyRepo()
    vacancy_repo.add(vacancy)
    profile_repo = _FakeSearchProfileRepo()
    profile_repo.add(profile)
    resume_repo = _FakeResumeRepo()
    resume_repo.add(resume)
    style_repo = _FakeStyleRepo()
    style_repo.add(style)

    match_repo = InMemoryVacancyMatchRepository()
    match_repo.create(match)
    llm = InMemoryLLMClient(responses={"*": llm_response})

    eng = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=eng)
    factory = sessionmaker(bind=eng, class_=Session, autocommit=False, autoflush=False)
    draft_repo = SqlCoverLetterDraftRepository(session_factory=factory)

    service = CoverLetterService(
        llm=llm,
        match_repo=match_repo,
        user_repo=user_repo,  # type: ignore[arg-type]
        vacancy_repo=vacancy_repo,  # type: ignore[arg-type]
        profile_repo=profile_repo,  # type: ignore[arg-type]
        resume_repo=resume_repo,  # type: ignore[arg-type]
        style_repo=style_repo,  # type: ignore[arg-type]
        draft_repo=draft_repo,
    )

    return _World(
        user=user,
        profile=profile,
        vacancy=vacancy,
        match=match,
        resume=resume,
        style=style,
        user_repo=user_repo,
        vacancy_repo=vacancy_repo,
        profile_repo=profile_repo,
        resume_repo=resume_repo,
        style_repo=style_repo,
        match_repo=match_repo,
        draft_repo=draft_repo,  # type: ignore[arg-type]
        llm=llm,
        service=service,
    )


async def test_generate_for_match_persists_content_update_via_sql_repo() -> None:
    """Issue #144 regression.

    ``generate_for_match`` must persist a refreshed body for a match
    that already has a draft, and must not insert a second row. The
    bug was that the service mutated the detached instance returned by
    :meth:`SqlCoverLetterDraftRepository.get_by_match`, so the change
    was silently lost.
    """
    world = _make_sql_world()

    first = await world.service.generate_for_match(world.match.id)
    assert first.id is not None
    assert first.content == "Dear hiring manager,\n\nSincerely,\nThe candidate"

    # Swap the LLM response so the second call would produce a
    # different body.
    world.llm._responses = {"*": "second body"}  # type: ignore[attr-defined]
    second = await world.service.generate_for_match(world.match.id)

    # Same row, refreshed body.
    assert second.id == first.id
    assert second.content == "second body"

    # A fresh read through the SQL repo must see the updated content
    # — this is the assertion that fails against the pre-fix code.
    sql_draft_repo = cast(SqlCoverLetterDraftRepository, world.draft_repo)
    fresh = sql_draft_repo.get_by_match(world.match.id)
    assert fresh is not None
    assert fresh.id == first.id
    assert fresh.content == "second body"

    # And there is still exactly one draft for the user — the
    # service must upsert, not insert.
    listed = sql_draft_repo.list_by_user(world.user.id)
    assert [d.id for d in listed] == [fresh.id]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def world() -> _World:
    return _make_world()
