"""Tests for the Telegram bot dispatcher.

These tests exercise the pure dispatch logic in
:class:`job_apply.features.telegram.bot.TelegramBot.handle_update` and verify
that the long-running :class:`TelegramBotProcess` extends the project's
standard :class:`BaseProcess`. HTTP transport is intentionally not touched:
a real :class:`httpx.AsyncClient` is only created lazily on first use, so
constructing a ``TelegramBot`` with test settings does not open any sockets.
"""

from __future__ import annotations

from typing import Any

from job_apply.features.telegram.bot import TelegramBot, TelegramSettings
from job_apply.features.telegram.process import TelegramBotProcess
from job_apply.runtime.process import BaseProcess


def _start_update(text: str, *, chat_id: int = 100) -> dict[str, Any]:
    """Build a minimal Telegram Update payload carrying a text message."""
    return {
        "update_id": 12345,
        "message": {
            "message_id": 1,
            "date": 0,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": chat_id, "is_bot": False, "first_name": "Alice"},
            "text": text,
        },
    }


async def test_handle_start_command_returns_welcome() -> None:
    """``/start`` must return a welcome message that mentions account linking."""
    bot = TelegramBot(settings=TelegramSettings(bot_token="test-token", polling_timeout=30))

    response = await bot.handle_update(_start_update("/start"))

    assert response is not None
    assert response.chat_id == 100
    text = response.text
    assert "Welcome" in text
    # The skeleton must include a hint about Telegram account linking and a
    # placeholder for the one-time deep-link token referenced in the issue.
    assert "Telegram" in text
    assert "deep-link" in text.lower() or "link" in text.lower()


async def test_handle_help_command_returns_help() -> None:
    """``/help`` must list the available commands."""
    bot = TelegramBot(settings=TelegramSettings(bot_token="test-token", polling_timeout=30))

    response = await bot.handle_update(_start_update("/help"))

    assert response is not None
    assert response.chat_id == 100
    text = response.text
    assert "/start" in text
    assert "/help" in text


async def test_handle_unknown_command_returns_fallback() -> None:
    """An unknown command must produce a fallback message that points to /help."""
    bot = TelegramBot(settings=TelegramSettings(bot_token="test-token", polling_timeout=30))

    response = await bot.handle_update(_start_update("/notacommand"))

    assert response is not None
    assert response.chat_id == 100
    text = response.text
    # Fallback explicitly nudges the user to /help so the bot never dead-ends.
    assert "/help" in text


def test_bot_process_is_base_process_subclass() -> None:
    """``TelegramBotProcess`` must inherit from :class:`BaseProcess`.

    Inheriting is what wires the process into the project's standard
    graceful-shutdown / structured-logging story. ``issubclass`` is checked
    directly rather than via an instance because constructing a process
    should remain cheap and side-effect free for this test.
    """
    assert issubclass(TelegramBotProcess, BaseProcess)
