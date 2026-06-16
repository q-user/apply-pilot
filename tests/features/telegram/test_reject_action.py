"""TDD tests for the :class:`RejectActionHandler` (M4, issue #38).

The handler is the use-case for the ``/reject <match_id>`` Telegram
command. It:

* resolves the local ``user_id`` from the ``telegram_user_id`` of the
  incoming update;
* validates the target match exists and is owned by the resolved user;
* rejects only from the allowed source states (``new``, ``scored``,
  ``review``, ``accepted``);
* records a :class:`VacancyMatchRejected` audit event with the optional
  reason in ``details``;
* returns a confirmation :class:`SendMessageRequest` for the chat.

All collaborators are wired through the constructor with the in-memory
fakes so the slice is exercised end-to-end without external I/O.
"""

from __future__ import annotations

import json
import uuid

import pytest

from job_apply.features.audit.models import AuditEventType
from job_apply.features.audit.repository import InMemoryAuditLogRepository
from job_apply.features.audit.service import AuditService
from job_apply.features.matches.models import MatchStatus, VacancyMatch
from job_apply.features.matches.repository import InMemoryVacancyMatchRepository
from job_apply.features.matches.service import MatchService
from job_apply.features.search_profiles.models import SearchProfile
from job_apply.features.search_profiles.repository import InMemorySearchProfileRepository
from job_apply.features.sources.models import Vacancy
from job_apply.features.telegram.actions.reject import (
    RejectActionHandler,
    parse_reject_command,
)
from job_apply.features.telegram.bot import TelegramBot, TelegramSettings
from job_apply.features.telegram.dto import SendMessageRequest
from job_apply.features.telegram.repository import InMemoryTelegramAccountRepository

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _vacancy(source_id: str = "hh-rej-1", title: str = "Backend") -> Vacancy:
    """Build a fully-populated :class:`Vacancy`."""
    v = Vacancy(
        source="hh",
        source_id=source_id,
        title=title,
        raw_data={"id": source_id, "name": title},
    )
    v.id = uuid.uuid4()
    return v


def _profile(user_id: uuid.UUID) -> SearchProfile:
    """Build a :class:`SearchProfile` owned by ``user_id``."""
    p = SearchProfile(user_id=user_id, title="Python", is_active=True)
    p.id = uuid.uuid4()
    return p


@pytest.fixture
def user_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def other_user_id() -> uuid.UUID:
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
def handler(
    match_service: MatchService,
    telegram_account_repo: InMemoryTelegramAccountRepository,
    audit_service: AuditService,
) -> RejectActionHandler:
    return RejectActionHandler(
        match_service=match_service,
        telegram_account_repo=telegram_account_repo,
        audit_service=audit_service,
    )


def _seed_match(
    match_repo: InMemoryVacancyMatchRepository,
    profile_repo: InMemorySearchProfileRepository,
    *,
    user_id: uuid.UUID,
    status: str = MatchStatus.NEW.value,
) -> VacancyMatch:
    """Create a profile + match owned by ``user_id`` with the given status."""
    profile = _profile(user_id)
    profile_repo.create(profile)
    vacancy = _vacancy()
    match = VacancyMatch(
        search_profile_id=profile.id,
        vacancy_id=vacancy.id,
        status=status,
    )
    return match_repo.create(match)


def _link_telegram(
    repo: InMemoryTelegramAccountRepository,
    *,
    user_id: uuid.UUID,
    telegram_user_id: int,
) -> None:
    """Create a Telegram account linking ``telegram_user_id`` to ``user_id``."""
    repo.create(user_id=user_id, telegram_user_id=telegram_user_id, username="alice")


# ---------------------------------------------------------------------------
# parse_reject_command
# ---------------------------------------------------------------------------


def test_parse_reject_command_with_match_id_only() -> None:
    """``/reject <match_id>`` parses to a RejectCommand with no reason."""
    match_id = "11111111-1111-1111-1111-111111111111"
    command = parse_reject_command(f"/reject {match_id}")

    assert command is not None
    assert command.match_id == uuid.UUID(match_id)
    assert command.reason is None


