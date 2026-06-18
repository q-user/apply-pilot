"""End-to-end integration tests for the Telegram action flow (M4, issue #42).

These tests wire up the full :class:`TelegramBot` (with every action handler
attached), the real services (:class:`MatchService`,
:class:`CoverLetterService`, :class:`AuditService`, :class:`StatsService`),
and the in-memory repositories — then drive a representative set of
``/link``, ``/accept``, ``/reject``, ``/defer``, ``/review`` and
``/regenerate`` workflows as a single connected user journey.

The HTTP transport for ``sendMessage`` is satisfied by a real
:class:`httpx.MockTransport` so the bot actually exercises its full code
path (dispatcher → handler → service → repository → audit) without making
any real network calls. The transport records every call so tests can
assert on what the bot actually tried to send.

No :class:`Mock` is used. Real classes only; the only fakes are the
in-memory repositories the project already provides for tests.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, cast

import httpx
import pytest

from apply_pilot.features.audit.models import AuditEventType
from apply_pilot.features.audit.repository import InMemoryAuditLogRepository
from apply_pilot.features.audit.service import AuditService
from apply_pilot.features.cover_letter.models import (
    CoverLetterDraft,
    CoverLetterDraftStatus,
)
from apply_pilot.features.cover_letter.repository import InMemoryCoverLetterDraftRepository
from apply_pilot.features.cover_letter.service import CoverLetterService
from apply_pilot.features.cover_letter_style.models import CoverLetterStyle
from apply_pilot.features.cover_letter_style.repository import (
    InMemoryCoverLetterStyleRepository,
)
from apply_pilot.features.matches.models import MatchStatus, VacancyMatch
from apply_pilot.features.matches.repository import InMemoryVacancyMatchRepository
from apply_pilot.features.matches.service import MatchService
from apply_pilot.features.resumes.models import Resume
from apply_pilot.features.scoring.llm import InMemoryLLMClient
from apply_pilot.features.search_profiles.models import SearchProfile
from apply_pilot.features.search_profiles.repository import InMemorySearchProfileRepository
from apply_pilot.features.sources.models import Vacancy
from apply_pilot.features.telegram.actions.accept import AcceptActionHandler
from apply_pilot.features.telegram.actions.defer import DeferActionHandler
from apply_pilot.features.telegram.actions.regenerate import RegenerateActionHandler
from apply_pilot.features.telegram.actions.reject import RejectActionHandler
from apply_pilot.features.telegram.actions.review import ReviewActionHandler
from apply_pilot.features.telegram.bot import TelegramBot, TelegramSettings
from apply_pilot.features.telegram.digest import StatsService
from apply_pilot.features.telegram.digest.sender import DigestSender
from apply_pilot.features.telegram.linking import TelegramLinkingService
from apply_pilot.features.telegram.repository import InMemoryTelegramAccountRepository
from apply_pilot.features.users.models import User
from apply_pilot.features.users.repository import InMemoryUsersRepository

# ---------------------------------------------------------------------------
# Fake repositories the slices need (the same shape used by
# ``test_regenerate_action.py`` so the cover-letter service can run)
# ---------------------------------------------------------------------------


@dataclass
class _FakeUserRepo:
    users: dict[uuid.UUID, User] = field(default_factory=dict)

    def get_by_id(self, user_id: uuid.UUID) -> User | None:
        return self.users.get(user_id)

    def add(self, user: User) -> User:
        self.users[user.id] = user
        return user

    def list_all(self) -> list[User]:
        return list(self.users.values())


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


# ---------------------------------------------------------------------------
# World fixture: a fully wired slice with all collaborators
# ---------------------------------------------------------------------------


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
    style_repo: InMemoryCoverLetterStyleRepository
    match_repo: InMemoryVacancyMatchRepository
    draft_repo: InMemoryCoverLetterDraftRepository
    llm: InMemoryLLMClient
    cover_letter_service: CoverLetterService
    match_service: MatchService
    telegram_account_repo: InMemoryTelegramAccountRepository
    audit_repo: InMemoryAuditLogRepository
    audit_service: AuditService
    bot: TelegramBot
    http_calls: list[dict[str, Any]]
    stats_service: StatsService
    digest_sender: DigestSender
    users_repo: InMemoryUsersRepository
    linking_service: TelegramLinkingService
    regenerate_responses: list[str] = field(default_factory=list)


def _make_world(
    *,
    initial_cover_letter: str = "Dear team, Sincerely, Alice",
    regenerate_responses: list[str] | None = None,
    score: int | None = 85,
) -> _World:
    """Build a fully wired TelegramBot world for the integration tests."""
    if regenerate_responses is None:
        regenerate_responses = ["Dear hiring team, Excited to apply. Sincerely, Alice"]

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
        description="Looking for a senior Python developer with 5+ years of experience.",
        employer_name="Acme Corp",
        location="Remote",
        salary_from=250000,
        salary_to=350000,
        salary_currency="RUR",
        schedule="remote",
        experience="5+ years",
        skills=["python", "django", "postgres"],
        url="https://hh.ru/vacancy/2001",
        raw_data={"id": "2001", "name": "Senior Python Developer"},
    )
    match = VacancyMatch(
        id=uuid.uuid4(),
        search_profile_id=profile.id,
        vacancy_id=vacancy.id,
        status=MatchStatus.SCORED.value,
        score=score,
        explanation="Strong match: 5+ years of Python and Django.",
    )
    resume = Resume(
        id=uuid.uuid4(),
        user_id=user.id,
        filename="alice.pdf",
        content_type="application/pdf",
        size=2048,
        raw_text="I am a senior Python developer.",
        plain_text="I am a senior Python developer.",
    )
    style = CoverLetterStyle(
        user_id=user.id,
        tone="friendly",
        length="medium",
        focus_areas=["python"],
        avoid_phrases=[],
        extra_instructions="",
    )
    draft = CoverLetterDraft(
        id=uuid.uuid4(),
        match_id=match.id,
        user_id=user.id,
        content=initial_cover_letter,
        prompt_version="cover_letter@1.0.0",
        status=CoverLetterDraftStatus.DRAFT.value,
        version=1,
    )

    # Repositories
    user_repo = _FakeUserRepo()
    user_repo.add(user)
    vacancy_repo = _FakeVacancyRepo()
    vacancy_repo.add(vacancy)
    profile_repo = InMemorySearchProfileRepository()
    profile_repo.create(profile)
    resume_repo = _FakeResumeRepo()
    resume_repo.add(resume)
    style_repo = InMemoryCoverLetterStyleRepository()
    style_repo.create(style)
    match_repo = InMemoryVacancyMatchRepository(
        list_user_profiles=lambda uid: profile_repo.list_by_user(uid),
    )
    match_repo.create(match)
    draft_repo = InMemoryCoverLetterDraftRepository()
    draft_repo.create(draft)

    # LLM returns the next queued response on every call.
    responses = list(regenerate_responses)

    def _responder(_prompt: str) -> str:
        return responses.pop(0) if responses else "fallback body"

    llm = InMemoryLLMClient(responses={"*": _responder})

    cover_letter_service = CoverLetterService(
        llm=llm,
        match_repo=match_repo,
        user_repo=user_repo,  # type: ignore[arg-type]
        vacancy_repo=vacancy_repo,  # type: ignore[arg-type]
        profile_repo=profile_repo,  # type: ignore[arg-type]
        resume_repo=resume_repo,  # type: ignore[arg-type]
        style_repo=style_repo,  # type: ignore[arg-type]
        draft_repo=draft_repo,
    )
    match_service = MatchService(match_repo=match_repo, profile_repo=profile_repo)

    telegram_account_repo = InMemoryTelegramAccountRepository()
    audit_repo = InMemoryAuditLogRepository()
    audit_service = AuditService(audit_repo=audit_repo)
    users_repo = InMemoryUsersRepository()
    # Add the user so the StatsService can resolve the profile_repo->user chain.
    users_repo.create(email=user.email, hashed_password=user.hashed_password, is_active=True)

    # Action handlers (real, not mock)
    accept_handler = AcceptActionHandler(
        match_service=match_service,
        telegram_account_repo=telegram_account_repo,
        audit_service=audit_service,
    )
    defer_handler = DeferActionHandler(
        match_service=match_service,
        telegram_account_repo=telegram_account_repo,
        audit_service=audit_service,
    )
    reject_handler = RejectActionHandler(
        match_service=match_service,
        telegram_account_repo=telegram_account_repo,
        audit_service=audit_service,
    )
    review_handler = ReviewActionHandler(
        match_service=match_service,
        vacancy_repo=vacancy_repo,  # type: ignore[arg-type]
        cover_letter_repo=draft_repo,
        telegram_account_repo=telegram_account_repo,
    )
    regenerate_handler = RegenerateActionHandler(
        cover_letter_service=cover_letter_service,
        telegram_account_repo=telegram_account_repo,
        audit_service=audit_service,
        profile_repo=profile_repo,
    )

    # HTTP transport: a real httpx.MockTransport that records every call.
    http_calls: list[dict[str, Any]] = []

    def _transport_handler(request: httpx.Request) -> httpx.Response:
        # ``request.content`` is the JSON body the bot sent (chat_id + text).
        # ``request.url`` is the absolute URL the dispatcher constructed
        # (``https://api.telegram.org/bot<token>/sendMessage``). Recording
        # both keeps the assertion surface close to what production sees.
        try:
            body = json.loads(request.content) if request.content else {}
        except json.JSONDecodeError:
            body = {"_raw": request.content.decode("utf-8", errors="replace")}
        http_calls.append(
            {
                "method": request.method,
                "url": str(request.url),
                "body": body,
            }
        )
        # Return a minimal "ok" response that mirrors the real Telegram API
        # shape so the bot does not raise on ``response.json()``.
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": len(http_calls)}},
        )

    http_transport = httpx.MockTransport(_transport_handler)
    http_client = httpx.AsyncClient(transport=http_transport)

    linking_service = TelegramLinkingService()
    bot = TelegramBot(
        settings=TelegramSettings(bot_token="test-token", polling_timeout=30),
        http_client=http_client,
        linking_service=linking_service,
        telegram_account_repository=telegram_account_repo,
        accept_handler=accept_handler,
        defer_handler=defer_handler,
        regenerate_handler=regenerate_handler,
        reject_handler=reject_handler,
        review_handler=review_handler,
    )

    # Digest slice (cross-slice) — real services on the same repos.
    fixed_now = datetime(2026, 6, 15, 9, 0, tzinfo=UTC)
    stats_service = StatsService(
        match_repo=match_repo,
        telegram_account_repo=telegram_account_repo,
        user_repo=users_repo,  # type: ignore[arg-type]
        profile_repo=profile_repo,  # type: ignore[arg-type]
        now=cast("Callable[[], datetime]", lambda: fixed_now),
    )
    digest_sender = DigestSender(
        stats_service=stats_service,
        telegram_bot=bot,
        telegram_account_repo=telegram_account_repo,
        now=cast("Callable[[], datetime]", lambda: fixed_now),
    )

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
        cover_letter_service=cover_letter_service,
        match_service=match_service,
        telegram_account_repo=telegram_account_repo,
        audit_repo=audit_repo,
        audit_service=audit_service,
        bot=bot,
        http_calls=http_calls,
        stats_service=stats_service,
        digest_sender=digest_sender,
        users_repo=users_repo,
        linking_service=linking_service,
        regenerate_responses=regenerate_responses,
    )


# ---------------------------------------------------------------------------
# Update / dispatch helpers
# ---------------------------------------------------------------------------


def _update(
    text: str,
    *,
    chat_id: int = 100,
    telegram_user_id: int = 100,
) -> dict[str, Any]:
    """Build a minimal Telegram Update carrying ``text`` from ``telegram_user_id``."""
    return {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "date": 0,
            "chat": {"id": chat_id, "type": "private"},
            "from": {
                "id": telegram_user_id,
                "is_bot": False,
                "first_name": "Alice",
                "username": "alice",
            },
            "text": text,
        },
    }


async def _dispatch(
    world: _World,
    update: dict[str, Any],
) -> dict[str, Any] | None:
    """Run the full bot pipeline: ``handle_update`` + ``send_message`` (HTTP).

    Mirrors :meth:`TelegramBotProcess._dispatch_update` so the test exercises
    the same code path as production: dispatcher → action handler → service
    → repository, then the actual ``sendMessage`` HTTP call against the
    :class:`httpx.MockTransport`. Returns the recorded HTTP call (or
    ``None`` if the bot did not produce a reply).
    """
    before = len(world.http_calls)
    request = await world.bot.handle_update(update)
    if request is None:
        return None
    await world.bot.send_message(request.chat_id, request.text)
    if len(world.http_calls) != before + 1:  # pragma: no cover - sanity guard
        raise AssertionError(
            f"expected exactly one HTTP call, got {len(world.http_calls) - before}"
        )
    return world.http_calls[-1]


@pytest.fixture
def world() -> _World:
    """Return a freshly built :class:`_World` for each test.

    The world wires the full TelegramBot, every action handler, every
    real service, every in-memory repository, and a httpx.MockTransport
    that records the bot's ``sendMessage`` HTTP calls. No test-side
    state is shared between tests.
    """
    return _make_world()


async def _link_account_async(
    world: _World,
    *,
    telegram_user_id: int = 100,
) -> dict[str, Any] | None:
    """Async variant of :func:`_link_account` — use directly in async tests."""
    token = world.linking_service.generate_token(user_id=str(world.user.id))
    return await _dispatch(
        world,
        _update(f"/link {token}", chat_id=telegram_user_id, telegram_user_id=telegram_user_id),
    )


# ---------------------------------------------------------------------------
# /accept workflow
# ---------------------------------------------------------------------------


async def test_full_accept_workflow(world: _World) -> None:
    """User links Telegram, then ``/accept <match_id>`` flips the match to accepted.

    Exercises the full chain: ``/link`` → ``TelegramLinkingService`` →
    ``TelegramAccountRepository.create`` → ``/accept`` → ``MatchService.update_status`` →
    ``AuditService.log_event`` → ``sendMessage`` HTTP call.
    """
    link_call = await _link_account_async(world, telegram_user_id=100)
    assert link_call is not None
    assert "linked" in link_call["body"]["text"].lower()

    # Linking is observed through the real repository — no direct write.
    account = world.telegram_account_repo.find_by_telegram_user_id(100)
    assert account is not None
    assert account.user_id == world.user.id

    # Drive the accept command.
    accept_call = await _dispatch(world, _update(f"/accept {world.match.id}", telegram_user_id=100))
    assert accept_call is not None
    body = accept_call["body"]
    assert body["chat_id"] == 100
    assert "accepted" in body["text"].lower()
    assert str(world.match.id) in body["text"]

    # State mutation observed through the real MatchService.
    updated = world.match_repo.get_by_id(world.match.id)
    assert updated is not None
    assert updated.status == MatchStatus.ACCEPTED.value

    # Audit log captured the event with the match id in details.
    accepted_logs = world.audit_repo.list_by_event_type(AuditEventType.MATCH_ACCEPTED.value)
    assert len(accepted_logs) == 1
    assert accepted_logs[0].user_id == world.user.id
    details = json.loads(accepted_logs[0].details)
    assert details["match_id"] == str(world.match.id)


# ---------------------------------------------------------------------------
# /reject workflow
# ---------------------------------------------------------------------------


async def test_full_reject_workflow(world: _World) -> None:
    """``/reject <match_id> too junior`` records the reason on the audit event."""
    await _link_account_async(world, telegram_user_id=200)

    reject_call = await _dispatch(
        world,
        _update(
            f"/reject {world.match.id} salary too low",
            chat_id=200,
            telegram_user_id=200,
        ),
    )
    assert reject_call is not None
    body = reject_call["body"]
    assert "rejected" in body["text"].lower()
    assert "salary too low" in body["text"]

    # Match is now rejected.
    assert world.match_repo.get_by_id(world.match.id).status == MatchStatus.REJECTED.value

    # Audit log carries the reason.
    rejected_logs = world.audit_repo.list_by_event_type(AuditEventType.VACANCY_MATCH_REJECTED.value)
    assert len(rejected_logs) == 1
    assert rejected_logs[0].user_id == world.user.id
    details = json.loads(rejected_logs[0].details)
    assert details["match_id"] == str(world.match.id)
    assert details["reason"] == "salary too low"


# ---------------------------------------------------------------------------
# /defer workflow
# ---------------------------------------------------------------------------


async def test_full_defer_workflow(world: _World) -> None:
    """``/defer <match_id>`` flips the match to deferred and logs the audit event."""
    await _link_account_async(world, telegram_user_id=300)

    defer_call = await _dispatch(
        world,
        _update(f"/defer {world.match.id}", chat_id=300, telegram_user_id=300),
    )
    assert defer_call is not None
    body = defer_call["body"]
    assert "deferred" in body["text"].lower()

    # Match is now deferred.
    assert world.match_repo.get_by_id(world.match.id).status == MatchStatus.DEFERRED.value

    # Audit log captured MATCH_DEFERRED with the match id in details.
    deferred_logs = world.audit_repo.list_by_event_type(AuditEventType.MATCH_DEFERRED.value)
    assert len(deferred_logs) == 1
    assert deferred_logs[0].user_id == world.user.id
    details = json.loads(deferred_logs[0].details)
    assert details["match_id"] == str(world.match.id)


# ---------------------------------------------------------------------------
# /review workflow
# ---------------------------------------------------------------------------


async def test_review_shows_full_match_details(world: _World) -> None:
    """``/review`` returns a card with score, salary, employer, and skills."""
    await _link_account_async(world, telegram_user_id=400)

    review_call = await _dispatch(
        world,
        _update(f"/review {world.match.id}", chat_id=400, telegram_user_id=400),
    )
    assert review_call is not None
    text = review_call["body"]["text"]

    # The match-derived fields are pinned in the card.
    assert "85/100" in text  # score
    assert "Acme Corp" in text  # employer
    assert "Senior Python Developer" in text  # title
    # Salary is rendered as the "<min>-<max> <currency>" line.
    assert "250000-350000" in text
    assert "RUR" in text
    # Explanation from the LLM scoring pass surfaces in the card.
    assert "5+ years of Python" in text
    # The cover letter is "ready" because the world seeds a draft.
    assert "ready" in text
    # Footer lists the next-step actions.
    assert "/accept" in text
    assert "/reject" in text
    assert "/defer" in text
    assert "/regenerate" in text
    # The match id appears (escaped for MarkdownV2 — the ``-`` characters
    # become ``\-`` in the rendered card).
    assert str(world.match.id).replace("-", "\\-") in text


# ---------------------------------------------------------------------------
# /regenerate workflow
# ---------------------------------------------------------------------------


async def test_regenerate_creates_new_draft_version(world: _World) -> None:
    """``/regenerate`` bumps the draft to v2 with the new LLM content."""
    await _link_account_async(world, telegram_user_id=500)

    pre = world.draft_repo.get_by_match(world.match.id)
    assert pre is not None
    pre_version = pre.version
    pre_content = pre.content

    regen_call = await _dispatch(
        world,
        _update(f"/regenerate {world.match.id}", chat_id=500, telegram_user_id=500),
    )
    assert regen_call is not None
    body = regen_call["body"]
    assert "regenerated" in body["text"].lower()
    assert "v2" in body["text"]
    # The new LLM content is included as a preview.
    assert "hiring team" in body["text"]
    # The previous content is no longer present.
    assert pre_content not in body["text"]

    after = world.draft_repo.get_by_match(world.match.id)
    assert after is not None
    assert after.id == pre.id
    assert after.version == pre_version + 1
    assert after.content != pre_content

    # The audit event records the new version.
    logs = world.audit_repo.list_by_event_type(AuditEventType.COVER_LETTER_REGENERATED.value)
    assert len(logs) == 1
    assert logs[0].user_id == world.user.id
    details = json.loads(logs[0].details)
    assert details["match_id"] == str(world.match.id)
    assert details["version"] == 2


# ---------------------------------------------------------------------------
# Idempotency / friendly error responses
# ---------------------------------------------------------------------------


async def test_cannot_reject_already_rejected(world: _World) -> None:
    """Rejecting a match that is already rejected returns a friendly error."""
    await _link_account_async(world, telegram_user_id=600)
    # First reject: succeeds.
    first = await _dispatch(
        world,
        _update(f"/reject {world.match.id}", chat_id=600, telegram_user_id=600),
    )
    assert first is not None
    assert "rejected" in first["body"]["text"].lower()

    # Second reject: same row is now in the ``rejected`` state, so the
    # handler short-circuits with a "cannot reject from status X" message
    # and does not record a second audit event.
    second = await _dispatch(
        world,
        _update(f"/reject {world.match.id}", chat_id=600, telegram_user_id=600),
    )
    assert second is not None
    assert "cannot reject" in second["body"]["text"].lower()
    assert "rejected" in second["body"]["text"].lower()

    # Only one audit event was recorded for the successful reject.
    rejected_logs = world.audit_repo.list_by_event_type(AuditEventType.VACANCY_MATCH_REJECTED.value)
    assert len(rejected_logs) == 1


async def test_cannot_accept_already_accepted(world: _World) -> None:
    """Accepting a match that is already accepted returns a friendly error."""
    await _link_account_async(world, telegram_user_id=700)
    # First accept: succeeds.
    first = await _dispatch(
        world,
        _update(f"/accept {world.match.id}", chat_id=700, telegram_user_id=700),
    )
    assert first is not None
    assert "accepted" in first["body"]["text"].lower()

    # Second accept: status is now ``accepted``, so the handler refuses
    # with a "cannot accept from status X" message.
    second = await _dispatch(
        world,
        _update(f"/accept {world.match.id}", chat_id=700, telegram_user_id=700),
    )
    assert second is not None
    assert "cannot accept" in second["body"]["text"].lower()
    assert "accepted" in second["body"]["text"].lower()

    # Only one MATCH_ACCEPTED audit event was recorded.
    accepted_logs = world.audit_repo.list_by_event_type(AuditEventType.MATCH_ACCEPTED.value)
    assert len(accepted_logs) == 1


# ---------------------------------------------------------------------------
# /link edge cases
# ---------------------------------------------------------------------------


async def test_unlinked_user_gets_help_message(world: _World) -> None:
    """A ``/accept`` from an unlinked Telegram account points the user at ``/link``."""
    # No /link has been issued. The bot must still reply (not crash) and
    # the reply must guide the user to /link.
    call = await _dispatch(
        world,
        _update(f"/accept {world.match.id}", telegram_user_id=999_999),
    )
    assert call is not None
    text = call["body"]["text"].lower()
    assert "link" in text
    # The match must remain in its original status.
    assert world.match_repo.get_by_id(world.match.id).status == MatchStatus.SCORED.value
    # No audit event was recorded for the failed action.
    assert world.audit_repo.list_by_event_type(AuditEventType.MATCH_ACCEPTED.value) == []


# ---------------------------------------------------------------------------
# Audit log accumulation
# ---------------------------------------------------------------------------


async def test_audit_log_records_all_actions(world: _World) -> None:
    """Multiple actions in a single session must each produce an audit event."""
    await _link_account_async(world, telegram_user_id=800)
    # Defer a different match. Build a second match owned by the same user.
    extra_match = VacancyMatch(
        id=uuid.uuid4(),
        search_profile_id=world.profile.id,
        vacancy_id=world.vacancy.id,
        status=MatchStatus.NEW.value,
    )
    world.match_repo.create(extra_match)

    await _dispatch(world, _update(f"/defer {world.match.id}", chat_id=800, telegram_user_id=800))
    await _dispatch(world, _update(f"/reject {extra_match.id}", chat_id=800, telegram_user_id=800))
    await _dispatch(
        world, _update(f"/regenerate {world.match.id}", chat_id=800, telegram_user_id=800)
    )

    # Three distinct event types recorded for the user.
    user_logs = world.audit_repo.list_by_user(world.user.id)
    event_types = {log.event_type for log in user_logs}
    assert AuditEventType.MATCH_DEFERRED.value in event_types
    assert AuditEventType.VACANCY_MATCH_REJECTED.value in event_types
    assert AuditEventType.COVER_LETTER_REGENERATED.value in event_types

    # Every event references the user that triggered it.
    for log in user_logs:
        assert log.user_id == world.user.id


# ---------------------------------------------------------------------------
# Cross-slice: digest excludes deferred matches
# ---------------------------------------------------------------------------


async def test_digest_excludes_deferred_matches(world: _World) -> None:
    """A match the user deferred must not surface in the daily digest.

    Wires the real :class:`StatsService` and :class:`DigestSender` against
    the same repositories the bot manipulates; the deferred match must
    drop out of every bucket the digest reports.
    """
    await _link_account_async(world, telegram_user_id=900)
    # Baseline: the world has one SCORED match. The digest should count it.
    pre_stats = world.stats_service.get_user_stats(world.user.id, on_date=date(2026, 6, 15))
    assert pre_stats.matches_total == 1
    assert pre_stats.matches_new == 1

    # /defer the match.
    await _dispatch(
        world,
        _update(f"/defer {world.match.id}", chat_id=900, telegram_user_id=900),
    )

    # The match is on the row but no longer in any digest bucket.
    post_stats = world.stats_service.get_user_stats(world.user.id, on_date=date(2026, 6, 15))
    assert post_stats.matches_total == 0
    assert post_stats.matches_new == 0
    assert post_stats.matches_review == 0
    assert post_stats.matches_accepted == 0
    assert post_stats.matches_rejected == 0
    assert post_stats.matches_applied == 0

    # The digest sender still dispatches to the user (the bot still knows
    # them) but the message body has zero counts. The full HTTP path
    # round-trips through the MockTransport, so we also confirm the
    # message was actually sent.
    before = len(world.http_calls)
    sent = await world.digest_sender.send_to_user(world.user.id, on_date=date(2026, 6, 15))
    assert sent is True
    assert len(world.http_calls) == before + 1
    digest_text = world.http_calls[-1]["body"]["text"]
    # ``render_digest_message`` formats the headline as ``"0 total"`` and
    # every other line as ``"<n> <bucket>"`` — there are no leftover
    # entries that would have been there if the deferred match was
    # leaking through.
    assert "0 total" in digest_text


# ---------------------------------------------------------------------------
# Unknown commands
# ---------------------------------------------------------------------------


async def test_invalid_command_returns_help(world: _World) -> None:
    """An unrecognised command must nudge the user to ``/help`` and emit no audit."""
    call = await _dispatch(
        world,
        _update("/notacommand 1-2-3", telegram_user_id=1000),
    )
    assert call is not None
    text = call["body"]["text"].lower()
    assert "/help" in text

    # No state changes and no audit events.
    assert world.match_repo.get_by_id(world.match.id).status == MatchStatus.SCORED.value
    assert world.audit_repo.list_by_user(world.user.id) == []
    # The dispatcher did call sendMessage exactly once.
    assert (
        len([c for c in world.http_calls if c["body"].get("text", "").lower().find("/help") >= 0])
        == 1
    )
