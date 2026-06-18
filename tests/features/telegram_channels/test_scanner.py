"""TDD tests for the Telegram-channels scanner (M7, issue #58).

:class:`TelegramChannelScanner` is the long-running process that
periodically polls the configured channels via the adapter's
:meth:`search` and pipes the resulting raw dicts through
:meth:`SourceService.ingest_vacancy_deduped`.

The scanner is a :class:`~apply_pilot.runtime.process.BaseProcess` so it
inherits the SIGINT/SIGTERM handling and the
``await self.wait_for_shutdown()`` primitive. The tests focus on the
slice-specific glue: the loop body, the shutdown interaction, and the
handoff into the source service.
"""

from __future__ import annotations

import asyncio

import pytest

from apply_pilot.features.sources.repository import InMemoryVacancyRepository
from apply_pilot.features.sources.service import SourceService
from apply_pilot.features.telegram_channels import (
    InMemoryTelegramChannelClient,
    TelegramChannelClassifier,
    TelegramChannelConfig,
    TelegramChannelMessage,
    TelegramChannelNormalizer,
    TelegramChannelScanner,
    TelegramChannelSourceAdapter,
)
from apply_pilot.features.telegram_channels import scanner as _scanner_module

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _msg(
    *,
    channel_id: str = "@jobs",
    message_id: int = 1,
    text: str = "#vacancy Senior Python",
) -> TelegramChannelMessage:
    return TelegramChannelMessage(channel_id=channel_id, message_id=message_id, text=text)


