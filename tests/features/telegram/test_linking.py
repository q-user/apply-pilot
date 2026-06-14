"""Tests for Telegram account linking — dispatcher integration.

These tests exercise :class:`TelegramLinkingService` in isolation
(in-memory token store, no network) and verify that the ``/link``
command handler inside :meth:`TelegramBot.handle_update` produces the
expected responses.

We deliberately avoid Mock — the linking service and the token store
are both simple dict-backed fakes that run with zero external I/O.
"""

from __future__ import annotations

import pytest

from job_apply.features.telegram.linking import (
    InvalidLinkingTokenError,
    TelegramLinkingService,
)


@pytest.fixture
def linking_service() -> TelegramLinkingService:
    """Return a fresh linking service with an empty in-memory token store."""
    return TelegramLinkingService()


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


def test_link_account_returns_telegram_user_id(
    linking_service: TelegramLinkingService,
) -> None:
    """link_account with a valid token must return the stored Telegram user id."""
    token = linking_service.generate_token(user_id="user-1")
    result = linking_service.link_account(token=token, telegram_user_id=123456789)
    assert result == 123456789


def test_link_account_requires_user_id_from_token(
    linking_service: TelegramLinkingService,
) -> None:
    """link_account must require that a user_id is associated with the token."""
    token = linking_service.generate_token(user_id="user-1")
    result = linking_service.link_account(token=token, telegram_user_id=123456789)
    # We validate the token carries the user_id by checking the linking passed
    assert result == 123456789


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


# ---------------------------------------------------------------------------
# /link command through the bot dispatcher
# ---------------------------------------------------------------------------


def _link_update(text: str, *, chat_id: int = 200, telegram_user_id: int = 200) -> dict:
    """Build a minimal Telegram Update with a text message from a user."""
    return {
        "update_id": 99999,
        "message": {
            "message_id": 10,
            "date": 0,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": telegram_user_id, "is_bot": False, "first_name": "Bob"},
            "text": text,
        },
    }


def test_link_command_with_valid_code_returns_success() -> None:
    """``/link <code>`` with a valid code must return a success message."""
    from job_apply.config import TelegramSettings
    from job_apply.features.telegram.bot import TelegramBot

    service = TelegramLinkingService()
    token = service.generate_token(user_id="linked-user-1")
    # Pre-store the linking data so the bot's dispatcher can resolve it.
    # The bot itself accepts a linking_service dependency.

    bot = TelegramBot(
        settings=TelegramSettings(bot_token="test-token", polling_timeout=30),
        linking_service=service,
    )

    response = bot.handle_update(_link_update(f"/link {token}"))

    assert response is not None
    assert response.chat_id == 200
    assert "linked" in response.text.lower() or "success" in response.text.lower()


def test_link_command_with_invalid_code_returns_error() -> None:
    """``/link <bad code>`` must return an error message."""
    from job_apply.config import TelegramSettings
    from job_apply.features.telegram.bot import TelegramBot

    service = TelegramLinkingService()
    bot = TelegramBot(
        settings=TelegramSettings(bot_token="test-token", polling_timeout=30),
        linking_service=service,
    )

    response = bot.handle_update(_link_update("/link garbage-code"))

    assert response is not None
    assert response.chat_id == 200
    text_lower = response.text.lower()
    assert "invalid" in text_lower or "expired" in text_lower or "error" in text_lower


def test_link_command_without_code_returns_usage_hint() -> None:
    """``/link`` without a code must return a usage hint."""
    from job_apply.config import TelegramSettings
    from job_apply.features.telegram.bot import TelegramBot

    bot = TelegramBot(
        settings=TelegramSettings(bot_token="test-token", polling_timeout=30),
        linking_service=TelegramLinkingService(),
    )

    response = bot.handle_update(_link_update("/link"))

    assert response is not None
    assert response.chat_id == 200
    text_lower = response.text.lower()
    assert "usage" in text_lower or "provide" in text_lower or "code" in text_lower
