"""TDD tests for the :class:`RegenerateActionHandler` (M4, issue #40)."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

import pytest

from job_apply.features.audit.models import AuditEventType
from job_apply.features.audit.repository import InMemoryAuditLogRepository
from job_apply.features.audit.service import AuditService
from job_apply.features.cover_letter.models import (
    CoverLetterDraft,
    CoverLetterDraftStatus,
)
from job_apply.features.cover_letter.repository import InMemoryCoverLetterDraftRepository
from job_apply.features.cover_letter.service import CoverLetterService
from job_apply.features.matches.models import MatchStatus, VacancyMatch
from job_apply.features.matches.repository import InMemoryVacancyMatchRepository
from job_apply.features.resumes.models import Resume
from job_apply.features.scoring.llm import InMemoryLLMClient
from job_apply.features.search_profiles.models import SearchProfile
from job_apply.features.search_profiles.repository import InMemorySearchProfileRepository
from job_apply.features.sources.models import Vacancy
from job_apply.features.telegram.actions.regenerate import (
    RegenerateActionHandler,
    _escape_markdownv2,
    parse_regenerate_command,
)
from job_apply.features.telegram.bot import TelegramBot, TelegramSettings
from job_apply.features.telegram.dto import SendMessageRequest
from job_apply.features.telegram.repository import InMemoryTelegramAccountRepository
from job_apply.features.users.models import User


@dataclass
class _FakeUserRepo:
    users: dict[uuid.UUID, User] = field(default_factory=dict)

    def get_by_id(self, user_id: uuid.UUID) -> User | None:
        return self.users.get(user_id)

    def add(self, user: User) -> User:
        self.users[user.id] = user
        return user


@dataclass
class _FakeVacancyRepo:
    vacancies: dict[uuid.UUID, Vacancy] = field(default_factory=dict)

    def get_by_id(self, vacancy_id: uuid.UUID) -> Vacancy | None:
        return self.vacancies.get(vacancy_id)

    def add(self, vacancy: Vacancy) -> Vacancy:
        self.vacancies[vacancy.id] = vacancy
        return vacancy


@dataclass
class _FakeResumeRepo:
    resumes: list[Resume] = field(default_factory=list)

    def get_active_by_user(self, user_id: uuid.UUID) -> Resume | None:
        owned = [r for r in self.resumes if r.user_id == user_id]
        if not owned:
            return None
        owned.sort(key=lambda r: (r.created_at, r.id), reverse=True)
        return owned[0]

    def add(self, resume: Resume) -> Resume:
        self.resumes.append(resume)
        return resume


@dataclass
class _FakeStyleRepo:
    styles: dict[uuid.UUID, object] = field(default_factory=dict)

    def get_by_user(self, user_id: uuid.UUID):
        return self.styles.get(user_id)

    def add(self, style: object) -> object:
        from job_apply.features.cover_letter_style.models import CoverLetterStyle

        assert isinstance(style, CoverLetterStyle)
        self.styles[style.user_id] = style
        return style


@dataclass
class _World:
    user: User
    profile: SearchProfile
    vacancy: Vacancy
    match: VacancyMatch
    resume: Resume
    draft: CoverLetterDraft
    user_repo: _FakeUserRepo
    vacancy_repo: _FakeVacancyRepo
    profile_repo: InMemorySearchProfileRepository
    resume_repo: _FakeResumeRepo
    style_repo: _FakeStyleRepo
    match_repo: InMemoryVacancyMatchRepository
    draft_repo: InMemoryCoverLetterDraftRepository
    llm: InMemoryLLMClient
    service: CoverLetterService
    telegram_account_repo: InMemoryTelegramAccountRepository
    audit_repo: InMemoryAuditLogRepository
    audit_service: AuditService


def _make_world(
    *,
    initial_content: str = "Dear hiring manager, Sincerely, The candidate",
    regenerate_content: str = "Dear hiring team, Excited to apply. Sincerely, The candidate",
) -> _World:
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
        keywords="python",
        salary_min=200000,
        salary_max=300000,
        location="Remote",
        schedule="remote",
        is_active=True,
    )
    vacancy = Vacancy(
        id=uuid.uuid4(),
        source="hh",
        source_id="2001",
        title="Senior Python Developer",
        description="Looking for a senior Python developer.",
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
        status=MatchStatus.REVIEW.value,
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
    from job_apply.features.cover_letter_style.models import CoverLetterStyle

    style = CoverLetterStyle(
        user_id=user.id,
        tone="friendly",
        length="medium",
        focus_areas=["python"],
        avoid_phrases=[],
        extra_instructions="",
    )

    user_repo = _FakeUserRepo()
    user_repo.add(user)
    vacancy_repo = _FakeVacancyRepo()
    vacancy_repo.add(vacancy)
    profile_repo = InMemorySearchProfileRepository()
    profile_repo.create(profile)
    resume_repo = _FakeResumeRepo()
    resume_repo.add(resume)
    style_repo = _FakeStyleRepo()
    style_repo.add(style)

    match_repo = InMemoryVacancyMatchRepository()
    match_repo.create(match)
    draft_repo = InMemoryCoverLetterDraftRepository()
    draft = CoverLetterDraft(
        id=uuid.uuid4(),
        match_id=match.id,
        user_id=user.id,
        content=initial_content,
        prompt_version="cover_letter@1.0.0",
        status=CoverLetterDraftStatus.DRAFT.value,
        version=1,
    )
    draft_repo.create(draft)

    def _responder(_prompt: str) -> str:
        # The service only calls the LLM once for the regeneration
        # (the initial draft was pre-seeded in the repo). The
        # callable always returns the fresh content.
        return regenerate_content

    llm = InMemoryLLMClient(responses={"*": _responder})

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

    telegram_account_repo = InMemoryTelegramAccountRepository()
    audit_repo = InMemoryAuditLogRepository()
    audit_service = AuditService(audit_repo=audit_repo)

    return _World(
        user=user,
        profile=profile,
        vacancy=vacancy,
        match=match,
        resume=resume,
        draft=draft,
        user_repo=user_repo,
        vacancy_repo=vacancy_repo,
        profile_repo=profile_repo,
        resume_repo=resume_repo,
        style_repo=style_repo,
        match_repo=match_repo,
        draft_repo=draft_repo,
        llm=llm,
        service=service,
        telegram_account_repo=telegram_account_repo,
        audit_repo=audit_repo,
        audit_service=audit_service,
    )


def _link_telegram(
    repo: InMemoryTelegramAccountRepository,
    *,
    user_id: uuid.UUID,
    telegram_user_id: int,
) -> None:
    repo.create(user_id=user_id, telegram_user_id=telegram_user_id, username="alice")


@pytest.fixture
def other_user_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def telegram_user_id() -> int:
    return 818181


@pytest.fixture
def world() -> _World:
    return _make_world()


@pytest.fixture
def handler(world: _World) -> RegenerateActionHandler:
    return RegenerateActionHandler(
        cover_letter_service=world.service,
        telegram_account_repo=world.telegram_account_repo,
        audit_service=world.audit_service,
        profile_repo=world.profile_repo,
    )


# parse_regenerate_command


def test_regenerate_command_parses_match_id() -> None:
    match_id = "11111111-1111-1111-1111-111111111111"
    command = parse_regenerate_command(f"/regenerate {match_id}")
    assert command is not None
    assert command.match_id == uuid.UUID(match_id)
    assert command.raw_args == match_id


def test_regenerate_command_no_args_returns_help() -> None:
    assert parse_regenerate_command("/regenerate") is None
    assert parse_regenerate_command("/regenerate   ") is None


def test_regenerate_command_with_invalid_uuid_returns_none() -> None:
    assert parse_regenerate_command("/regenerate not-a-uuid") is None


# RegenerateActionHandler.handle


async def test_handle_regenerates_cover_letter_for_owner(
    handler: RegenerateActionHandler,
    world: _World,
    telegram_user_id: int,
) -> None:
    _link_telegram(
        world.telegram_account_repo, user_id=world.user.id, telegram_user_id=telegram_user_id
    )
    pre = world.draft_repo.get_by_match(world.match.id)
    assert pre is not None
    pre_version = pre.version
    pre_content = pre.content

    response = await handler.handle(
        chat_id=200,
        telegram_user_id=telegram_user_id,
        command=parse_regenerate_command(f"/regenerate {world.match.id}"),
    )

    assert isinstance(response, SendMessageRequest)
    assert response.chat_id == 200
    assert "Dear hiring team" in response.text
    assert pre_content not in response.text
    after = world.draft_repo.get_by_match(world.match.id)
    assert after is not None
    assert after.id == pre.id
    assert after.content != pre_content
    assert after.version == pre_version + 1


async def test_handle_bumps_version_on_each_regenerate(
    handler: RegenerateActionHandler,
    world: _World,
    telegram_user_id: int,
) -> None:
    _link_telegram(
        world.telegram_account_repo, user_id=world.user.id, telegram_user_id=telegram_user_id
    )
    await handler.handle(
        chat_id=200,
        telegram_user_id=telegram_user_id,
        command=parse_regenerate_command(f"/regenerate {world.match.id}"),
    )
    await handler.handle(
        chat_id=200,
        telegram_user_id=telegram_user_id,
        command=parse_regenerate_command(f"/regenerate {world.match.id}"),
    )
    draft = world.draft_repo.get_by_match(world.match.id)
    assert draft is not None
    assert draft.version == 3


async def test_handle_rejects_unknown_match(
    handler: RegenerateActionHandler,
    world: _World,
    telegram_user_id: int,
) -> None:
    _link_telegram(
        world.telegram_account_repo, user_id=world.user.id, telegram_user_id=telegram_user_id
    )
    missing_id = uuid.uuid4()
    response = await handler.handle(
        chat_id=200,
        telegram_user_id=telegram_user_id,
        command=parse_regenerate_command(f"/regenerate {missing_id}"),
    )
    assert isinstance(response, SendMessageRequest)
    text = response.text.lower()
    assert "not found" in text or "unknown" in text
    assert world.audit_repo.list_by_event_type(AuditEventType.COVER_LETTER_REGENERATED.value) == []


async def test_handle_rejects_match_for_non_owner(
    handler: RegenerateActionHandler,
    world: _World,
    telegram_user_id: int,
    other_user_id: uuid.UUID,
) -> None:
    world.telegram_account_repo.create(
        user_id=other_user_id, telegram_user_id=telegram_user_id, username="mallory"
    )
    response = await handler.handle(
        chat_id=200,
        telegram_user_id=telegram_user_id,
        command=parse_regenerate_command(f"/regenerate {world.match.id}"),
    )
    assert isinstance(response, SendMessageRequest)
    text = response.text.lower()
    assert "forbidden" in text or "not" in text or "cannot" in text or "don't" in text
    draft = world.draft_repo.get_by_match(world.match.id)
    assert draft is not None
    assert draft.version == 1
    assert world.audit_repo.list_by_event_type(AuditEventType.COVER_LETTER_REGENERATED.value) == []


async def test_handle_creates_audit_event(
    handler: RegenerateActionHandler,
    world: _World,
    telegram_user_id: int,
) -> None:
    _link_telegram(
        world.telegram_account_repo, user_id=world.user.id, telegram_user_id=telegram_user_id
    )
    await handler.handle(
        chat_id=200,
        telegram_user_id=telegram_user_id,
        command=parse_regenerate_command(f"/regenerate {world.match.id}"),
    )
    logs = world.audit_repo.list_by_event_type(AuditEventType.COVER_LETTER_REGENERATED.value)
    assert len(logs) == 1
    assert logs[0].user_id == world.user.id
    details = json.loads(logs[0].details)
    assert details["match_id"] == str(world.match.id)
    assert details["version"] == 2


async def test_handle_rejects_unlinked_telegram_account(
    handler: RegenerateActionHandler,
    world: _World,
    telegram_user_id: int,
) -> None:
    response = await handler.handle(
        chat_id=200,
        telegram_user_id=telegram_user_id,
        command=parse_regenerate_command(f"/regenerate {world.match.id}"),
    )
    assert isinstance(response, SendMessageRequest)
    text = response.text.lower()
    assert "link" in text or "not linked" in text or "unknown" in text
    draft = world.draft_repo.get_by_match(world.match.id)
    assert draft is not None
    assert draft.version == 1
    assert world.audit_repo.list_by_event_type(AuditEventType.COVER_LETTER_REGENERATED.value) == []


async def test_handle_refuses_when_no_draft_exists(
    world: _World,
    telegram_user_id: int,
) -> None:
    _link_telegram(
        world.telegram_account_repo, user_id=world.user.id, telegram_user_id=telegram_user_id
    )
    world.draft_repo._by_id.pop(world.draft.id, None)
    world.draft_repo._by_match.pop(world.match.id, None)

    handler = RegenerateActionHandler(
        cover_letter_service=world.service,
        telegram_account_repo=world.telegram_account_repo,
        audit_service=world.audit_service,
        profile_repo=world.profile_repo,
    )

    response = await handler.handle(
        chat_id=200,
        telegram_user_id=telegram_user_id,
        command=parse_regenerate_command(f"/regenerate {world.match.id}"),
    )
    assert isinstance(response, SendMessageRequest)
    text = response.text.lower()
    assert "review" in text or "no cover letter" in text
    assert world.audit_repo.list_by_event_type(AuditEventType.COVER_LETTER_REGENERATED.value) == []


async def test_handle_returns_markdownv2_preview(
    handler: RegenerateActionHandler,
    world: _World,
    telegram_user_id: int,
) -> None:
    _link_telegram(
        world.telegram_account_repo, user_id=world.user.id, telegram_user_id=telegram_user_id
    )
    response = await handler.handle(
        chat_id=200,
        telegram_user_id=telegram_user_id,
        command=parse_regenerate_command(f"/regenerate {world.match.id}"),
    )
    assert isinstance(response, SendMessageRequest)
    assert "v2" in response.text
    assert "*Cover letter regenerated*" in response.text
    assert _escape_markdownv2(str(world.match.id)) in response.text


# Bot dispatcher integration


def _regenerate_update(
    text: str,
    *,
    chat_id: int = 700,
    telegram_user_id: int = 700,
) -> dict:
    return {
        "update_id": 7000,
        "message": {
            "message_id": 70,
            "date": 0,
            "chat": {"id": chat_id, "type": "private"},
            "from": {
                "id": telegram_user_id,
                "is_bot": False,
                "first_name": "Eve",
            },
            "text": text,
        },
    }


async def test_dispatcher_routes_regenerate_command(world: _World) -> None:
    telegram_user_id = 700
    _link_telegram(
        world.telegram_account_repo, user_id=world.user.id, telegram_user_id=telegram_user_id
    )
    handler = RegenerateActionHandler(
        cover_letter_service=world.service,
        telegram_account_repo=world.telegram_account_repo,
        audit_service=world.audit_service,
        profile_repo=world.profile_repo,
    )
    bot = TelegramBot(
        settings=TelegramSettings(bot_token="test-token", polling_timeout=30),
        regenerate_handler=handler,
    )
    response = await bot.handle_update(
        _regenerate_update(f"/regenerate {world.match.id}", telegram_user_id=telegram_user_id)
    )
    assert response is not None
    assert "regenerated" in response.text.lower() or "v2" in response.text
    draft = world.draft_repo.get_by_match(world.match.id)
    assert draft is not None
    assert draft.version == 2


async def test_dispatcher_regenerate_command_without_args_returns_help(world: _World) -> None:
    handler = RegenerateActionHandler(
        cover_letter_service=world.service,
        telegram_account_repo=world.telegram_account_repo,
        audit_service=world.audit_service,
        profile_repo=world.profile_repo,
    )
    bot = TelegramBot(
        settings=TelegramSettings(bot_token="test-token", polling_timeout=30),
        regenerate_handler=handler,
    )
    response = await bot.handle_update(_regenerate_update("/regenerate"))
    assert response is not None
    text = response.text.lower()
    assert "usage" in text or "regenerate" in text


async def test_dispatcher_includes_regenerate_in_help() -> None:
    bot = TelegramBot(settings=TelegramSettings(bot_token="test-token", polling_timeout=30))
    response = await bot.handle_update(_regenerate_update("/help"))
    assert response is not None
    assert "/regenerate" in response.text