def _make_scanner(
    *,
    channels: list[TelegramChannelConfig] | None = None,
    messages_by_channel: dict[str, list[TelegramChannelMessage]] | None = None,
    poll_interval_seconds: float = 0.01,
):
    """Build a scanner wired to in-memory fakes."""
    channels = channels or [TelegramChannelConfig(identifier="@jobs")]
    client = InMemoryTelegramChannelClient(
        channels=channels, messages_by_channel=messages_by_channel or {}
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
    scanner = TelegramChannelScanner(
        adapter=adapter,
        source_service=service,
        poll_interval_seconds=poll_interval_seconds,
    )
    return scanner, client, service, repo


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestScannerConstruction:
    def test_invalid_poll_interval_raises(self) -> None:
        """A non-positive poll interval is rejected up-front."""
        channels = [TelegramChannelConfig(identifier="@jobs")]
        client = InMemoryTelegramChannelClient(channels=channels)
        adapter = TelegramChannelSourceAdapter(
            client=client,
            classifier=TelegramChannelClassifier(),
            normalizer=TelegramChannelNormalizer(),
            channels=channels,
        )
        service = SourceService(InMemoryVacancyRepository())

        with pytest.raises(ValueError, match="poll_interval_seconds"):
            TelegramChannelScanner(
                adapter=adapter,
                source_service=service,
                poll_interval_seconds=0,
            )

    def test_name_defaults_to_telegram_channel_scanner(self) -> None:
        """The default ``name`` flows through to the :class:`BaseProcess` log lines."""
        scanner, *_ = _make_scanner()
        assert scanner.name == "telegram-channel-scanner"


# ---------------------------------------------------------------------------
# Polling loop
# ---------------------------------------------------------------------------


class TestScannerRun:
    def test_run_ingests_seeded_vacancies(self) -> None:
        """A single ``run`` tick persists every seeded vacancy message."""
        scanner, _client, _service, repo = _make_scanner(
            messages_by_channel={
                "@jobs": [
                    _msg(message_id=1, text="#vacancy Senior Python"),
                    _msg(message_id=2, text="#vacancy Go Developer"),
                ],
            },
        )

        async def drive() -> None:
            task = asyncio.create_task(scanner.run())
            # Give the loop one tick; then ask it to stop.
            await asyncio.sleep(0.05)
            scanner.stop()
            await asyncio.wait_for(task, timeout=2.0)

        asyncio_run(drive())

        assert len(repo.list_by_source("telegram_channel")) == 2

    def test_run_ignores_non_vacancy_messages(self) -> None:
        """Posts that the classifier rejects are not persisted."""
        scanner, _client, _service, repo = _make_scanner(
            messages_by_channel={
                "@jobs": [
                    _msg(message_id=1, text="#vacancy Senior Python"),
                    _msg(message_id=2, text="Casual chat, no vacancy here"),
                ],
            },
        )

        async def drive() -> None:
            task = asyncio.create_task(scanner.run())
            await asyncio.sleep(0.05)
            scanner.stop()
            await asyncio.wait_for(task, timeout=2.0)

        asyncio_run(drive())

        rows = repo.list_by_source("telegram_channel")
        assert len(rows) == 1
        assert rows[0].source_id == "@jobs:1"

    def test_run_drains_messages_after_ingest(self) -> None:
        """A second tick sees an empty channel — no double-ingest."""
        scanner, _client, _service, repo = _make_scanner(
            messages_by_channel={
                "@jobs": [_msg(message_id=1, text="#vacancy Senior Python")],
            },
        )

        async def drive() -> None:
            task = asyncio.create_task(scanner.run())
            await asyncio.sleep(0.1)
            scanner.stop()
            await asyncio.wait_for(task, timeout=2.0)

        asyncio_run(drive())

        # Only one row should land; the drain-on-fetch semantics of
        # the in-memory client prevent the next tick from re-ingesting
        # the same message.
        assert len(repo.list_by_source("telegram_channel")) == 1

    def test_run_dedupes_same_message_across_ticks(self) -> None:
        """A re-added message with the same natural key is deduped.

        The in-memory client is ``keep_buffer=False`` by default, so
        the same message is only re-emitted when explicitly re-added.
        The dedup check in ``SourceService.ingest_vacancy_deduped``
        still has to fire, and the test asserts that the count stays
        at one row even when the message is queued twice.
        """
        scanner, client, _service, repo = _make_scanner()

        async def drive() -> None:
            client.add_message(_msg(message_id=1, text="#vacancy Senior Python"))
            task = asyncio.create_task(scanner.run())
            await asyncio.sleep(0.05)
            # Re-add the same message — the dedup detector must skip it.
            client.add_message(_msg(message_id=1, text="#vacancy Senior Python"))
            await asyncio.sleep(0.05)
            scanner.stop()
            await asyncio.wait_for(task, timeout=2.0)

        asyncio_run(drive())

        assert len(repo.list_by_source("telegram_channel")) == 1

    def test_run_handles_no_channels_gracefully(self) -> None:
        """A scanner with no channels shuts down cleanly."""
        scanner, _client, _service, repo = _make_scanner(channels=[])

        async def drive() -> None:
            task = asyncio.create_task(scanner.run())
            await asyncio.sleep(0.05)
            scanner.stop()
            await asyncio.wait_for(task, timeout=2.0)

        asyncio_run(drive())
        assert repo.list_by_source("telegram_channel") == []

    def test_repeated_tick_failures_do_not_leak_tasks(self) -> None:
        """Regression for #147.

        Each transient ``_tick`` failure used to schedule
        ``asyncio.create_task(self._shutdown_event.wait())`` inside
        the error-recovery branch but never cancel that task when the
        ``asyncio.wait_for`` backoff timed out. The orphaned tasks
        accumulated on the running loop for the lifetime of the
        scanner. After the fix, repeated failures must not grow the
        task count.

        The leak only shows up while the scanner is still running —
        tearing the loop down (via ``scanner.stop()``) flushes the
        leaked tasks. So the test samples ``asyncio.all_tasks()``
        during the run, not after.
        """
        scanner, _client, _service, _repo = _make_scanner(poll_interval_seconds=0.01)

        # Shrink the inter-error backoff so the test runs fast. The
        # module-level constant is the value ``run`` reads at call
        # time, so a quick monkey-patch is enough.
        original_backoff = _scanner_module._ERROR_BACKOFF_SECONDS
        _scanner_module._ERROR_BACKOFF_SECONDS = 0.02
        try:
            # Replace ``_tick`` with a coroutine that always raises.
            # The error-recovery branch swallows the exception and
            # only logs; the scanner keeps spinning.
            tick_calls = 0

            async def always_fail() -> None:
                nonlocal tick_calls
                tick_calls += 1
                raise RuntimeError("boom")

            scanner._tick = always_fail  # type: ignore[method-assign]

            async def drive() -> None:
                task = asyncio.create_task(scanner.run())
                try:
                    # Wait until we've had several error-recovery
                    # cycles and then sample the live task list.
                    while tick_calls < 5:
                        await asyncio.sleep(0.05)
                    # Let the current cycle's backoff window settle
                    # so the leak (if any) is fully visible.
                    await asyncio.sleep(0.1)
                    live = len(asyncio.all_tasks())
                finally:
                    scanner.stop()
                    await asyncio.wait_for(task, timeout=5.0)

                # While the scanner is alive, the loop should contain
                # only a small, bounded set of tasks: the test
                # coroutine itself, the ``scanner.run()`` task, and
                # at most one ``asyncio.wait_for`` wrapper. On the
                # buggy code each error cycle leaks an extra
                # ``Event.wait()`` task, so the count grows linearly
                # with the number of cycles (>= 5 by now).
                assert live <= 3, (
                    f"asyncio.all_tasks() reported {live} live tasks after "
                    f"{tick_calls} error cycles — scanner is leaking tasks (#147)"
                )

            asyncio_run(drive())
        finally:
            _scanner_module._ERROR_BACKOFF_SECONDS = original_backoff


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def asyncio_run(coro):  # type: ignore[no-untyped-def]
    """Run a coroutine to completion from a sync test."""
    return asyncio.run(coro)
