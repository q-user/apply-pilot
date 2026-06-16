"""TDD tests for the :class:`AcceptActionHandler` (M4, issue #37).

The handler is the use-case for the ``/accept <match_id>`` Telegram
command. It:

* resolves the local ``user_id`` from the ``telegram_user_id`` of the
  incoming update;
* validates the target match exists and is owned by the resolved user;
* accepts only from the allowed source states (``new``, ``scored``,
  ``review``);
* flips the match's status to ``accepted`` via
  :meth:`MatchService.update_status`;
* records a ``MATCH_ACCEPTED`` audit event with ``match_id`` in
  ``details``;
* returns a confirmation :class:`SendMessageRequest` for the chat.

All collaborators are wired through the constructor with the in-memory
fakes so the slice is exercised end-to-end without external I/O and
without ``Mock``.
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
from job_apply.features.telegram.actions.accept import (
    AcceptActionHandler,
    parse_accept_command,
)
from job_apply.features.telegram.bot import TelegramBot, TelegramSettings
from job_apply.features.telegram.dto import SendMessageRequest
from job_apply.features.telegram.repository import InMemoryTelegramAccountRepository

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _vacancy(source_id: str = "hh-acc-1", title: str = "Backend") -> Vacancy:
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
    return 515151


@pytest.fixture
def profile_repo() -> InMemorySearchProfileRepository:
    return InMemorySearchProfileRepository()


@pytest.fixture
def match_repo(
    profile_repo: InMemorySearchProfileRepository,
) -> InMemoryVacancyMatchRepository:
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
) -> AcceptActionHandler:
    return AcceptActionHandler(
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
# parse_accept_command
# ---------------------------------------------------------------------------


def test_parse_accept_command_with_match_id() -> None:
    """``/accept <match_id>`` parses to an AcceptCommand with the match_id."""
    match_id = "11111111-1111-1111-1111-111111111111"
    command = parse_accept_command(f"/accept {match_id}")

    assert command is not None
    assert command.match_id == uuid.UUID(match_id)
    # raw_args is captured so the dispatcher can show the user's input back
    # to them when displaying the help text.
    assert command.raw_args == match_id


def test_parse_accept_command_without_args_returns_none() -> None:
    """``/accept`` with no args must return None so the caller shows help text."""
    assert parse_accept_command("/accept") is None
    assert parse_accept_command("/accept   ") is None


def test_parse_accept_command_with_invalid_uuid_returns_none() -> None:
    """``/accept <garbage>`` must return None so the caller shows usage text."""
    assert parse_accept_command("/accept not-a-uuid") is None


# ---------------------------------------------------------------------------
# AcceptActionHandler.handle
# ---------------------------------------------------------------------------


def test_handle_accepts_match_for_owner(
    handler: AcceptActionHandler,
    match_repo: InMemoryVacancyMatchRepository,
    profile_repo: InMemorySearchProfileRepository,
    telegram_account_repo: InMemoryTelegramAccountRepository,
    audit_repo: InMemoryAuditLogRepository,
    user_id: uuid.UUID,
    telegram_user_id: int,
) -> None:
    """The owner can accept their own match and receive a confirmation message."""
    match = _seed_match(match_repo, profile_repo, user_id=user_id)
    _link_telegram(telegram_account_repo, user_id=user_id, telegram_user_id=telegram_user_id)

    response = handler.handle(
        chat_id=100,
        telegram_user_id=telegram_user_id,
        command=parse_accept_command(f"/accept {match.id}"),
    )

    assert isinstance(response, SendMessageRequest)
    assert response.chat_id == 100
    text = response.text.lower()
    assert str(match.id) in response.text
    assert "accepted" in text
    # State was updated to "accepted".
    assert match_repo.get_by_id(match.id).status == MatchStatus.ACCEPTED.value
    # Audit event was recorded with match_id in details.
    logs = audit_repo.list_by_event_type(AuditEventType.MATCH_ACCEPTED.value)
    assert len(logs) == 1
    assert logs[0].user_id == user_id
    details = json.loads(logs[0].details)
    assert details["match_id"] == str(match.id)


def test_handle_refuses_non_owner(
    handler: AcceptActionHandler,
    match_repo: InMemoryVacancyMatchRepository,
    profile_repo: InMemorySearchProfileRepository,
    telegram_account_repo: InMemoryTelegramAccountRepository,
    user_id: uuid.UUID,
    other_user_id: uuid.UUID,
    telegram_user_id: int,
) -> None:
    """A user trying to accept another user's match must get an error and no state change."""
    match = _seed_match(match_repo, profile_repo, user_id=other_user_id)
    _link_telegram(telegram_account_repo, user_id=user_id, telegram_user_id=telegram_user_id)

    response = handler.handle(
        chat_id=100,
        telegram_user_id=telegram_user_id,
        command=parse_accept_command(f"/accept {match.id}"),
    )

    assert isinstance(response, SendMessageRequest)
    text = response.text.lower()
    assert "forbidden" in text or "not" in text or "cannot" in text or "don't" in text
    # State was not updated.
    assert match_repo.get_by_id(match.id).status == MatchStatus.NEW.value


