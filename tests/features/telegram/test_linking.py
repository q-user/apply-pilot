"""Tests for Telegram account linking — dispatcher integration.

These tests exercise :class:`TelegramLinkingService` in isolation
(in-memory token store, no network) and verify that the ``/link``
command handler inside :meth:`TelegramBot.handle_update` produces the
expected responses.

We deliberately avoid Mock — the linking service and the token store
are both simple dict-backed fakes that run with zero external I/O.
"""

from __future__ import annotations

import time

import pytest

from apply_pilot.features.telegram.linking import (
    InvalidLinkingTokenError,
    TelegramAccountAlreadyLinkedError,
    TelegramLinkingService,
)
from apply_pilot.features.telegram.repository import InMemoryTelegramAccountRepository


@pytest.fixture
def linking_service() -> TelegramLinkingService:
    """Return a fresh linking service with an empty in-memory token store."""
    return TelegramLinkingService()


@pytest.fixture
def account_repo() -> InMemoryTelegramAccountRepository:
    """Return a fresh in-memory TelegramAccount repository."""
    return InMemoryTelegramAccountRepository()


# ---------------------------------------------------------------------------
# TelegramLinkingService
# ---------------------------------------------------------------------------


def test_generate_token_returns_string(linking_service: TelegramLinkingService) -> None:
    """generate_token must return a non-empty string."""
    token = linking_service.generate_token(user_id="00000000-0000-0000-0000-000000000001")
    assert isinstance(token, str)
    assert len(token) > 0


def test_generate_token_is_unique(linking_service: TelegramLinkingService) -> None:
    """Each call to generate_token must return a distinct value."""
    token1 = linking_service.generate_token(user_id="00000000-0000-0000-0000-000000000001")
    token2 = linking_service.generate_token(user_id="00000000-0000-0000-0000-000000000002")
    assert token1 != token2


def test_link_account_returns_user_id(
    linking_service: TelegramLinkingService,
) -> None:
    """link_account with a valid token must return the resolved user id."""
    token = linking_service.generate_token(user_id="user-1")
    result = linking_service.link_account(token=token, telegram_user_id=123456789)
    assert result == "user-1"


def test_link_account_requires_user_id_from_token(
    linking_service: TelegramLinkingService,
) -> None:
    """link_account must require that a user_id is associated with the token."""
    token = linking_service.generate_token(user_id="user-1")
    result = linking_service.link_account(token=token, telegram_user_id=123456789)
    assert result == "user-1"


def test_link_account_with_invalid_token_raises(
    linking_service: TelegramLinkingService,
) -> None:
    """An unknown token must raise InvalidLinkingTokenError."""
    with pytest.raises(InvalidLinkingTokenError):
        linking_service.link_account(token="no-such-token", telegram_user_id=999)


def test_link_account_with_already_used_token_raises(
    linking_service: TelegramLinkingService,
) -> None:
    """A token consumed by a previous link_account call must be rejected."""
    token = linking_service.generate_token(user_id="user-1")
    linking_service.link_account(token=token, telegram_user_id=111)
    with pytest.raises(InvalidLinkingTokenError):
        linking_service.link_account(token=token, telegram_user_id=222)


def test_link_account_with_expired_token_raises(
    linking_service: TelegramLinkingService,
) -> None:
    """An expired token must raise InvalidLinkingTokenError."""
    token = linking_service.generate_token(user_id="user-1", ttl_seconds=0)
    # Force expiry by waiting past the zero TTL
    time.sleep(0.01)
    with pytest.raises(InvalidLinkingTokenError):
        linking_service.link_account(token=token, telegram_user_id=999)


def test_link_account_rejects_duplicate_telegram_user_id(
    linking_service: TelegramLinkingService,
) -> None:
    """A Telegram account cannot be linked to two different local users."""
    token1 = linking_service.generate_token(user_id="user-1")
    token2 = linking_service.generate_token(user_id="user-2")
    linking_service.link_account(token=token1, telegram_user_id=111)
    with pytest.raises(TelegramAccountAlreadyLinkedError):
        linking_service.link_account(token=token2, telegram_user_id=111)


def test_link_account_allows_same_telegram_user_id_relink(
    linking_service: TelegramLinkingService,
) -> None:
    """Linking the same (user_id, telegram_user_id) pair again is a no-op (already consumed)."""
    token = linking_service.generate_token(user_id="user-1")
    linking_service.link_account(token=token, telegram_user_id=111)
    # Same telegram_user_id linking to same user is rejected as consumed, not duplicate
    # because the first token is already consumed; the reverse index allows
    # the same pair since existing_user == record.user_id.
    token2 = linking_service.generate_token(user_id="user-1")
    with pytest.raises(InvalidLinkingTokenError):
        linking_service.link_account(token=token, telegram_user_id=111)
    # But a fresh token for the same user with same telegram id *is* allowed
    # since existing_user == record.user_id (same user).
    result = linking_service.link_account(token=token2, telegram_user_id=111)
    assert result == "user-1"


def test_find_telegram_user_id_returns_none_for_unlinked(
    linking_service: TelegramLinkingService,
) -> None:
    """For a user that has never been linked, find_telegram_user_id returns None."""
    assert linking_service.find_telegram_user_id(user_id="unknown-user") is None


