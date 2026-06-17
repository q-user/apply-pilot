"""End-to-end tests for the Telegram-channels slice (M7, issue #58).

These tests exercise the full vertical — client → classifier →
adapter → source service → repository — so a regression in any of the
collaborators surfaces here. The slice's public surface is the
:class:`TelegramChannelSourceAdapter` registered in an
:class:`AdapterRegistry`; the integration tests treat it as such.
"""

from __future__ import annotations

import asyncio

from job_apply.features.sources.adapter import AdapterRegistry, SourceQuery
from job_apply.features.sources.repository import InMemoryVacancyRepository
from job_apply.features.sources.service import SourceService
from job_apply.features.telegram_channels import (
    InMemoryTelegramChannelClient,
    TelegramChannelClassifier,
    TelegramChannelConfig,
    TelegramChannelMessage,
    TelegramChannelNormalizer,
    TelegramChannelSourceAdapter,
)


def _msg(
    *,
    channel_id: str = "@jobs",
    message_id: int = 1,
    text: str = "#vacancy Senior Python Developer\nAcme Corp\nSalary: 250-350k",
) -> TelegramChannelMessage:
    return TelegramChannelMessage(channel_id=channel_id, message_id=message_id, text=text)


def asyncio_run(coro):  # type: ignore[no-untyped-def]
    """Run a coroutine to completion from a sync test."""
    return asyncio.run(coro)


def _build_e2e_stack(messages_by_channel: dict[str, list[TelegramChannelMessage]]):
    """Wire the slice end-to-end with in-memory fakes."""
    channels = [TelegramChannelConfig(identifier=cid) for cid in messages_by_channel]
    client = InMemoryTelegramChannelClient(
        channels=channels, messages_by_channel=messages_by_channel
    )
    classifier = TelegramChannelClassifier()
    normalizer = TelegramChannelNormalizer()
    adapter = TelegramChannelSourceAdapter(
        client=client,
        classifier=classifier,
        normalizer=normalizer,
        channels=channels,
    )
    repo = InMemoryVacancyRepository()
    service = SourceService(repo)
    registry = AdapterRegistry()
    registry.register(adapter)
    return client, adapter, service, repo, registry


class TestEndToEndFlow:
    def test_search_then_ingest_persists_vacancy(self) -> None:
        """``search`` returns raws → ``adapter.normalize`` + ``ingest_batch`` persists them."""
        client, adapter, service, repo, _registry = _build_e2e_stack(
            {"@jobs": [_msg(message_id=1, text="#vacancy Senior Python")]}
        )

        raws = asyncio_run(adapter.search(SourceQuery()))

        # Normalise via the slice's adapter (mirrors what the scanner does).
        vacancies = [adapter.normalize(raw) for raw in raws]
        new, _duplicates = asyncio_run(service.ingest_batch(vacancies))

        assert len(new) == 1
        rows = repo.list_by_source("telegram_channel")
        assert len(rows) == 1
        assert rows[0].source == "telegram_channel"
        assert rows[0].source_id == "@jobs:1"
        assert "Senior Python" in rows[0].title

    def test_registry_lookup_round_trip(self) -> None:
        """Looking the adapter up by name in the registry returns the same instance."""
        _client, adapter, _service, _repo, registry = _build_e2e_stack({})

        looked_up = registry.get("telegram_channel")

        assert looked_up is adapter

    def test_multi_channel_ingest(self) -> None:
        """A search across multiple channels yields one row per message."""
        _client, adapter, service, repo, _registry = _build_e2e_stack(
            {
                "@jobs": [_msg(channel_id="@jobs", message_id=1, text="#vacancy A")],
                "@remote": [_msg(channel_id="@remote", message_id=2, text="#vacancy B")],
            }
        )

        raws = asyncio_run(adapter.search(SourceQuery()))
        vacancies = [adapter.normalize(raw) for raw in raws]
        asyncio_run(service.ingest_batch(vacancies))

        rows = repo.list_by_source("telegram_channel")
        assert len(rows) == 2
        ids = {r.source_id for r in rows}
        assert ids == {"@jobs:1", "@remote:2"}

    def test_dedup_skips_repeat_messages(self) -> None:
        """``ingest_batch`` dedupes re-posts of the same message."""
        _client, adapter, service, repo, _registry = _build_e2e_stack(
            {"@jobs": [_msg(message_id=1, text="#vacancy Senior Python")]}
        )

        raws = asyncio_run(adapter.search(SourceQuery()))
        vacancies = [adapter.normalize(raw) for raw in raws]

        for _ in range(2):
            asyncio_run(service.ingest_batch(vacancies))

        # Only one row — the second pass detects the (source, source_id) collision.
        assert len(repo.list_by_source("telegram_channel")) == 1
