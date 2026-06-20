"""Long-running scheduler for the MAX daily digest.

:class:`MaxDigestRunner` extends :class:`~apply_pilot.runtime.process.BaseProcess`
so it picks up the standard SIGINT/SIGTERM graceful-shutdown wiring.
The loop computes the next ``digest_hour_utc`` deadline, sleeps until
then (or until shutdown), and then dispatches the digest.

The ``now`` and ``sleep`` callables are injected so tests can drive a
fake clock and a recording sleep without touching wall-clock time.

Mirrors :class:`apply_pilot.features.telegram.digest.runner.DigestRunner`
but tags the process name ``max-digest-runner`` so the runner can be
distinguished from the Telegram-side runner in supervisor logs.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

from apply_pilot.runtime.process import BaseProcess

_LOGGER = logging.getLogger("apply_pilot.features.max.digest.runner")


class _SenderLike(Protocol):
    async def send_to_all_users(
        self,
        users: list[Any] | None = None,
        *,
        on_date: date | None = None,
    ) -> int: ...


class MaxDigestRunner(BaseProcess):
    """A :class:`BaseProcess` that fires the digest once a day at *digest_hour_utc*.

    Defaults: ``name="max-digest-runner"``, ``digest_hour_utc=9``. The
    hour is interpreted in UTC; per-user timezones will be a follow-up
    slice. ``sleep`` defaults to :func:`asyncio.sleep`; tests pass a
    recorder.
    """

    DEFAULT_DIGEST_HOUR_UTC = 9

    def __init__(
        self,
        sender: _SenderLike,
        *,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Any] | None = None,
        digest_hour_utc: int = DEFAULT_DIGEST_HOUR_UTC,
    ) -> None:
        super().__init__(name="max-digest-runner", signals=(signal.SIGINT, signal.SIGTERM))
        self._sender = sender
        self._now: Callable[[], datetime] = now or self._default_now
        self._sleep: Callable[[float], Any] = sleep or asyncio.sleep
        self._digest_hour_utc = digest_hour_utc

    # ------------------------------------------------------------------
    # Public attributes
    # ------------------------------------------------------------------
    @property
    def sender(self) -> _SenderLike:
        return self._sender

    @property
    def now(self) -> Callable[[], datetime]:
        return self._now

    @property
    def sleep(self) -> Callable[[float], Any]:
        return self._sleep

    @property
    def digest_hour_utc(self) -> int:
        return self._digest_hour_utc

    # ------------------------------------------------------------------
    # Loop
    # ------------------------------------------------------------------

    async def run(self) -> int:
        """Run the daily tick loop until shutdown is requested."""
        self.start()
        try:
            while not self.is_shutdown_set():
                wait_seconds = self._seconds_until_next_deadline()
                if wait_seconds > 0:
                    try:
                        await self._sleep(wait_seconds)
                    except asyncio.CancelledError:
                        raise
                if self.is_shutdown_set():
                    break
                await self.run_once()
            return 0
        finally:
            self.stop()

    async def run_once(self, *, on_date: date | None = None) -> int:
        """Dispatch a single digest iteration; return the count sent.

        Exposed for tests and the manual ``POST /digest/max/send``
        endpoint so callers can trigger an iteration without spinning
        up the scheduler.
        """
        target_date = on_date or self._now().date()
        try:
            sent = await self._sender.send_to_all_users(on_date=target_date)
        except Exception:
            _LOGGER.exception(
                "max.digest.run_once.failed",
                extra={
                    "event": "max.digest.run_once.failed",
                    "on_date": target_date.isoformat(),
                },
            )
            return 0
        _LOGGER.info(
            "max.digest.run_once.sent",
            extra={
                "event": "max.digest.run_once.sent",
                "on_date": target_date.isoformat(),
                "sent": sent,
            },
        )
        return sent

    # ------------------------------------------------------------------
    # Deadline computation
    # ------------------------------------------------------------------

    def _seconds_until_next_deadline(self) -> float:
        """Return the number of seconds until the next ``digest_hour_utc`` boundary.

        A non-positive value means "the deadline has already passed
        this cycle; dispatch immediately". The hour is interpreted in
        UTC; the loop uses :meth:`BaseProcess.is_shutdown_set` to bail
        out cleanly if SIGTERM arrives during the sleep.
        """
        now = self._now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        deadline = now.replace(hour=self._digest_hour_utc, minute=0, second=0, microsecond=0)
        if deadline <= now:
            deadline += timedelta(days=1)
        return (deadline - now).total_seconds()

    @staticmethod
    def _default_now() -> datetime:
        return datetime.now(UTC)


__all__ = ["MaxDigestRunner"]
