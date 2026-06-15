"""Telegram bot feature slice.

Contains the command-dispatcher :class:`TelegramBot` and the long-running
:class:`TelegramBotProcess` that polls the Telegram Bot API. The slice owns
its own DTOs (``SendMessageRequest``) and the small :class:`TelegramSettings`
wrapper used by the dispatcher; cross-slice dependencies stay on
:mod:`job_apply.runtime.process` and :mod:`job_apply.config`.
"""

from job_apply.features.telegram.bot import TelegramBot, TelegramSettings
from job_apply.features.telegram.dto import SendMessageRequest
from job_apply.features.telegram.linking import (
    InvalidLinkingTokenError,
    TelegramAccountAlreadyLinkedError,
    TelegramLinkingService,
)
from job_apply.features.telegram.models import TelegramAccount
from job_apply.features.telegram.process import TelegramBotProcess, main
from job_apply.features.telegram.repository import (
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
