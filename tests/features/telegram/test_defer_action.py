"""TDD tests for the :class:`DeferActionHandler` (M4, issue #39).

The handler is the use-case for the ``/defer <match_id>`` Telegram
command. It:

* resolves the local ``user_id`` from the ``telegram_user_id`` of the
  incoming update;
* validates the target match exists and is owned by the resolved user;
* defers only from the allowed source states (``new``, ``scored``,
  ``review``, ``deferred`` — deferring an already-deferred match is a
  no-op that still records an audit event);
* flips the match's status to ``deferred`` via
  :meth:`MatchService.update_status`;
* records a ``MATCH_DEFERRED`` audit event with ``match_id`` in
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
from job_apply.features.telegram.actions.defer import (
    DeferActionHandler,
    parse_defer_command,
)
from job_apply.features.telegram.bot import TelegramBot, TelegramSettings
from job_apply.features.telegram.dto import SendMessageRequest
from job_apply.features.telegram.repository import InMemoryTelegramAccountRepository

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _vacancy(source_id: str = "hh-def-1", title: str = "Backend") -> Vacancy:
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
    return 626262


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
) -> DeferActionHandler:
    return DeferActionHandler(
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
# parse_defer_command
# ---------------------------------------------------------------------------


def test_defer_command_parses_match_id() -> None:
    """``/defer <match_id>`` parses to a DeferCommand with the match_id."""
    match_id = "11111111-1111-1111-1111-111111111111"
    command = parse_defer_command(f"/defer {match_id}")

    assert command is not None
    assert command.match_id == uuid.UUID(match_id)
    # raw_args is captured so the dispatcher can show the user's input back
    # to them when displaying the help text.
    assert command.raw_args == match_id


def test_defer_command_no_args_returns_help() -> None:
    """``/defer`` with no args must return None so the caller shows help text."""
    assert parse_defer_command("/defer") is None
    assert parse_defer_command("/defer   ") is None


def test_defer_command_with_invalid_uuid_returns_none() -> None:
    """``/defer <garbage>`` must return None so the caller shows usage text."""
    assert parse_defer_command("/defer not-a-uuid") is None


# ---------------------------------------------------------------------------
# DeferActionHandler.handle
# ---------------------------------------------------------------------------


def test_handle_defers_match_for_owner(
    handler: DeferActionHandler,
    match_repo: InMemoryVacancyMatchRepository,
    profile_repo: InMemorySearchProfileRepository,
    telegram_account_repo: InMemoryTelegramAccountRepository,
    user_id: uuid.UUID,
    telegram_user_id: int,
) -> None:
    """The owner can defer their own match and receive a confirmation message."""
    match = _seed_match(match_repo, profile_repo, user_id=user_id)
    _link_telegram(telegram_account_repo, user_id=user_id, telegram_user_id=telegram_user_id)

    response = handler.handle(
        chat_id=100,
        telegram_user_id=telegram_user_id,
        command=parse_defer_command(f"/defer {match.id}"),
    )

    assert isinstance(response, SendMessageRequest)
    assert response.chat_id == 100
    text = response.text.lower()
    assert str(match.id) in response.text
    assert "deferred" in text
    # State was updated to "deferred".
    assert match_repo.get_by_id(match.id).status == MatchStatus.DEFERRED.value


def test_handle_rejects_unknown_match(
    handler: DeferActionHandler,
    telegram_account_repo: InMemoryTelegramAccountRepository,
    user_id: uuid.UUID,
    telegram_user_id: int,
) -> None:
    """Deferring a match that does not exist must return a clear error."""
    _link_telegram(telegram_account_repo, user_id=user_id, telegram_user_id=telegram_user_id)
    missing_id = uuid.uuid4()

    response = handler.handle(
        chat_id=100,
        telegram_user_id=telegram_user_id,
        command=parse_defer_command(f"/defer {missing_id}"),
    )

    assert isinstance(response, SendMessageRequest)
    text = response.text.lower()
    assert "not found" in text or "unknown" in text
    # No side-effects: state is untouched and no audit event was written.


def test_handle_rejects_match_for_non_owner(
    handler: DeferActionHandler,
    match_repo: InMemoryVacancyMatchRepository,
    profile_repo: InMemorySearchProfileRepository,
    telegram_account_repo: InMemoryTelegramAccountRepository,
    audit_repo: InMemoryAuditLogRepository,
    user_id: uuid.UUID,
    other_user_id: uuid.UUID,
    telegram_user_id: int,
) -> None:
    """A user trying to defer another user's match must get an error and no state change."""
    match = _seed_match(match_repo, profile_repo, user_id=other_user_id)
    _link_telegram(telegram_account_repo, user_id=user_id, telegram_user_id=telegram_user_id)

    response = handler.handle(
        chat_id=100,
        telegram_user_id=telegram_user_id,
        command=parse_defer_command(f"/defer {match.id}"),
    )

    assert isinstance(response, SendMessageRequest)
    text = response.text.lower()
    assert "forbidden" in text or "not" in text or "cannot" in text or "don't" in text
    # State was not updated.
    assert match_repo.get_by_id(match.id).status == MatchStatus.NEW.value
    # No audit event was recorded.
    assert audit_repo.list_by_event_type(AuditEventType.MATCH_DEFERRED.value) == []


