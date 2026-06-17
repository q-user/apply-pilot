"""Telegram-channels client (M7, issue #58).

:class:`TelegramChannelClient` is the narrow contract between the
adapter / scanner and the underlying Telegram transport (TDLib,
``python-telegram-bot``, or a custom polling client). The contract is
intentionally minimal — ``list_channels`` returns the slice's static
configuration, and ``fetch_new_messages`` pulls any messages that
arrived since the last call.

The protocol is :class:`typing.Protocol` so the tests can inject an
in-memory fake without subclassing. The production wiring will plug
in a real transport in a follow-up; the slice does not depend on
``python-telegram-bot`` or TDLib at runtime, and tests never touch
the network.

Drain semantics
---------------

:func:`TelegramChannelClient.fetch_new_messages` is **destructive**
by default — once a message has been pulled, the transport should
not return it again. That mirrors the way the bot's long-poll
loop works (the ``offset`` cursor advances past every read update)
and means the scanner does not need to keep its own cursor state.
The ``keep_buffer`` parameter exists for tests that want to inspect
the same message twice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from apply_pilot.features.telegram_channels.config import TelegramChannelConfig


@dataclass(frozen=True, slots=True)
class TelegramChannelMessage:
    """A single message pulled from a Telegram channel.

    The ``(channel_id, message_id)`` pair is the natural key — the
    adapter uses it to build the
    :attr:`~apply_pilot.features.sources.models.Vacancy.source_id` for
    deduplication.

    Attributes:
        channel_id: Channel handle (``@username``) or numeric id
            (``-100…``).
        message_id: Telegram's monotonically-increasing message id.
        text: Raw post text. ``None`` for media-only posts; the
            classifier treats those as non-vacancies.
        author: Optional display name of the post author (the
            channel itself, for anonymous channels).
        published_at: Optional message timestamp; ``None`` when the
            transport does not provide it.
        extra: Source-specific extension point for follow-up
            transport implementations (TDLib ``Message`` fields,
            ``python-telegram-bot`` ``Message`` fields, etc.).
    """

    channel_id: str
    message_id: int
    text: str | None
    author: str | None = None
    published_at: datetime | None = None
    extra: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.channel_id or not self.channel_id.strip():
            raise ValueError("TelegramChannelMessage.channel_id must be a non-empty string.")
        if self.message_id <= 0:
            raise ValueError(
                f"TelegramChannelMessage.message_id must be a positive integer, "
                f"got {self.message_id!r}."
            )

    def to_raw_dict(self) -> dict[str, Any]:
        """Serialise to the dict shape the normaliser expects.

        The dict is what the adapter passes through ``search()`` and
        what the normaliser / repository store in
        :attr:`Vacancy.raw_data`. Keeping the serialisation in one
        place means the normaliser never has to know about
        :class:`TelegramChannelMessage`.
        """
        return {
            "channel_id": self.channel_id,
            "message_id": self.message_id,
            "text": self.text,
            "author": self.author,
            "published_at": self.published_at.isoformat() if self.published_at else None,
        }


@runtime_checkable
class TelegramChannelClient(Protocol):
    """The narrow contract every Telegram-channel transport implements.

    Methods
    -------
    list_channels:
        Return the slice's static channel configuration. The scanner
        calls this once at start-up; the production wiring can
        refresh it later if the env var changes.
    fetch_new_messages:
        Return any messages that arrived since the last call. The
        default behaviour is destructive: a successful call advances
        the transport's cursor past every returned message, and a
        subsequent call returns only newer traffic. ``keep_buffer=True``
        is the test-friendly override.
    """

    def list_channels(self) -> list[TelegramChannelConfig]: ...

    async def fetch_new_messages(
        self, channel_id: str, *, keep_buffer: bool = False
    ) -> list[TelegramChannelMessage]: ...


class InMemoryTelegramChannelClient:
    """Dict-backed fake used by every other test in the slice.

    The fake's job is to simulate the Telegram API surface so the
    adapter / scanner can be exercised end to end without touching
    the network. Two pieces of state:

    * ``_channels`` — the static channel list (mirrors what a real
      transport would return from ``getChannels`` / similar).
    * ``_messages_by_channel`` — the per-channel pending message
      buffer. ``fetch_new_messages`` drains it by default; tests can
      use ``add_message`` to push new traffic between assertions.

    The fake's "drain" semantics match the protocol contract: a
    successful ``fetch_new_messages`` call empties the channel
    buffer so a subsequent call yields nothing. ``keep_buffer=True``
    is the escape hatch for tests that need to re-read.
    """

    def __init__(
        self,
        channels: list[TelegramChannelConfig] | None = None,
        messages_by_channel: dict[str, list[TelegramChannelMessage]] | None = None,
    ) -> None:
        self._channels: list[TelegramChannelConfig] = list(channels or [])
        self._messages_by_channel: dict[str, list[TelegramChannelMessage]] = {
            cid: list(messages) for cid, messages in (messages_by_channel or {}).items()
        }

    def list_channels(self) -> list[TelegramChannelConfig]:
        """Return the slice's static channel configuration."""
        return list(self._channels)

    async def fetch_new_messages(
        self, channel_id: str, *, keep_buffer: bool = False
    ) -> list[TelegramChannelMessage]:
        """Return and (by default) drain pending messages for ``channel_id``.

        Unknown channels yield an empty list — the scanner treats
        "no such channel" and "no pending messages" identically so a
        transient race between the slice's channel list and the
        transport does not crash the loop.
        """
        pending = self._messages_by_channel.get(channel_id, [])
        snapshot = list(pending)
        if not keep_buffer:
            self._messages_by_channel[channel_id] = []
        return snapshot

    def add_message(self, message: TelegramChannelMessage) -> None:
        """Test convenience: push ``message`` onto the channel's pending buffer."""
        self._messages_by_channel.setdefault(message.channel_id, []).append(message)


__all__ = [
    "InMemoryTelegramChannelClient",
    "TelegramChannelClient",
    "TelegramChannelMessage",
]
