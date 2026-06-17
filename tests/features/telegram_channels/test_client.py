"""TDD tests for the Telegram-channels client (M7, issue #58).

The :class:`TelegramChannelClient` is the narrow contract between the
adapter / scanner and the underlying Telegram transport (TDLib,
``python-telegram-bot``, or any other). The contract is intentionally
minimal: a list of channel configs the watcher should poll, and a
"fetch new messages" hook that the transport implements.

The :class:`InMemoryTelegramChannelClient` is a dict-backed fake used
by every other test in the slice. The fake's job is to simulate the
Telegram API surface so the adapter / scanner can be exercised end to
end without touching the network.
"""

from __future__ import annotations

import pytest

from job_apply.features.telegram_channels import (
    InMemoryTelegramChannelClient,
    TelegramChannelClient,
    TelegramChannelConfig,
    TelegramChannelMessage,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _vacancy_message(
    *,
    channel_id: str = "@jobs",
    message_id: int = 1,
    text: str = "#vacancy Senior Python Developer",
    author: str | None = "JobBot",
) -> TelegramChannelMessage:
    """Build a minimal :class:`TelegramChannelMessage`."""
    return TelegramChannelMessage(
        channel_id=channel_id,
        message_id=message_id,
        text=text,
        author=author,
    )


# ---------------------------------------------------------------------------
# TelegramChannelMessage
# ---------------------------------------------------------------------------


class TestTelegramChannelMessage:
    def test_required_fields(self) -> None:
        """``channel_id`` and ``message_id`` are the natural key."""
        msg = _vacancy_message()
        assert msg.channel_id == "@jobs"
        assert msg.message_id == 1

    def test_text_preserved(self) -> None:
        """The message text round-trips verbatim."""
        msg = _vacancy_message(text="Hello, world!")
        assert msg.text == "Hello, world!"

    def test_author_optional(self) -> None:
        """``author`` is optional and defaults to ``None``."""
        msg = TelegramChannelMessage(channel_id="@jobs", message_id=1, text="x")
        assert msg.author is None

    def test_published_at_optional(self) -> None:
        """``published_at`` is optional and defaults to ``None``."""
        msg = TelegramChannelMessage(channel_id="@jobs", message_id=1, text="x")
        assert msg.published_at is None

    def test_invalid_message_id_raises(self) -> None:
        """``message_id`` must be positive — Telegram's id space is positive."""
        with pytest.raises(ValueError, match="message_id"):
            TelegramChannelMessage(channel_id="@jobs", message_id=0, text="x")

    def test_empty_channel_id_raises(self) -> None:
        """An empty ``channel_id`` is rejected."""
        with pytest.raises(ValueError, match="channel_id"):
            TelegramChannelMessage(channel_id="", message_id=1, text="x")


# ---------------------------------------------------------------------------
# InMemoryTelegramChannelClient
# ---------------------------------------------------------------------------


class TestInMemoryTelegramChannelClient:
    def test_implements_protocol(self) -> None:
        """The in-memory fake is a structural :class:`TelegramChannelClient`."""
        client: TelegramChannelClient = InMemoryTelegramChannelClient()
        assert isinstance(client, TelegramChannelClient)

    def test_fetch_new_messages_returns_seeded_messages(self) -> None:
        """``fetch_new_messages`` returns the messages seeded for the channel."""
        messages = [_vacancy_message(message_id=1), _vacancy_message(message_id=2)]
        client = InMemoryTelegramChannelClient(
            channels=[TelegramChannelConfig(identifier="@jobs")],
            messages_by_channel={"@jobs": messages},
        )

        result = asyncio_run(client.fetch_new_messages("@jobs"))

        assert result == messages

    def test_fetch_new_messages_unknown_channel_returns_empty(self) -> None:
        """An unknown channel yields an empty list (not an error)."""
        client = InMemoryTelegramChannelClient(
            channels=[TelegramChannelConfig(identifier="@jobs")],
        )

        result = asyncio_run(client.fetch_new_messages("@unknown"))

        assert result == []

    def test_fetch_new_messages_drains_by_default(self) -> None:
        """After a fetch, the channel is empty so the next call yields nothing.

        The scanner relies on this drain semantics — once a message has
        been pulled, it should not appear again on the next poll.
        """
        client = InMemoryTelegramChannelClient(
            channels=[TelegramChannelConfig(identifier="@jobs")],
            messages_by_channel={"@jobs": [_vacancy_message(message_id=1)]},
        )

        first = asyncio_run(client.fetch_new_messages("@jobs"))
        second = asyncio_run(client.fetch_new_messages("@jobs"))

        assert len(first) == 1
        assert second == []

    def test_fetch_new_messages_keep_buffer_does_not_drain(self) -> None:
        """``keep_buffer=True`` leaves the message in place (re-pollable)."""
        client = InMemoryTelegramChannelClient(
            channels=[TelegramChannelConfig(identifier="@jobs")],
            messages_by_channel={"@jobs": [_vacancy_message(message_id=1)]},
        )

        first = asyncio_run(client.fetch_new_messages("@jobs", keep_buffer=True))
        second = asyncio_run(client.fetch_new_messages("@jobs", keep_buffer=True))

        assert first == second
        assert len(first) == 1

    def test_add_message_appends_to_channel(self) -> None:
        """``add_message`` is a test convenience to simulate new traffic."""
        client = InMemoryTelegramChannelClient(
            channels=[TelegramChannelConfig(identifier="@jobs")],
        )

        client.add_message(_vacancy_message(message_id=1))
        result = asyncio_run(client.fetch_new_messages("@jobs"))

        assert len(result) == 1
        assert result[0].message_id == 1

    def test_add_message_unknown_channel_appends(self) -> None:
        """``add_message`` does not require the channel to be pre-declared."""
        client = InMemoryTelegramChannelClient()

        client.add_message(_vacancy_message(channel_id="@adhoc", message_id=1))
        result = asyncio_run(client.fetch_new_messages("@adhoc"))

        assert len(result) == 1

    def test_list_channels(self) -> None:
        """``list_channels`` returns every configured :class:`TelegramChannelConfig`."""
        configs = [
            TelegramChannelConfig(identifier="@jobs"),
            TelegramChannelConfig(identifier="@remote", display_name="Remote"),
        ]
        client = InMemoryTelegramChannelClient(channels=configs)

        result = client.list_channels()

        assert result == configs

    def test_list_channels_empty_by_default(self) -> None:
        """A freshly-constructed client watches no channels."""
        client = InMemoryTelegramChannelClient()

        assert client.list_channels() == []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def asyncio_run(coro):  # type: ignore[no-untyped-def]
    """Run a coroutine to completion from a sync test."""
    import asyncio

    return asyncio.run(coro)