def test_handle_creates_audit_event(
    handler: DeferActionHandler,
    match_repo: InMemoryVacancyMatchRepository,
    profile_repo: InMemorySearchProfileRepository,
    telegram_account_repo: InMemoryTelegramAccountRepository,
    audit_repo: InMemoryAuditLogRepository,
    user_id: uuid.UUID,
    telegram_user_id: int,
) -> None:
    """A successful defer must record a MATCH_DEFERRED audit event with the match_id."""
    match = _seed_match(match_repo, profile_repo, user_id=user_id)
    _link_telegram(telegram_account_repo, user_id=user_id, telegram_user_id=telegram_user_id)

    response = handler.handle(
        chat_id=100,
        telegram_user_id=telegram_user_id,
        command=parse_defer_command(f"/defer {match.id}"),
    )

    assert isinstance(response, SendMessageRequest)
    # Audit event was recorded with match_id in details.
    logs = audit_repo.list_by_event_type(AuditEventType.MATCH_DEFERRED.value)
    assert len(logs) == 1
    assert logs[0].user_id == user_id
    details = json.loads(logs[0].details)
    assert details["match_id"] == str(match.id)


def test_handle_refuses_defer_from_rejected_status(
    handler: DeferActionHandler,
    match_repo: InMemoryVacancyMatchRepository,
    profile_repo: InMemorySearchProfileRepository,
    telegram_account_repo: InMemoryTelegramAccountRepository,
    user_id: uuid.UUID,
    telegram_user_id: int,
) -> None:
    """Deferring a match that is already rejected must return an error."""
    match = _seed_match(
        match_repo, profile_repo, user_id=user_id, status=MatchStatus.REJECTED.value
    )
    _link_telegram(telegram_account_repo, user_id=user_id, telegram_user_id=telegram_user_id)

    response = handler.handle(
        chat_id=100,
        telegram_user_id=telegram_user_id,
        command=parse_defer_command(f"/defer {match.id}"),
    )

    assert isinstance(response, SendMessageRequest)
    text = response.text.lower()
    assert "already" in text or "cannot" in text or "invalid" in text or "not allowed" in text
    # State was not changed.
    assert match_repo.get_by_id(match.id).status == MatchStatus.REJECTED.value


def test_handle_refuses_defer_from_accepted_status(
    handler: DeferActionHandler,
    match_repo: InMemoryVacancyMatchRepository,
    profile_repo: InMemorySearchProfileRepository,
    telegram_account_repo: InMemoryTelegramAccountRepository,
    user_id: uuid.UUID,
    telegram_user_id: int,
) -> None:
    """Deferring a match that has been accepted must return an error."""
    match = _seed_match(
        match_repo, profile_repo, user_id=user_id, status=MatchStatus.ACCEPTED.value
    )
    _link_telegram(telegram_account_repo, user_id=user_id, telegram_user_id=telegram_user_id)

    response = handler.handle(
        chat_id=100,
        telegram_user_id=telegram_user_id,
        command=parse_defer_command(f"/defer {match.id}"),
    )

    assert isinstance(response, SendMessageRequest)
    text = response.text.lower()
    assert "already" in text or "cannot" in text or "invalid" in text or "not allowed" in text
    # State was not changed.
    assert match_repo.get_by_id(match.id).status == MatchStatus.ACCEPTED.value


def test_handle_rejects_unlinked_telegram_account(
    handler: DeferActionHandler,
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
        command=parse_defer_command(f"/defer {match.id}"),
    )

    assert isinstance(response, SendMessageRequest)
    text = response.text.lower()
    assert "link" in text or "not linked" in text or "unknown" in text
    # State was not updated.
    assert match_repo.get_by_id(match.id).status == MatchStatus.NEW.value


