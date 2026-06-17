"""Tests for the BaseProcess runtime helper.

These tests do NOT deliver real OS signals. They patch
``asyncio.get_running_loop`` so :class:`BaseProcess` registers its signal
callbacks against a mock loop, capture those callbacks, and invoke them
directly (or via the real loop's ``call_soon``) to assert the shutdown
event is set.
"""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Callable
from unittest.mock import MagicMock

import pytest

from apply_pilot.runtime.process import BaseProcess


def _build_mock_loop(
    captured: dict[int, Callable[[], None]],
    handles: dict[int, MagicMock],
) -> MagicMock:
    """Return a mock loop whose ``add_signal_handler`` records callbacks
    *and* the handles it returns."""

    def add_signal_handler(sig: int, callback: Callable[[], None]) -> MagicMock:
        captured[sig] = callback
        handle = MagicMock(name=f"handle-{sig}")
        handles[sig] = handle
        return handle

    mock_loop = MagicMock(name="loop")
    mock_loop.add_signal_handler.side_effect = add_signal_handler
    return mock_loop


def test_base_process_registers_signal_handlers(monkeypatch: pytest.MonkeyPatch) -> None:
    """``start()`` must register callbacks for SIGINT and SIGTERM."""
    process = BaseProcess(name="test-worker")
    captured: dict[int, Callable[[], None]] = {}
    handles: dict[int, MagicMock] = {}
    mock_loop = _build_mock_loop(captured, handles)

    async def scenario() -> None:
        monkeypatch.setattr(asyncio, "get_running_loop", lambda: mock_loop)
        process.start()
        try:
            assert mock_loop.add_signal_handler.call_count == 2
            registered_sigs = {call.args[0] for call in mock_loop.add_signal_handler.call_args_list}
            assert registered_sigs == {signal.SIGINT, signal.SIGTERM}
            for sig in (signal.SIGINT, signal.SIGTERM):
                assert callable(captured[sig])
        finally:
            process.stop()

    asyncio.run(scenario())


def test_base_process_shutdown_flag_set_on_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invoking the registered callback must set the shutdown event."""
    process = BaseProcess(name="test-worker")
    captured: dict[int, Callable[[], None]] = {}
    handles: dict[int, MagicMock] = {}
    mock_loop = _build_mock_loop(captured, handles)

    async def scenario() -> None:
        monkeypatch.setattr(asyncio, "get_running_loop", lambda: mock_loop)
        process.start()
        try:
            assert process.is_shutdown_set() is False
            captured[signal.SIGINT]()
            assert process.is_shutdown_set() is True
        finally:
            process.stop()

    asyncio.run(scenario())


def test_base_process_wait_for_shutdown_returns_after_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``wait_for_shutdown`` must complete once a signal callback fires."""
    process = BaseProcess(name="test-worker")
    captured: dict[int, Callable[[], None]] = {}
    handles: dict[int, MagicMock] = {}

    async def scenario() -> None:
        # Save the real loop *before* the patch so we can schedule the
        # trigger on the loop that actually runs this coroutine.
        real_loop = asyncio.get_running_loop()
        mock_loop = _build_mock_loop(captured, handles)
        monkeypatch.setattr(asyncio, "get_running_loop", lambda: mock_loop)
        process.start()
        try:
            real_loop.call_soon(captured[signal.SIGTERM])
            await asyncio.wait_for(process.wait_for_shutdown(), timeout=1.0)
            assert process.is_shutdown_set() is True
        finally:
            process.stop()

    asyncio.run(scenario())


def test_base_process_stop_sets_event_without_signal() -> None:
    """``stop()`` is a programmatic shutdown path independent of signals."""
    process = BaseProcess(name="test-worker")

    assert process.is_shutdown_set() is False
    process.stop()
    assert process.is_shutdown_set() is True


def test_base_process_restores_handlers_on_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    """``stop()`` must cancel the handles returned by ``add_signal_handler``."""
    process = BaseProcess(name="test-worker")
    captured: dict[int, Callable[[], None]] = {}
    handles: dict[int, MagicMock] = {}
    mock_loop = _build_mock_loop(captured, handles)

    async def scenario() -> None:
        monkeypatch.setattr(asyncio, "get_running_loop", lambda: mock_loop)
        process.start()
        process.stop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            assert handles[sig].cancel.call_count == 1, f"handle for signal {sig} not cancelled"

    asyncio.run(scenario())


def test_base_process_run_returns_zero_on_graceful_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run()`` must exit 0 once a signal callback fires."""
    process = BaseProcess(name="test-worker")
    captured: dict[int, Callable[[], None]] = {}
    handles: dict[int, MagicMock] = {}

    async def scenario() -> int:
        mock_loop = _build_mock_loop(captured, handles)
        monkeypatch.setattr(asyncio, "get_running_loop", lambda: mock_loop)

        task = asyncio.create_task(process.run())
        # Give ``start()`` a chance to register handlers.
        await asyncio.sleep(0)
        captured[signal.SIGTERM]()
        return await asyncio.wait_for(task, timeout=1.0)

    assert asyncio.run(scenario()) == 0
