"""Integration tests for the ``AcceptActionHandler`` writing-style memory hook.

The handler must record the accepted cover letter into the user's style
memory when:

* a valid :class:`CoverLetterDraft` exists for the match;
* the caller's Telegram account is linked;
* the match is in an allowed source state and the ownership check passes.

The recording is best-effort: a failure inside the style memory layer
must not break the accept command. The handler is decoupled from the
``StyleMemoryService`` through an optional ``style_memory_service``
collaborator that the wiring code in :mod:`process` will inject.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

import pytest

from apply_pilot.features.audit.models import AuditEventType
from apply_pilot.features.audit.repository import InMemoryAuditLogRepository
from apply_pilot.features.audit.service import AuditService
from apply_pilot.features.cover_letter.models import CoverLetterDraft
from apply_pilot.features.cover_letter.repository import InMemoryCoverLetterDraftRepository
from apply_pilot.features.matches.models import MatchStatus, VacancyMatch
from apply_pilot.features.matches.repository import InMemoryVacancyMatchRepository
from apply_pilot.features.matches.service import MatchService
from apply_pilot.features.search_profiles.models import SearchProfile
from apply_pilot.features.search_profiles.repository import InMemorySearchProfileRepository
from apply_pilot.features.sources.models import Vacancy
from apply_pilot.features.telegram.actions.accept import (
    AcceptActionHandler,
    parse_accept_command,
)
from apply_pilot.features.telegram.dto import SendMessageRequest
from apply_pilot.features.telegram.repository import InMemoryTelegramAccountRepository
from apply_pilot.features.writing_style_memory.repository import InMemoryStyleMemoryRepository
from apply_pilot.features.writing_style_memory.service import StyleMemoryService

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _vacancy() -> Vacancy:
    v = Vacancy(
        source="hh",
        source_id="hh-1",
        title="Backend",
        raw_data={"id": "hh-1", "name": "Backend"},
    )
    v.id = uuid.uuid4()
    return v


def _profile(user_id: uuid.UUID) -> SearchProfile:
    p = SearchProfile(user_id=user_id, title="Python", is_active=True)
    p.id = uuid.uuid4()
    return p


def _seed_match(
    match_repo: InMemoryVacancyMatchRepository,
    profile_repo: InMemorySearchProfileRepository,
    *,
    user_id: uuid.UUID,
    status: str = MatchStatus.NEW.value,
) -> VacancyMatch:
    profile = _profile(user_id)
    profile_repo.create(profile)
    vacancy = _vacancy()
    match = VacancyMatch(
        search_profile_id=profile.id,
        vacancy_id=vacancy.id,
        status=status,
    )
    return match_repo.create(match)


def _seed_draft(
    draft_repo: InMemoryCoverLetterDraftRepository,
    *,
    user_id: uuid.UUID,
    match_id: uuid.UUID,
    content: str,
) -> CoverLetterDraft:
    draft = CoverLetterDraft(
        match_id=match_id,
        user_id=user_id,
        content=content,
        prompt_version="cover-letter@1.0.0",
        model_used="gpt-test",
        status="draft",
    )
    return draft_repo.create(draft)


def _link(repo: InMemoryTelegramAccountRepository, *, user_id: uuid.UUID, tg: int) -> None:
    repo.create(user_id=user_id, telegram_user_id=tg, username="alice")


@pytest.fixture
def user_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def telegram_user_id() -> int:
    return 424242


@pytest.fixture
def profile_repo() -> InMemorySearchProfileRepository:
    return InMemorySearchProfileRepository()


@pytest.fixture
def match_repo(profile_repo: InMemorySearchProfileRepository) -> InMemoryVacancyMatchRepository:
    return InMemoryVacancyMatchRepository(list_user_profiles=profile_repo.list_by_user)


@pytest.fixture
def match_service(
    match_repo: InMemoryVacancyMatchRepository,
    profile_repo: InMemorySearchProfileRepository,
) -> MatchService:
    return MatchService(match_repo=match_repo, profile_repo=profile_repo)


@pytest.fixture
def telegram_account_repo() -> InMemoryTelegramAccountRepository:
    return InMemoryTelegramAccountRepository()


@pytest.fixture
def audit_repo() -> InMemoryAuditLogRepository:
    return InMemoryAuditLogRepository()


@pytest.fixture
def audit_service(audit_repo: InMemoryAuditLogRepository) -> AuditService:
    return AuditService(audit_repo=audit_repo)


@pytest.fixture
def draft_repo() -> InMemoryCoverLetterDraftRepository:
    return InMemoryCoverLetterDraftRepository()


@pytest.fixture
def style_repo() -> InMemoryStyleMemoryRepository:
    return InMemoryStyleMemoryRepository()


@pytest.fixture
def style_memory_service(style_repo: InMemoryStyleMemoryRepository) -> StyleMemoryService:
    return StyleMemoryService(repository=style_repo)


@pytest.fixture
def handler(
    match_service: MatchService,
    telegram_account_repo: InMemoryTelegramAccountRepository,
    audit_service: AuditService,
    style_memory_service: StyleMemoryService,
    draft_repo: InMemoryCoverLetterDraftRepository,
) -> AcceptActionHandler:
    return AcceptActionHandler(
        match_service=match_service,
        telegram_account_repo=telegram_account_repo,
        audit_service=audit_service,
        style_memory_service=style_memory_service,
        draft_repository=draft_repo,
    )


# ---------------------------------------------------------------------------
# Recording-on-accept
# ---------------------------------------------------------------------------


def test_accept_records_accepted_letter_into_style_memory(
    handler: AcceptActionHandler,
    match_repo: InMemoryVacancyMatchRepository,
    profile_repo: InMemorySearchProfileRepository,
    draft_repo: InMemoryCoverLetterDraftRepository,
    telegram_account_repo: InMemoryTelegramAccountRepository,
    style_repo: InMemoryStyleMemoryRepository,
    user_id: uuid.UUID,
    telegram_user_id: int,
) -> None:
    """Accepting a match with a draft must append a style memory entry."""
    match = _seed_match(match_repo, profile_repo, user_id=user_id)
    draft = _seed_draft(
        draft_repo,
        user_id=user_id,
        match_id=match.id,
        content="Hello! I would love to bring my FastAPI expertise to your team.",
    )
    _link(telegram_account_repo, user_id=user_id, tg=telegram_user_id)

    response = handler.handle(
        chat_id=1,
        telegram_user_id=telegram_user_id,
        command=parse_accept_command(f"/accept {match.id}"),
    )

    assert isinstance(response, SendMessageRequest)
    assert "accepted" in response.text.lower()
    entries = style_repo.list_for_user(user_id)
    assert len(entries) == 1
    # The recorded entry's ``cover_letter_id`` is the FK to the
    # ``cover_letter_drafts`` table, i.e. the draft's own id.
    assert entries[0].cover_letter_id == draft.id
    assert "FastAPI" in entries[0].letter_text


def test_accept_succeeds_when_no_draft_exists_for_match(
    handler: AcceptActionHandler,
    match_repo: InMemoryVacancyMatchRepository,
    profile_repo: InMemorySearchProfileRepository,
    draft_repo: InMemoryCoverLetterDraftRepository,
    telegram_account_repo: InMemoryTelegramAccountRepository,
    style_repo: InMemoryStyleMemoryRepository,
    user_id: uuid.UUID,
    telegram_user_id: int,
) -> None:
    """A match with no draft must still be accepted and produce no memory entry."""
    match = _seed_match(match_repo, profile_repo, user_id=user_id)
    assert draft_repo.get_by_match(match.id) is None
    _link(telegram_account_repo, user_id=user_id, tg=telegram_user_id)

    response = handler.handle(
        chat_id=1,
        telegram_user_id=telegram_user_id,
        command=parse_accept_command(f"/accept {match.id}"),
    )

    assert isinstance(response, SendMessageRequest)
    assert "accepted" in response.text.lower()
    assert style_repo.list_for_user(user_id) == []


def test_accept_succeeds_when_style_memory_service_is_none(
    match_service: MatchService,
    match_repo: InMemoryVacancyMatchRepository,
    profile_repo: InMemorySearchProfileRepository,
    draft_repo: InMemoryCoverLetterDraftRepository,
    telegram_account_repo: InMemoryTelegramAccountRepository,
    audit_service: AuditService,
    user_id: uuid.UUID,
    telegram_user_id: int,
) -> None:
    """The handler must keep working when no style memory service is wired in."""
    match = _seed_match(match_repo, profile_repo, user_id=user_id)
    _seed_draft(
        draft_repo,
        user_id=user_id,
        match_id=match.id,
        content="A letter that would normally be recorded.",
    )
    _link(telegram_account_repo, user_id=user_id, tg=telegram_user_id)

    handler = AcceptActionHandler(
        match_service=match_service,
        telegram_account_repo=telegram_account_repo,
        audit_service=audit_service,
        # No style_memory_service, no draft_repository.
    )

    response = handler.handle(
        chat_id=1,
        telegram_user_id=telegram_user_id,
        command=parse_accept_command(f"/accept {match.id}"),
    )

    assert isinstance(response, SendMessageRequest)
    assert "accepted" in response.text.lower()


@dataclass
class _ExplodingStyleMemoryService:
    """A ``StyleMemoryService``-shaped collaborator that always blows up.

    The handler must catch the exception, log it, and still complete the
    accept flow. This mirrors the existing "best-effort" contract for
    :class:`ApplyJobEnqueuer`.
    """

    def record_accepted_letter(
        self,
        *,
        user_id: uuid.UUID,
        cover_letter_id: uuid.UUID,
        letter_text: str,
    ) -> None:
        raise RuntimeError("style memory storage is down")


def test_accept_succeeds_when_style_memory_recording_fails(
    match_service: MatchService,
    match_repo: InMemoryVacancyMatchRepository,
    profile_repo: InMemorySearchProfileRepository,
    draft_repo: InMemoryCoverLetterDraftRepository,
    telegram_account_repo: InMemoryTelegramAccountRepository,
    audit_service: AuditService,
    audit_repo: InMemoryAuditLogRepository,
    user_id: uuid.UUID,
    telegram_user_id: int,
) -> None:
    """A failure in the style memory layer must not break the accept command."""
    match = _seed_match(match_repo, profile_repo, user_id=user_id)
    _seed_draft(
        draft_repo,
        user_id=user_id,
        match_id=match.id,
        content="The letter that will fail to record.",
    )
    _link(telegram_account_repo, user_id=user_id, tg=telegram_user_id)

    handler = AcceptActionHandler(
        match_service=match_service,
        telegram_account_repo=telegram_account_repo,
        audit_service=audit_service,
        style_memory_service=_ExplodingStyleMemoryService(),  # type: ignore[arg-type]
        draft_repository=draft_repo,
    )

    response = handler.handle(
        chat_id=1,
        telegram_user_id=telegram_user_id,
        command=parse_accept_command(f"/accept {match.id}"),
    )

    assert isinstance(response, SendMessageRequest)
    assert "accepted" in response.text.lower()
    # Audit event is still recorded.
    logs = audit_repo.list_by_event_type(AuditEventType.MATCH_ACCEPTED.value)
    assert len(logs) == 1


def test_existing_accept_action_tests_still_pass(
    # Smoke check: the existing accept flow (without the new
    # style-memory collaborators) must keep working — the original
    # tests in ``test_accept_action.py`` rely on it.
    handler: AcceptActionHandler,
    match_repo: InMemoryVacancyMatchRepository,
    profile_repo: InMemorySearchProfileRepository,
    telegram_account_repo: InMemoryTelegramAccountRepository,
    audit_repo: InMemoryAuditLogRepository,
    user_id: uuid.UUID,
    telegram_user_id: int,
) -> None:
    match = _seed_match(match_repo, profile_repo, user_id=user_id)
    _link(telegram_account_repo, user_id=user_id, tg=telegram_user_id)

    response = handler.handle(
        chat_id=1,
        telegram_user_id=telegram_user_id,
        command=parse_accept_command(f"/accept {match.id}"),
    )

    assert isinstance(response, SendMessageRequest)
    assert "accepted" in response.text.lower()
    logs = audit_repo.list_by_event_type(AuditEventType.MATCH_ACCEPTED.value)
    assert len(logs) == 1
    details = json.loads(logs[0].details)
    assert details["match_id"] == str(match.id)
