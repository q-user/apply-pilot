"""Telegram-channels scanner (M7, issue #58).

:class:`TelegramChannelScanner` is the slice's long-running process.
It subclasses :class:`~job_apply.runtime.process.BaseProcess` so it
inherits SIGINT / SIGTERM handling and the
``await self.wait_for_shutdown()`` primitive. The loop body is
deliberately small:

1. Call the adapter's :meth:`~TelegramChannelSourceAdapter.search`
   to fetch the raw message dicts.
2. Push each raw through
   :meth:`~job_apply.features.sources.service.SourceService.ingest_vacancy_deduped`
   so the dedup detector catches re-posts.
3. Sleep for :attr:`poll_interval_seconds`, but bail out as soon as
   the shutdown event is set so SIGTERM is honoured mid-sleep.

Why a :class:`BaseProcess`
--------------------------

The slice is meant to run as a long-lived daemon (``apply-pilot-tg-scanner``
or similar), and the rest of the project's background workers
(:class:`~job_apply.features.telegram.process.TelegramBotProcess`,
:class:`~job_apply.features.apply_worker.runtime.ApplyWorkerProcess`)
already follow the :class:`BaseProcess` pattern. Reusing the base
class gives the scanner the same OS-signal handling and structured
log lines for free.

Why the scanner does **not** import the env directly
----------------------------------------------------

The scanner's ``__init__`` takes an :class:`TelegramChannelSourceAdapter`
and a :class:`SourceService` — the env-driven settings live in
:func:`job_apply.features.telegram_channels.config.get_telegram_channels_settings`,
which a follow-up ``apply-pilot-tg-scanner`` entry point will read
once at start-up and pass to the scanner. Keeping the scanner env-free
makes the test surface simpler: tests build the adapter / service
by hand and never touch ``os.environ``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from typing import Any

from job_apply.features.sources.service import SourceService
from job_apply.features.telegram_channels.adapter import TelegramChannelSourceAdapter
from job_apply.runtime.process import BaseProcess

_LOG_PREFIX = "job_apply.features.telegram_channels.scanner."

#: Wait a couple of seconds after a transient error before the next
#: tick. Anything longer would noticeably delay recovery from a
#: transient blip in the transport; anything shorter would burn CPU
#: in a tight loop while Telegram is down.
_ERROR_BACKOFF_SECONDS: float = 2.0


class TelegramChannelScanner(BaseProcess):
    """A :class:`BaseProcess` that polls Telegram channels for vacancies.

    The scanner is collaborator-injected: tests build it with the
    in-memory client + in-memory repository the rest of the slice
    uses; production wiring (the future ``apply-pilot-tg-scanner``
    entry point) plugs in a real transport + the SQLAlchemy-backed
    :class:`SourceService`.

    Args:
        adapter: The :class:`TelegramChannelSourceAdapter` that owns
            the transport, classifier, and normaliser.
        source_service: The :class:`SourceService` to push normalised
            vacancies through.
        poll_interval_seconds: Sleep between polls. Must be positive.
        name: Process name used in structured log lines.
    """

    def __init__(
        self,
        *,
        adapter: TelegramChannelSourceAdapter,
        source_service: SourceService,
        poll_interval_seconds: float,
        name: str = "telegram-channel-scanner",
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError(
                f"poll_interval_seconds must be positive, got {poll_interval_seconds!r}"
            )
        super().__init__(name=name, signals=(signal.SIGINT, signal.SIGTERM))
        self._adapter = adapter
        self._source_service = source_service
        self._poll_interval_seconds = poll_interval_seconds
        self._logger = logging.getLogger(_LOG_PREFIX + "TelegramChannelScanner")

    # ------------------------------------------------------------------
    # Read-only collaborators
    # ------------------------------------------------------------------

    @property
    def adapter(self) -> TelegramChannelSourceAdapter:
        """Return the injected adapter (read-only)."""
        return self._adapter

    @property
    def source_service(self) -> SourceService:
        """Return the injected source service (read-only)."""
        return self._source_service

    @property
    def poll_interval_seconds(self) -> float:
        """Return the configured poll interval (seconds)."""
        return self._poll_interval_seconds

    # ------------------------------------------------------------------
    # Loop
    # ------------------------------------------------------------------

    async def run(self) -> int:
        """Poll until shutdown is requested.

        Returns 0 on a graceful shutdown. A single iteration
        exception is logged and the loop continues — a transient
        transport blip must not crash the worker.
        """
        self.start()
        try:
            while not self.is_shutdown_set():
                try:
                    await self._tick()
                except asyncio.CancelledError:
                    # Runtime-driven cancellation; let it propagate
                    # so the surrounding ``asyncio.run`` exits cleanly.
                    raise
                except Exception:  # noqa: BLE001 — never crash the scanner
                    self._logger.exception(
                        "telegram_channels.scanner.tick_failed",
                        extra={"event": "telegram_channels.scanner.tick_failed"},
                    )
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(
                            self._wait_for_shutdown_no_log(),
                            timeout=_ERROR_BACKOFF_SECONDS,
                        )
                    continue

                if self.is_shutdown_set():
                    break
                # Sleep with the shutdown event as a cancellation
                # point. ``wait_for`` returns when the event is set;
                # on a clean timeout we go around the loop again.
                try:
                    await asyncio.wait_for(
                        self.wait_for_shutdown(),
                        timeout=self._poll_interval_seconds,
                    )
                    break  # Event was set during the wait.
                except TimeoutError:
                    pass
            return 0
        finally:
            self.stop()

    async def _tick(self) -> None:
        """Run one poll + ingest pass.

        Pulls the raw message dicts from the adapter, normalises
        each via the adapter (so the slice's
        :class:`TelegramChannelNormalizer` owns the source-specific
        mapping), and pipes the resulting :class:`Vacancy` list
        through :meth:`SourceService.ingest_batch`. The batch path
        is preferred over the per-message
        :meth:`~SourceService.ingest_vacancy_deduped` because it
        applies batch-level dedup (a single message in two
        channels of the same run, for example) and emits a single
        log line per tick.

        Exceptions raised by ``search`` or ``ingest_batch``
        propagate to the outer ``run`` for logging and backoff.
        """
        # The adapter ignores ``SourceQuery`` (channels are a feed,
        # not a search API) but the Protocol mandates the argument.
        from job_apply.features.sources.adapter import SourceQuery
        from job_apply.features.sources.models import Vacancy

        raws: list[dict[str, Any]] = await self._adapter.search(SourceQuery())
        if not raws:
            return

        vacancies: list[Vacancy] = []
        for raw in raws:
            try:
                vacancy = self._adapter.normalize(raw)
            except ValueError as exc:
                # A malformed raw (missing channel_id / message_id)
                # is logged and skipped so a single bad message
                # cannot crash the scanner.
                self._logger.warning(
                    "telegram_channels.scanner.normalize_failed",
                    extra={
                        "event": "telegram_channels.scanner.normalize_failed",
                        "error": str(exc),
                    },
                )
                continue
            vacancies.append(vacancy)

        if not vacancies:
            return
        new, duplicates = await self._source_service.ingest_batch(vacancies)
        for vacancy in new:
            self._logger.info(
                "telegram_channels.scanner.ingested",
                extra={
                    "event": "telegram_channels.scanner.ingested",
                    "vacancy_id": str(vacancy.id),
                    "source_id": vacancy.source_id,
                },
            )
        if duplicates:
            self._logger.info(
                "telegram_channels.scanner.duplicates",
                extra={
                    "event": "telegram_channels.scanner.duplicates",
                    "count": len(duplicates),
                },
            )

    async def _wait_for_shutdown_no_log(self) -> None:
        """Wait for the shutdown event without emitting a log line.

        ``BaseProcess.wait_for_shutdown`` logs a ``process.shutdown``
        event unconditionally; using it from an error-recovery
        branch would emit misleading noise while the worker is still
        alive. This wrapper delegates to the underlying event
        without the side effect.
        """
        await asyncio.wait(
            {asyncio.create_task(self._shutdown_event.wait())},
            return_when=asyncio.FIRST_COMPLETED,
        )


__all__ = ["TelegramChannelScanner"]
