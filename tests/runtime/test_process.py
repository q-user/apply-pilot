"""Tests for the background process runtime helpers."""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Callable

from job_apply.runtime.process import BaseProcess, run_forever


class FakeRegistrar:
    """Stand-in for ``asyncio.get_running_loop().add_signal_handler``.

    Records every ``(signal, callback)`` pair it receives so tests can assert
    that :class:`BaseProcess` registered the expected signals without
    touching the real loop or :mod:`signal` module.
    """

    def __init__(self) -> None:
        self.signals: dict[int, Callable[[], None]] = {}

    def __call__(self, sig: int, callback: Callable[[], None]) -> None:
        self.signals[sig] = callback


def test_base_process_runs_coroutine() -> None:
    """``BaseProcess`` with a trivial coroutine runs to completion and exits cleanly."""
    called: list[str] = []

    async def coro() -> None:
        called.append("ran")

    async def _drive() -> None:
        async with BaseProcess(coro, signal_registrar=FakeRegistrar()) as proc:
            await proc.run_forever()

    asyncio.run(_drive())
    assert called == ["ran"]


def test_base_process_registers_signal_handlers() -> None:
    """Verifies the handler map is populated via the DI ``signal_registrar``."""
    registrar = FakeRegistrar()

    async def coro() -> None:
        return None

    async def _drive() -> None:
        async with BaseProcess(coro, signal_registrar=registrar) as proc:
            # Both shutdown signals must be registered with the fake registrar.
            assert signal.SIGINT in registrar.signals
            assert signal.SIGTERM in registrar.signals
            # Invoking the registered handler must set the process stop event.
            assert not proc.stop_event.is_set()
            registrar.signals[signal.SIGINT]()
            assert proc.stop_event.is_set()

    asyncio.run(_drive())


def test_run_forever_awaits_shutdown_event() -> None:
    """Shutdown event is set -> coroutine returns within timeout."""
    stop = asyncio.Event()

    async def coro() -> None:
        # Block on the stop event so the coroutine only completes after
        # shutdown is requested.
        await stop.wait()

    async def _drive() -> None:
        task = asyncio.create_task(run_forever(coro, shutdown_event=stop))
        # Yield once so the task can schedule both the coroutine and the watcher.
        await asyncio.sleep(0)
        stop.set()
        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(_drive())