def test_parse_reject_command_with_reason() -> None:
    """``/reject <match_id> <reason>`` parses to a RejectCommand with reason."""
    match_id = "11111111-1111-1111-1111-111111111111"
    command = parse_reject_command(f"/reject {match_id} not interested")

    assert command is not None
    assert command.match_id == uuid.UUID(match_id)
    assert command.reason == "not interested"


def test_parse_reject_command_without_args_returns_none() -> None:
    """``/reject`` with no args must return None so the caller shows help text."""
    assert parse_reject_command("/reject") is None
    assert parse_reject_command("/reject   ") is None


def test_parse_reject_command_with_invalid_uuid_returns_none() -> None:
    """``/reject <garbage>`` must return None so the caller shows usage text."""
    assert parse_reject_command("/reject not-a-uuid") is None


# ---------------------------------------------------------------------------
# RejectActionHandler.handle
# ---------------------------------------------------------------------------


def test_handle_rejects_match_for_owner(
    handler: RejectActionHandler,
    match_repo: InMemoryVacancyMatchRepository,
    profile_repo: InMemorySearchProfileRepository,
    telegram_account_repo: InMemoryTelegramAccountRepository,
    user_id: uuid.UUID,
    telegram_user_id: int,
) -> None:
    """The owner can reject their own match and receive a confirmation message."""
    match = _seed_match(match_repo, profile_repo, user_id=user_id)
    _link_telegram(telegram_account_repo, user_id=user_id, telegram_user_id=telegram_user_id)

    response = handler.handle(
        chat_id=100,
        telegram_user_id=telegram_user_id,
        command=parse_reject_command(f"/reject {match.id}"),
    )

    assert isinstance(response, SendMessageRequest)
    assert response.chat_id == 100
    assert str(match.id) in response.text
    assert "rejected" in response.text.lower()
    # State was updated.
    assert match_repo.get_by_id(match.id).status == MatchStatus.REJECTED.value


def test_handle_rejects_with_reason(
    handler: RejectActionHandler,
    match_repo: InMemoryVacancyMatchRepository,
    profile_repo: InMemorySearchProfileRepository,
    telegram_account_repo: InMemoryTelegramAccountRepository,
    audit_repo: InMemoryAuditLogRepository,
    user_id: uuid.UUID,
    telegram_user_id: int,
) -> None:
    """The reason provided with the command must be stored in the audit details."""
    match = _seed_match(match_repo, profile_repo, user_id=user_id)
    _link_telegram(telegram_account_repo, user_id=user_id, telegram_user_id=telegram_user_id)

    handler.handle(
        chat_id=100,
        telegram_user_id=telegram_user_id,
        command=parse_reject_command(f"/reject {match.id} salary too low"),
    )

    logs = audit_repo.list_by_event_type(AuditEventType.VACANCY_MATCH_REJECTED.value)
    assert len(logs) == 1
    assert logs[0].user_id == user_id
    details = json.loads(logs[0].details)
    assert details["match_id"] == str(match.id)
    assert details["reason"] == "salary too low"


def test_handle_rejects_match_for_non_owner(
    handler: RejectActionHandler,
    match_repo: InMemoryVacancyMatchRepository,
    profile_repo: InMemorySearchProfileRepository,
    telegram_account_repo: InMemoryTelegramAccountRepository,
    user_id: uuid.UUID,
    other_user_id: uuid.UUID,
    telegram_user_id: int,
) -> None:
    """A user trying to reject another user's match must get an error and no state change."""
    match = _seed_match(match_repo, profile_repo, user_id=other_user_id)
    _link_telegram(telegram_account_repo, user_id=user_id, telegram_user_id=telegram_user_id)

    response = handler.handle(
        chat_id=100,
        telegram_user_id=telegram_user_id,
        command=parse_reject_command(f"/reject {match.id}"),
    )

    assert isinstance(response, SendMessageRequest)
    text = response.text.lower()
    assert "forbidden" in text or "not" in text or "cannot" in text or "don't" in text
    # State was not updated.
    assert match_repo.get_by_id(match.id).status == MatchStatus.NEW.value