def test_handle_refuses_accept_from_rejected_status(
    handler: AcceptActionHandler,
    match_repo: InMemoryVacancyMatchRepository,
    profile_repo: InMemorySearchProfileRepository,
    telegram_account_repo: InMemoryTelegramAccountRepository,
    user_id: uuid.UUID,
    telegram_user_id: int,
) -> None:
    """Accepting a match that is already rejected must return an error."""
    match = _seed_match(
        match_repo, profile_repo, user_id=user_id, status=MatchStatus.REJECTED.value
    )
    _link_telegram(telegram_account_repo, user_id=user_id, telegram_user_id=telegram_user_id)

    response = handler.handle(
        chat_id=100,
        telegram_user_id=telegram_user_id,
        command=parse_accept_command(f"/accept {match.id}"),
    )

    assert isinstance(response, SendMessageRequest)
    text = response.text.lower()
    assert "already" in text or "cannot" in text or "invalid" in text or "not allowed" in text
    # State was not changed.
    assert match_repo.get_by_id(match.id).status == MatchStatus.REJECTED.value


def test_handle_refuses_accept_from_applied_status(
    handler: AcceptActionHandler,
    match_repo: InMemoryVacancyMatchRepository,
    profile_repo: InMemorySearchProfileRepository,
    telegram_account_repo: InMemoryTelegramAccountRepository,
    user_id: uuid.UUID,
    telegram_user_id: int,
) -> None:
    """Accepting a match that has been applied to must return an error."""
    match = _seed_match(match_repo, profile_repo, user_id=user_id, status=MatchStatus.APPLIED.value)
    _link_telegram(telegram_account_repo, user_id=user_id, telegram_user_id=telegram_user_id)

    response = handler.handle(
        chat_id=100,
        telegram_user_id=telegram_user_id,
        command=parse_accept_command(f"/accept {match.id}"),
    )

    assert isinstance(response, SendMessageRequest)
    text = response.text.lower()
    assert "already" in text or "cannot" in text or "invalid" in text or "not allowed" in text
    # State was not changed.
    assert match_repo.get_by_id(match.id).status == MatchStatus.APPLIED.value


def test_handle_rejects_unlinked_telegram_account(
    handler: AcceptActionHandler,
    match_repo: InMemoryVacancyMatchRepository,
    profile_repo: InMemorySearchProfileRepository,
    user_id: uuid.UUID,
    telegram_user_id: int,
) -> None:
    """An update from a Telegram user with no linked account must be refused."""
    match = _seed_match(match_repo, profile_repo, user_id=user_id)

    response = handler.handle(
        chat_id=100,
        telegram_user_id=telegram_user_id,
        command=parse_accept_command(f"/accept {match.id}"),
    )

    assert isinstance(response, SendMessageRequest)
    text = response.text.lower()
    assert "link" in text or "not linked" in text or "unknown" in text
    # State was not updated.
    assert match_repo.get_by_id(match.id).status == MatchStatus.NEW.value


# ---------------------------------------------------------------------------
# Bot dispatcher integration
# ---------------------------------------------------------------------------


def _accept_update(
    text: str,
    *,
    chat_id: int = 800,
    telegram_user_id: int = 800,
) -> dict:
    """Build a minimal Telegram Update carrying a /accept text message."""
    return {
        "update_id": 8000,
        "message": {
            "message_id": 80,
            "date": 0,
            "chat": {"id": chat_id, "type": "private"},
            "from": {
                "id": telegram_user_id,
                "is_bot": False,
                "first_name": "Dave",
            },
            "text": text,
        },
    }


def test_dispatcher_routes_accept_command(
    match_service: MatchService,
    match_repo: InMemoryVacancyMatchRepository,
    profile_repo: InMemorySearchProfileRepository,
    telegram_account_repo: InMemoryTelegramAccountRepository,
    audit_service: AuditService,
    user_id: uuid.UUID,
) -> None:
    """The bot must delegate ``/accept <id>`` to the AcceptActionHandler."""
    match = _seed_match(match_repo, profile_repo, user_id=user_id)
    telegram_user_id = 800
    _link_telegram(telegram_account_repo, user_id=user_id, telegram_user_id=telegram_user_id)

    handler = AcceptActionHandler(
        match_service=match_service,
        telegram_account_repo=telegram_account_repo,
        audit_service=audit_service,
    )
    bot = TelegramBot(
        settings=TelegramSettings(bot_token="test-token", polling_timeout=30),
        accept_handler=handler,
    )

    response = bot.handle_update(_accept_update(f"/accept {match.id}", telegram_user_id=800))

    assert response is not None
    text = response.text.lower()
    assert "accepted" in text
    assert match_repo.get_by_id(match.id).status == MatchStatus.ACCEPTED.value
