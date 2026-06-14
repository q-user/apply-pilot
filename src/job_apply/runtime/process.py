"""Long-running process skeleton with graceful signal-based shutdown.

Background workers (the scheduler, the notification bot, the scanner, ...)
all need the same boring boilerplate: log on start, install SIGINT/SIGTERM
handlers, wait for a shutdown event, log on shutdown, exit 0. This module
captures that boilerplate in :class:`BaseProcess` so individual workers
just compose the actual work loop on top of it.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Callable, Iterable

SignalHandler = Callable[[int, object | None], object]

_LOG_PREFIX = "job_apply.runtime."


class BaseProcess:
    """A long-running process with graceful SIGINT/SIGTERM shutdown.

    Signal handling is delegated to the running asyncio loop via
    :meth:`asyncio.AbstractEventLoop.add_signal_handler`, so the OS-level
    signal handler is only replaced while the loop is alive and is
    restored automatically when the loop is closed. ``start()`` must
    therefore be called from within a running event loop.

    Typical usage::

        class MyWorker(BaseProcess):
            async def run(self) -> int:
                self.start()
                try:
                    while not self.is_shutdown_set():
                        await self.do_step()
                    return 0
                finally:
                    self.stop()
    """

    def __init__(
        self,
        name: str = "process",
        *,
        signals: Iterable[int] = (signal.SIGINT, signal.SIGTERM),
    ) -> None:
        self.name = name
        self.signals: tuple[int, ...] = tuple(signals)
        self._shutdown_event = asyncio.Event()
        self._installed_handlers: dict[int, asyncio.Handle | None] = {}
        self._started = False
        self._logger = logging.getLogger(f"{_LOG_PREFIX}{name}")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Install signal handlers and emit a structured start log.

        Idempotent: calling ``start()`` a second time is a no-op so
        subclasses can safely call it from their own ``run()`` override.

        Must be called from within a running asyncio event loop because
        :meth:`asyncio.AbstractEventLoop.add_signal_handler` requires it.
        """
        if self._started:
            return
        self._started = True
        loop = asyncio.get_running_loop()
        for sig in self.signals:
            self._installed_handlers[sig] = self._install_one(loop, sig)
        self._logger.info(
            "process.start",
            extra={"event": "process.start", "proc_name": self.name},
        )

    def _install_one(self, loop: asyncio.AbstractEventLoop, sig: int) -> asyncio.Handle | None:
        try:
            return loop.add_signal_handler(sig, self._shutdown_event.set)
        except NotImplementedError:
            # Selector loops on Windows don't support signal handlers.
            self._logger.warning(
                "process.signal.unsupported",
                extra={
                    "event": "process.signal.unsupported",
                    "proc_name": self.name,
                    "signum": sig,
                },
            )
            return None

    def stop(self) -> None:
        """Set the shutdown event and cancel installed signal handlers.

        Safe to call multiple times and safe to call without a prior
        ``start()`` (the handler dict will simply be empty).
        """
        self._shutdown_event.set()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        for sig, handle in self._installed_handlers.items():
            if handle is None or loop is None:
                continue
            try:
                handle.cancel()
            except (ValueError, RuntimeError):
                self._logger.debug(
                    "process.stop.cancel_failed",
                    extra={
                        "event": "process.stop.cancel_failed",
                        "proc_name": self.name,
                        "signum": sig,
                    },
                )
        self._installed_handlers.clear()

    def is_shutdown_set(self) -> bool:
        """Return whether the shutdown event has been set."""
        return self._shutdown_event.is_set()

    # ------------------------------------------------------------------
    # Async coordination
    # ------------------------------------------------------------------
    async def wait_for_shutdown(self) -> None:
        """Block until the shutdown event is set; log a structured line.

        Emits ``process.shutdown`` *after* the event is observed so the
        log line is a reliable signal that the wait completed.
        """
        await self._shutdown_event.wait()
        self._logger.info(
            "process.shutdown",
            extra={"event": "process.shutdown", "proc_name": self.name},
        )

    async def run(self) -> int:
        """Default runner: start, wait for shutdown, restore, exit 0.

        Returns 0 on a graceful shutdown. Subclasses override this to
        run their own work loop while still benefiting from the signal
        wiring by calling ``self.start()`` / ``self.stop()`` around it.
        """
        self.start()
        try:
            await self.wait_for_shutdown()
            return 0
        finally:
            self.stop()


__all__ = ["BaseProcess"]
