"""TDD tests for the Telegram-channel :class:`SourceAdapter` (M7, issue #58).

The adapter is the slice's public surface — the cross-source
orchestration code (the future ``VacancySearchService`` migration, the
:class:`ApplyWorker`) only ever sees a
:class:`~apply_pilot.features.sources.adapter.SourceAdapter` Protocol.
:class:`TelegramChannelSourceAdapter` is the concrete implementation:

* :meth:`search` pulls messages from the configured
  :class:`TelegramChannelClient`, runs them through the classifier
  and the normaliser, and returns the raw dicts (the same shape the
  :class:`SourceAdapter` Protocol mandates).
* :meth:`apply` raises :class:`NotImplementedError` — channels are
  read-only sources; there is no programmatic apply via the channel.
* :meth:`extract_screening_questions` returns an empty list — Telegram
  channels do not carry structured screening questions.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

import pytest

from apply_pilot.features.apply_worker.models import ApplyJob
from apply_pilot.features.sources.adapter import (
    AdapterRegistry,
    SourceAdapter,
    SourceQuery,
)
from apply_pilot.features.telegram_channels import (
    InMemoryTelegramChannelClient,
    TelegramChannelClassifier,
    TelegramChannelConfig,
    TelegramChannelMessage,
    TelegramChannelSourceAdapter,
)
from apply_pilot.features.telegram_channels.normalizer import TelegramChannelNormalizer

# ---------------------------------------------------------------------------
# World fixture
# ---------------------------------------------------------------------------


def _msg(
    *,
    channel_id: str = "@jobs",
    message_id: int = 1,
    text: str = "#vacancy Senior Python Developer\nAcme Corp",
    author: str | None = None,
) -> TelegramChannelMessage:
    return TelegramChannelMessage(
        channel_id=channel_id, message_id=message_id, text=text, author=author
    )


@dataclass
class _TelegramChannelWorld:
    """Bundle of collaborators a :class:`TelegramChannelSourceAdapter` consumes in tests."""

    client: InMemoryTelegramChannelClient
    classifier: TelegramChannelClassifier
    normalizer: TelegramChannelNormalizer
    adapter: TelegramChannelSourceAdapter


def _make_world(
    *,
    channels: list[TelegramChannelConfig] | None = None,
    messages_by_channel: dict[str, list[TelegramChannelMessage]] | None = None,
    vacancy_markers: tuple[str, ...] | None = None,
) -> _TelegramChannelWorld:
    """Build a :class:`TelegramChannelSourceAdapter` wired to in-memory fakes."""
    channels = channels or [TelegramChannelConfig(identifier="@jobs")]
    client = InMemoryTelegramChannelClient(
        channels=channels,
        messages_by_channel=messages_by_channel or {},
    )
    classifier = TelegramChannelClassifier(vacancy_markers=vacancy_markers or ())
    normalizer = TelegramChannelNormalizer()
    adapter = TelegramChannelSourceAdapter(
        client=client,
        classifier=classifier,
        normalizer=normalizer,
        channels=channels,
    )
    return _TelegramChannelWorld(
        client=client,
        classifier=classifier,
        normalizer=normalizer,
        adapter=adapter,
    )


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestTelegramChannelAdapterProtocol:
    def test_satisfies_protocol(self) -> None:
        """The adapter is a structural :class:`SourceAdapter`."""
        world = _make_world()
        adapter: SourceAdapter = world.adapter
        assert isinstance(adapter, SourceAdapter)
        assert adapter.name == "telegram_channel"


# ---------------------------------------------------------------------------
# search() — pulls from the client, filters via the classifier
# ---------------------------------------------------------------------------


class TestTelegramChannelAdapterSearch:
    def test_search_returns_raw_dicts(self) -> None:
        """``search`` returns one raw dict per accepted message."""
        world = _make_world(
            channels=[TelegramChannelConfig(identifier="@jobs")],
            messages_by_channel={
                "@jobs": [
                    _msg(message_id=1, text="#vacancy Senior Python"),
                    _msg(message_id=2, text="#vacancy Go Developer"),
                ],
            },
        )

        result = asyncio_run(world.adapter.search(SourceQuery()))

        assert len(result) == 2
        for item in result:
            assert item["channel_id"] == "@jobs"
            assert item["text"].startswith("#vacancy")

    def test_search_skips_non_vacancy_posts(self) -> None:
        """Messages that do not match the classifier are dropped."""
        world = _make_world(
            channels=[TelegramChannelConfig(identifier="@jobs")],
            messages_by_channel={
                "@jobs": [
                    _msg(message_id=1, text="#vacancy Senior Python"),
                    _msg(message_id=2, text="Just chatting, no vacancy today"),
                ],
            },
        )

        result = asyncio_run(world.adapter.search(SourceQuery()))

        assert len(result) == 1
        assert result[0]["message_id"] == 1

    def test_search_iterates_every_configured_channel(self) -> None:
        """``search`` polls every channel the adapter was constructed with."""
        world = _make_world(
            channels=[
                TelegramChannelConfig(identifier="@jobs"),
                TelegramChannelConfig(identifier="@remote"),
            ],
            messages_by_channel={
                "@jobs": [_msg(message_id=1, text="#vacancy A", channel_id="@jobs")],
                "@remote": [_msg(message_id=2, text="#vacancy B", channel_id="@remote")],
            },
        )

        result = asyncio_run(world.adapter.search(SourceQuery()))

        ids = sorted(item["message_id"] for item in result)
        assert ids == [1, 2]

    def test_search_returns_empty_when_no_channels_configured(self) -> None:
        """An adapter with zero channels returns ``[]`` (no error)."""
        world = _make_world(channels=[])
        result = asyncio_run(world.adapter.search(SourceQuery()))
        assert result == []

    def test_search_skips_channel_with_no_messages(self) -> None:
        """A channel with no pending messages contributes nothing to the result."""
        world = _make_world(
            channels=[
                TelegramChannelConfig(identifier="@jobs"),
                TelegramChannelConfig(identifier="@remote"),
            ],
            messages_by_channel={
                "@jobs": [_msg(message_id=1, text="#vacancy A", channel_id="@jobs")],
            },
        )
        result = asyncio_run(world.adapter.search(SourceQuery()))
        assert len(result) == 1


# ---------------------------------------------------------------------------
# normalize() — delegate to the slice's normaliser
# ---------------------------------------------------------------------------


class TestTelegramChannelAdapterNormalize:
    def test_normalize_delegates_to_telegram_channel_normalizer(self) -> None:
        """``normalize`` forwards the raw dict to :class:`TelegramChannelNormalizer`."""
        world = _make_world()
        raw = {
            "channel_id": "@jobs",
            "message_id": 99,
            "text": "#vacancy Senior Python",
            "author": "JobBot",
        }

        vacancy = world.adapter.normalize(raw)

        assert vacancy.source == "telegram_channel"
        assert vacancy.source_id == "@jobs:99"


# ---------------------------------------------------------------------------
# extract_screening_questions() — channels do not carry them
# ---------------------------------------------------------------------------


class TestTelegramChannelAdapterScreening:
    def test_extract_screening_questions_returns_empty(self) -> None:
        """Telegram channels do not carry structured screening questions."""
        world = _make_world()
        raw = {
            "channel_id": "@jobs",
            "message_id": 1,
            "text": "#vacancy Senior Python",
        }

        assert world.adapter.extract_screening_questions(raw) == []


# ---------------------------------------------------------------------------
# apply() — read-only source, NotImplementedError
# ---------------------------------------------------------------------------


class TestTelegramChannelAdapterApply:
    def test_apply_raises_not_implemented(self) -> None:
        """Channels are read-only; ``apply`` raises :class:`NotImplementedError`."""
        world = _make_world()
        job = ApplyJob(
            match_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            vacancy_id=uuid.uuid4(),
        )

        with pytest.raises(NotImplementedError, match="telegram_channel"):
            asyncio_run(world.adapter.apply(job))


# ---------------------------------------------------------------------------
# AdapterRegistry integration
# ---------------------------------------------------------------------------


class TestTelegramChannelAdapterRegistry:
    def test_registered_under_name(self) -> None:
        """The adapter is retrievable from the :class:`AdapterRegistry` by name."""
        registry = AdapterRegistry()
        world = _make_world()

        registry.register(world.adapter)

        assert registry.get("telegram_channel") is world.adapter
        assert "telegram_channel" in registry.list()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def asyncio_run(coro):  # type: ignore[no-untyped-def]
    """Run a coroutine to completion from a sync test."""
    return asyncio.run(coro)
