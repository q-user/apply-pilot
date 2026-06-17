"""Telegram bot feature slice.

Contains the command-dispatcher :class:`TelegramBot` and the long-running
:class:`TelegramBotProcess` that polls the Telegram Bot API. The slice owns
its own DTOs (``SendMessageRequest``) and the small :class:`TelegramSettings`
wrapper used by the dispatcher; cross-slice dependencies stay on
:mod:`apply_pilot.runtime.process` and :mod:`apply_pilot.config`.
"""

from apply_pilot.features.telegram.bot import TelegramBot, TelegramSettings
from apply_pilot.features.telegram.dto import SendMessageRequest
from apply_pilot.features.telegram.linking import (
    InvalidLinkingTokenError,
    TelegramAccountAlreadyLinkedError,
    TelegramLinkingService,
)
from apply_pilot.features.telegram.models import TelegramAccount
from apply_pilot.features.telegram.process import TelegramBotProcess, main
from apply_pilot.features.telegram.repository import (
    InMemoryTelegramAccountRepository,
    SqlAlchemyTelegramAccountRepository,
    TelegramAccountRepository,
)

__all__ = [
    "InMemoryTelegramAccountRepository",
    "InvalidLinkingTokenError",
    "SendMessageRequest",
    "SqlAlchemyTelegramAccountRepository",
    "TelegramAccount",
    "TelegramAccountAlreadyLinkedError",
    "TelegramAccountRepository",
    "TelegramBot",
    "TelegramBotProcess",
    "TelegramLinkingService",
    "TelegramSettings",
    "main",
]
