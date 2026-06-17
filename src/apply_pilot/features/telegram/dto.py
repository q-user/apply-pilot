"""Telegram transport DTOs.

The :class:`SendMessageRequest` dataclass is the small, transport-agnostic
payload produced by command actions and consumed by the bot's HTTP
client. It lives in its own module so command actions can import it
without triggering a circular import with the bot's dispatcher.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SendMessageRequest:
    """A minimal DTO describing a chat reply produced by the dispatcher.

    Decoupling the dispatch decision from the HTTP transport keeps the
    command rules testable in isolation. :class:`TelegramBotProcess` is the
    only caller that turns these into actual ``sendMessage`` calls.
    """

    chat_id: int
    text: str


__all__ = ["SendMessageRequest"]