def test_handle_rejects_unknown_match(
    handler: RejectActionHandler,
    match_repo: InMemoryVacancyMatchRepository,
    profile_repo: InMemorySearchProfileRepository,
    telegram_account_repo: InMemoryTelegramAccountRepository,
    user_id: uuid.UUID,
    telegram_user_id: int,
) -> None:
    """A non-existent match id must return an error and not crash."""
    _seed_match(match_repo, profile_repo, user_id=user_id)  # ensure repo is not empty
    _link_telegram(telegram_account_repo, user_id=user_id, telegram_user_id=telegram_user_id)
    unknown_id = uuid.uuid4()

    response = handler.handle(
        chat_id=100,
        telegram_user_id=telegram_user_id,
        command=parse_reject_command(f"/reject {unknown_id}"),
    )

    assert isinstance(response, SendMessageRequest)
    text = response.text.lower()
    assert "not found" in text or "unknown" in text


def test_handle_creates_audit_event_with_reason(
    handler: RejectActionHandler,
    match_repo: InMemoryVacancyMatchRepository,
    profile_repo: InMemorySearchProfileRepository,
    telegram_account_repo: InMemoryTelegramAccountRepository,
    audit_repo: InMemoryAuditLogRepository,
    user_id: uuid.UUID,
    telegram_user_id: int,
) -> None:
    """A successful reject must always emit a VACANCY_MATCH_REJECTED audit event."""
    match = _seed_match(match_repo, profile_repo, user_id=user_id)
    _link_telegram(telegram_account_repo, user_id=user_id, telegram_user_id=telegram_user_id)

    handler.handle(
        chat_id=100,
        telegram_user_id=telegram_user_id,
        command=parse_reject_command(f"/reject {match.id} not a fit"),
    )

    logs = audit_repo.list_by_user(user_id)
    assert len(logs) == 1
    log = logs[0]
    assert log.event_type == AuditEventType.VACANCY_MATCH_REJECTED.value
    details = json.loads(log.details)
    assert details["match_id"] == str(match.id)
    assert details["reason"] == "not a fit"


def test_handle_rejects_from_each_allowed_status(
    handler: RejectActionHandler,
    match_repo: InMemoryVacancyMatchRepository,
    profile_repo: InMemorySearchProfileRepository,
    telegram_account_repo: InMemoryTelegramAccountRepository,
    user_id: uuid.UUID,
    telegram_user_id: int,
) -> None:
    """Reject must work from new, scored, review, and accepted."""
    _link_telegram(
        telegram_account_repo,
        user_id=user_id,
        telegram_user_id=telegram_user_id,
    )
    for source in (
        MatchStatus.NEW.value,
        MatchStatus.SCORED.value,
        MatchStatus.REVIEW.value,
        MatchStatus.ACCEPTED.value,
    ):
        match = _seed_match(match_repo, profile_repo, user_id=user_id, status=source)

        response = handler.handle(
            chat_id=100,
            telegram_user_id=telegram_user_id,
            command=parse_reject_command(f"/reject {match.id}"),
        )

        assert isinstance(response, SendMessageRequest)
        assert match_repo.get_by_id(match.id).status == MatchStatus.REJECTED.value


def test_handle_refuses_reject_from_rejected_status(
    handler: RejectActionHandler,
    match_repo: InMemoryVacancyMatchRepository,
    profile_repo: InMemorySearchProfileRepository,
    telegram_account_repo: InMemoryTelegramAccountRepository,
    user_id: uuid.UUID,
    telegram_user_id: int,
) -> None:
    """Rejecting a match that is already rejected must return an error."""
    match = _seed_match(
        match_repo, profile_repo, user_id=user_id, status=MatchStatus.REJECTED.value
    )
    _link_telegram(telegram_account_repo, user_id=user_id, telegram_user_id=telegram_user_id)

    response = handler.handle(
        chat_id=100,
        telegram_user_id=telegram_user_id,
        command=parse_reject_command(f"/reject {match.id}"),
    )

    assert isinstance(response, SendMessageRequest)
    text = response.text.lower()
    assert "already" in text or "cannot" in text or "invalid" in text or "not allowed" in text


def test_handle_refuses_reject_from_applied_status(
    handler: RejectActionHandler,
    match_repo: InMemoryVacancyMatchRepository,
    profile_repo: InMemorySearchProfileRepository,
    telegram_account_repo: InMemoryTelegramAccountRepository,
    user_id: uuid.UUID,
    telegram_user_id: int,
) -> None:
    """Rejecting a match that has been applied to must return an error."""
    match = _seed_match(match_repo, profile_repo, user_id=user_id, status=MatchStatus.APPLIED.value)
    _link_telegram(telegram_account_repo, user_id=user_id, telegram_user_id=telegram_user_id)

    response = handler.handle(
        chat_id=100,
        telegram_user_id=telegram_user_id,
        command=parse_reject_command(f"/reject {match.id}"),
    )

    assert isinstance(response, SendMessageRequest)
    text = response.text.lower()
    assert "already" in text or "cannot" in text or "invalid" in text or "not allowed" in text