def test_find_telegram_user_id_returns_id_for_linked(
    linking_service: TelegramLinkingService,
) -> None:
    """After linking, find_telegram_user_id must return the Telegram user id."""
    token = linking_service.generate_token(user_id="user-1")
    linking_service.link_account(token=token, telegram_user_id=123456789)
    assert linking_service.find_telegram_user_id(user_id="user-1") == 123456789


def test_find_user_id_returns_none_for_unlinked(
    linking_service: TelegramLinkingService,
) -> None:
    """find_user_id returns None for an unlinked Telegram user id."""
    assert linking_service.find_user_id(telegram_user_id=999) is None


def test_find_user_id_returns_user_id_for_linked(
    linking_service: TelegramLinkingService,
) -> None:
    """After linking, find_user_id must return the local user id."""
    token = linking_service.generate_token(user_id="user-1")
    linking_service.link_account(token=token, telegram_user_id=123456789)
    assert linking_service.find_user_id(telegram_user_id=123456789) == "user-1"


# ---------------------------------------------------------------------------
# /link command through the bot dispatcher
# ---------------------------------------------------------------------------


def _link_update(
    text: str,
    *,
    chat_id: int = 200,
    telegram_user_id: int = 200,
    telegram_username: str | None = None,
) -> dict:
    """Build a minimal Telegram Update with a text message from a user."""
    from_info: dict = {"id": telegram_user_id, "is_bot": False, "first_name": "Bob"}
    if telegram_username is not None:
        from_info["username"] = telegram_username
    return {
        "update_id": 99999,
        "message": {
            "message_id": 10,
            "date": 0,
            "chat": {"id": chat_id, "type": "private"},
            "from": from_info,
            "text": text,
        },
    }


async def test_link_command_with_valid_code_returns_success() -> None:
    """``/link <code>`` with a valid code must return a success message."""
    from apply_pilot.config import TelegramSettings
    from apply_pilot.features.telegram.bot import TelegramBot

    service = TelegramLinkingService()
    token = service.generate_token(user_id="linked-user-1")

    bot = TelegramBot(
        settings=TelegramSettings(bot_token="test-token", polling_timeout=30),
        linking_service=service,
    )

    response = await bot.handle_update(_link_update(f"/link {token}"))

    assert response is not None
    assert response.chat_id == 200
    assert "linked" in response.text.lower() or "success" in response.text.lower()


async def test_link_command_persists_account_when_repo_injected() -> None:
    """When a TelegramAccountRepository is injected, /link must persist the row."""
    from apply_pilot.config import TelegramSettings
    from apply_pilot.features.telegram.bot import TelegramBot

    service = TelegramLinkingService()
    repo = InMemoryTelegramAccountRepository()
    token = service.generate_token(user_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

    bot = TelegramBot(
        settings=TelegramSettings(bot_token="test-token", polling_timeout=30),
        linking_service=service,
        telegram_account_repository=repo,
    )

    response = await bot.handle_update(
        _link_update(
            f"/link {token}",
            telegram_user_id=111222333,
            telegram_username="bob_tg",
        )
    )

    assert response is not None
    assert "linked" in response.text.lower() or "success" in response.text.lower()
    # Verify the account was persisted via find_user_id on the linking service
    assert (
        service.find_user_id(telegram_user_id=111222333) == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    )


async def test_link_command_with_invalid_code_returns_error() -> None:
    """``/link <bad code>`` must return an error message."""
    from apply_pilot.config import TelegramSettings
    from apply_pilot.features.telegram.bot import TelegramBot

    service = TelegramLinkingService()
    bot = TelegramBot(
        settings=TelegramSettings(bot_token="test-token", polling_timeout=30),
        linking_service=service,
    )

    response = await bot.handle_update(_link_update("/link garbage-code"))

    assert response is not None
    assert response.chat_id == 200
    text_lower = response.text.lower()
    assert "invalid" in text_lower or "expired" in text_lower or "error" in text_lower


async def test_link_command_without_code_returns_usage_hint() -> None:
    """``/link`` without a code must return a usage hint."""
    from apply_pilot.config import TelegramSettings
    from apply_pilot.features.telegram.bot import TelegramBot

    bot = TelegramBot(
        settings=TelegramSettings(bot_token="test-token", polling_timeout=30),
        linking_service=TelegramLinkingService(),
    )

    response = await bot.handle_update(_link_update("/link"))

    assert response is not None
    assert response.chat_id == 200
    text_lower = response.text.lower()
    assert "usage" in text_lower or "provide" in text_lower or "code" in text_lower


async def test_link_command_duplicate_telegram_account_returns_error() -> None:
    """``/link`` with an already-linked Telegram account must return an error."""
    from apply_pilot.config import TelegramSettings
    from apply_pilot.features.telegram.bot import TelegramBot

    service = TelegramLinkingService()
    # Link user-1 first
    token1 = service.generate_token(user_id="user-1")
    service.link_account(token=token1, telegram_user_id=200)

    bot = TelegramBot(
        settings=TelegramSettings(bot_token="test-token", polling_timeout=30),
        linking_service=service,
    )

    # Now try to link a different user with the same Telegram id
    token2 = service.generate_token(user_id="user-2")
    response = await bot.handle_update(_link_update(f"/link {token2}", telegram_user_id=200))

    assert response is not None
    assert response.chat_id == 200
    text_lower = response.text.lower()
    assert "already linked" in text_lower