@pytest.mark.parametrize("source_status", [MatchStatus.NEW, MatchStatus.SCORED, MatchStatus.REVIEW])
def test_handle_defers_from_each_allowed_status(
    handler: DeferActionHandler,
    match_repo: InMemoryVacancyMatchRepository,
    profile_repo: InMemorySearchProfileRepository,
    telegram_account_repo: InMemoryTelegramAccountRepository,
    user_id: uuid.UUID,
    telegram_user_id: int,
    source_status: MatchStatus,
) -> None:
    """Defer is allowed from ``new``, ``scored`` and ``review``."""
    match = _seed_match(match_repo, profile_repo, user_id=user_id, status=source_status.value)
    _link_telegram(telegram_account_repo, user_id=user_id, telegram_user_id=telegram_user_id)

    response = handler.handle(
        chat_id=100,
        telegram_user_id=telegram_user_id,
        command=parse_defer_command(f"/defer {match.id}"),
    )

    assert isinstance(response, SendMessageRequest)
    text = response.text.lower()
    assert "deferred" in text
    assert match_repo.get_by_id(match.id).status == MatchStatus.DEFERRED.value


def test_handle_defers_already_deferred_match(
    handler: DeferActionHandler,
    match_repo: InMemoryVacancyMatchRepository,
    profile_repo: InMemorySearchProfileRepository,
    telegram_account_repo: InMemoryTelegramAccountRepository,
    audit_repo: InMemoryAuditLogRepository,
    user_id: uuid.UUID,
    telegram_user_id: int,
) -> None:
    """Deferring an already-deferred match is allowed and records an audit event."""
    match = _seed_match(
        match_repo, profile_repo, user_id=user_id, status=MatchStatus.DEFERRED.value
    )
    _link_telegram(telegram_account_repo, user_id=user_id, telegram_user_id=telegram_user_id)

    response = handler.handle(
        chat_id=100,
        telegram_user_id=telegram_user_id,
        command=parse_defer_command(f"/defer {match.id}"),
    )

    assert isinstance(response, SendMessageRequest)
    text = response.text.lower()
    assert "deferred" in text
    # State stays "deferred" (idempotent).
    assert match_repo.get_by_id(match.id).status == MatchStatus.DEFERRED.value
    # Audit event was still recorded for the explicit user action.
    logs = audit_repo.list_by_event_type(AuditEventType.MATCH_DEFERRED.value)
    assert len(logs) == 1


# ---------------------------------------------------------------------------
# Bot dispatcher integration
# ---------------------------------------------------------------------------


def _defer_update(
    text: str,
    *,
    chat_id: int = 600,
    telegram_user_id: int = 600,
) -> dict:
    """Build a minimal Telegram Update carrying a /defer text message."""
    return {
        "update_id": 6000,
        "message": {
            "message_id": 60,
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


async def test_dispatcher_routes_defer_command(
    match_service: MatchService,
    match_repo: InMemoryVacancyMatchRepository,
    profile_repo: InMemorySearchProfileRepository,
    telegram_account_repo: InMemoryTelegramAccountRepository,
    audit_service: AuditService,
    user_id: uuid.UUID,
) -> None:
    """The bot must delegate ``/defer <id>`` to the DeferActionHandler."""
    match = _seed_match(match_repo, profile_repo, user_id=user_id)
    telegram_user_id = 600
    _link_telegram(telegram_account_repo, user_id=user_id, telegram_user_id=telegram_user_id)

    handler = DeferActionHandler(
        match_service=match_service,
        telegram_account_repo=telegram_account_repo,
        audit_service=audit_service,
    )
    bot = TelegramBot(
        settings=TelegramSettings(bot_token="test-token", polling_timeout=30),
        defer_handler=handler,
    )

    response = await bot.handle_update(_defer_update(f"/defer {match.id}", telegram_user_id=600))

    assert response is not None
    text = response.text.lower()
    assert "deferred" in text
    assert match_repo.get_by_id(match.id).status == MatchStatus.DEFERRED.value


async def test_dispatcher_defer_command_without_args_returns_help() -> None:
    """``/defer`` without args must return the help text (not a crash)."""
    bot = TelegramBot(
        settings=TelegramSettings(bot_token="test-token", polling_timeout=30),
        defer_handler=DeferActionHandler(
            match_service=MatchService(
                match_repo=InMemoryVacancyMatchRepository(),
                profile_repo=InMemorySearchProfileRepository(),
            ),
            telegram_account_repo=InMemoryTelegramAccountRepository(),
            audit_service=AuditService(audit_repo=InMemoryAuditLogRepository()),
        ),
    )

    response = await bot.handle_update(_defer_update("/defer"))

    assert response is not None
    text = response.text.lower()
    assert "usage" in text or "defer" in text


async def test_dispatcher_includes_defer_in_help() -> None:
    """``/help`` must list ``/defer`` so users discover the command."""
    bot = TelegramBot(settings=TelegramSettings(bot_token="test-token", polling_timeout=30))

    response = await bot.handle_update(_defer_update("/help"))

    assert response is not None
    assert "/defer" in response.text