def test_handle_rejects_unlinked_telegram_account(
    handler: RejectActionHandler,
    match_repo: InMemoryVacancyMatchRepository,
    profile_repo: InMemorySearchProfileRepository,
    user_id: uuid.UUID,
    telegram_user_id: int,
) -> None:
    """An update from a Telegram user with no linked account must be rejected."""
    match = _seed_match(match_repo, profile_repo, user_id=user_id)

    response = handler.handle(
        chat_id=100,
        telegram_user_id=telegram_user_id,
        command=parse_reject_command(f"/reject {match.id}"),
    )

    assert isinstance(response, SendMessageRequest)
    text = response.text.lower()
    assert "link" in text or "not linked" in text or "unknown" in text
    # State was not updated.
    assert match_repo.get_by_id(match.id).status == MatchStatus.NEW.value


# ---------------------------------------------------------------------------
# Bot dispatcher integration
# ---------------------------------------------------------------------------


def _reject_update(
    text: str,
    *,
    chat_id: int = 700,
    telegram_user_id: int = 700,
) -> dict:
    """Build a minimal Telegram Update carrying a /reject text message."""
    return {
        "update_id": 7000,
        "message": {
            "message_id": 70,
            "date": 0,
            "chat": {"id": chat_id, "type": "private"},
            "from": {
                "id": telegram_user_id,
                "is_bot": False,
                "first_name": "Carol",
            },
            "text": text,
        },
    }


async def test_dispatcher_routes_reject_command(
    match_service: MatchService,
    match_repo: InMemoryVacancyMatchRepository,
    profile_repo: InMemorySearchProfileRepository,
    telegram_account_repo: InMemoryTelegramAccountRepository,
    audit_service: AuditService,
    user_id: uuid.UUID,
) -> None:
    """The bot must delegate ``/reject <id>`` to the RejectActionHandler."""
    match = _seed_match(match_repo, profile_repo, user_id=user_id)
    telegram_user_id = 700
    _link_telegram(telegram_account_repo, user_id=user_id, telegram_user_id=telegram_user_id)

    handler = RejectActionHandler(
        match_service=match_service,
        telegram_account_repo=telegram_account_repo,
        audit_service=audit_service,
    )
    bot = TelegramBot(
        settings=TelegramSettings(bot_token="test-token", polling_timeout=30),
        reject_handler=handler,
    )

    response = await bot.handle_update(_reject_update(f"/reject {match.id}", telegram_user_id=700))

    assert response is not None
    text = response.text.lower()
    assert "rejected" in text
    assert match_repo.get_by_id(match.id).status == MatchStatus.REJECTED.value


async def test_dispatcher_reject_command_without_args_returns_help() -> None:
    """``/reject`` without args must return the help text (not a crash)."""
    from job_apply.config import TelegramSettings

    bot = TelegramBot(
        settings=TelegramSettings(bot_token="test-token", polling_timeout=30),
        reject_handler=RejectActionHandler(
            match_service=MatchService(
                match_repo=InMemoryVacancyMatchRepository(),
                profile_repo=InMemorySearchProfileRepository(),
            ),
            telegram_account_repo=InMemoryTelegramAccountRepository(),
            audit_service=AuditService(audit_repo=InMemoryAuditLogRepository()),
        ),
    )

    response = await bot.handle_update(_reject_update("/reject"))

    assert response is not None
    text = response.text.lower()
    assert "usage" in text or "reject" in text


async def test_dispatcher_includes_reject_in_help() -> None:
    """``/help`` must list ``/reject`` so users discover the command."""
    from job_apply.config import TelegramSettings

    bot = TelegramBot(settings=TelegramSettings(bot_token="test-token", polling_timeout=30))

    response = await bot.handle_update(_reject_update("/help"))

    assert response is not None
    assert "/reject" in response.text
