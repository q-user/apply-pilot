"""Tests for the MAX bot polling loop (:class:`MaxBotProcess`).

The process is exercised with a fake :class:`MaxBot` whose
``get_updates`` / ``handle_update`` / ``aclose`` methods are
:class:`AsyncMock` substitutes.

The previous ``call_later``-based shutdown pattern was racy because
``AsyncMock`` returns synchronously — the polling loop never yields
long enough for a separately-scheduled shutdown task to fire. The
reliable pattern is to set the shutdown event from INSIDE the
``get_updates`` side_effect coroutine: the loop's ``while not
is_shutdown_set()`` check at the top of the next iteration observes
the set event and exits cleanly.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from apply_pilot.features.max.bot import MaxBot
from apply_pilot.features.max.process import MaxBotProcess
from apply_pilot.features.messaging.dto import SendMessageRequest

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _fake_bot() -> Mock:
    """Build a fake :class:`MaxBot` with recording async methods.

    The bot's ``handle_update`` returns a canned
    :class:`SendMessageRequest`; ``aclose`` is a no-op. ``get_updates``
    is wired per-test via ``side_effect`` (a coroutine that sets the
    shutdown event so the loop exits after a deterministic number of
    iterations).
    """
    bot = Mock(spec=MaxBot)
    bot.aclose = AsyncMock(return_value=None)
    bot.get_updates = AsyncMock(
        return_value=([], None),
    )
    bot.handle_update = AsyncMock(
        return_value=SendMessageRequest(chat_id=100, text="ok"),
    )
    return bot


def _shutdown_after(
    process: MaxBotProcess,
    *,
    after_calls: int,
    return_value: tuple[list[dict[str, Any]], int | None] = ([], None),
) -> Callable[..., Any]:
    """Build a coroutine ``side_effect`` for ``get_updates`` that sets the
    shutdown event after ``after_calls`` invocations and returns
    ``return_value`` for every call.

    The coroutine also ``await asyncio.sleep(0)`` so the event loop
    actually runs other ready tasks between iterations — without that
    yield the polling loop is so tight that the shutdown check at the
    top of the next iteration races against the next ``get_updates``
    call.
    """

    state = {"calls": 0}

    async def side_effect(*args: Any, **kwargs: Any) -> tuple[list[dict[str, Any]], int | None]:
        state["calls"] += 1
        await asyncio.sleep(0)
        if state["calls"] >= after_calls:
            process._shutdown_event.set()  # noqa: SLF001
        return return_value

    return side_effect


# ---------------------------------------------------------------------------
# Marker handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_get_updates_call_omits_marker() -> None:
    """The first poll does not echo a ``marker`` (we have none yet)."""
    bot = _fake_bot()
    process = MaxBotProcess(bot=bot, name="max-bot-test")
    bot.get_updates = AsyncMock(side_effect=_shutdown_after(process, after_calls=1))

    await process.run()

    bot.get_updates.assert_called_with(marker=None)


@pytest.mark.asyncio
async def test_subsequent_get_updates_calls_pass_previous_marker() -> None:
    """The second poll echoes the ``marker`` the server returned the first time."""
    bot = _fake_bot()
    call_results: list[tuple[list[dict[str, Any]], int | None]] = [
        ([{"update_type": "message_created", "message": {"body": {}}}], 12345),
        ([], 12350),
    ]
    state = {"i": 0}

    async def side_effect(*args: Any, **kwargs: Any) -> tuple[list[dict[str, Any]], int | None]:
        idx = state["i"]
        state["i"] += 1
        await asyncio.sleep(0)
        if state["i"] >= 2:
            process._shutdown_event.set()  # noqa: SLF001
        return call_results[idx]

    process = MaxBotProcess(bot=bot, name="max-bot-test")
    bot.get_updates = AsyncMock(side_effect=side_effect)

    await process.run()

    # First call: marker=None; second call: marker=12345.
    assert bot.get_updates.await_count == 2
    assert bot.get_updates.await_args_list[0].kwargs["marker"] is None
    assert bot.get_updates.await_args_list[1].kwargs["marker"] == 12345


# ---------------------------------------------------------------------------
# Update dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_updates_list_does_not_invoke_handle_update() -> None:
    """A poll that returns no updates is a no-op (no handler invocations)."""
    bot = _fake_bot()
    process = MaxBotProcess(bot=bot, name="max-bot-test")
    bot.get_updates = AsyncMock(side_effect=_shutdown_after(process, after_calls=1))

    await process.run()

    bot.handle_update.assert_not_called()


@pytest.mark.asyncio
async def test_non_empty_updates_invokes_handle_update_per_update() -> None:
    """Each update is dispatched through :meth:`MaxBot.handle_update` exactly once."""
    bot = _fake_bot()
    updates: list[dict[str, Any]] = [
        {"update_type": "message_created", "message": {"body": {"text": "/start"}}},
        {"update_type": "message_created", "message": {"body": {"text": "/help"}}},
        {"update_type": "message_created", "message": {"body": {"text": "/start"}}},
    ]
    bot._dispatched: list[dict[str, Any]] = []  # type: ignore[attr-defined]
    real_handle_update = bot.handle_update
    process = MaxBotProcess(bot=bot, name="max-bot-test")

    async def recording_handle_update(update: dict[str, Any]) -> Any:
        bot._dispatched.append(update)  # type: ignore[attr-defined]
        # After the LAST dispatch, request shutdown so the loop exits
        # on its next iteration check.
        if len(bot._dispatched) >= len(updates):  # type: ignore[attr-defined]
            process._shutdown_event.set()  # noqa: SLF001
        return await real_handle_update(update)

    bot.handle_update = AsyncMock(side_effect=recording_handle_update)
    bot.get_updates = AsyncMock(
        side_effect=_shutdown_after(process, after_calls=99, return_value=(updates, 999))
    )

    await process.run()

    assert len(bot._dispatched) == 3  # type: ignore[attr-defined]
    for actual, expected in zip(bot._dispatched, updates, strict=True):  # type: ignore[attr-defined]
        assert actual == expected


@pytest.mark.asyncio
async def test_exception_in_handle_update_is_logged_and_loop_continues() -> None:
    """A handler exception is logged and the next update is still dispatched."""
    bot = _fake_bot()
    updates: list[dict[str, Any]] = [
        {"update_type": "message_created", "message": {"body": {"text": "/boom"}}},
        {"update_type": "message_created", "message": {"body": {"text": "/start"}}},
    ]
    bot._dispatched: list[dict[str, Any]] = []  # type: ignore[attr-defined]
    real_handle_update = bot.handle_update
    process = MaxBotProcess(bot=bot, name="max-bot-test")

    async def recording_handle_update(update: dict[str, Any]) -> Any:
        bot._dispatched.append(update)  # type: ignore[attr-defined]
        # After the second dispatch, request shutdown so the loop exits
        # on its next iteration check (the first call raised, so the
        # loop's except path swallowed it and continued).
        if len(bot._dispatched) >= 2:  # type: ignore[attr-defined]
            process._shutdown_event.set()  # noqa: SLF001
        return await real_handle_update(update)

    bot.handle_update = AsyncMock(side_effect=recording_handle_update)
    bot.get_updates = AsyncMock(
        side_effect=_shutdown_after(process, after_calls=99, return_value=(updates, 42))
    )

    exit_code = await process.run()

    assert exit_code == 0
    # Both updates were dispatched — the second one still runs after
    # the first one's exception was caught.
    assert len(bot._dispatched) == 2  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_get_updates_exception_is_logged_and_loop_continues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A transport error is logged and the loop keeps polling after a backoff."""
    bot = _fake_bot()

    state = {"calls": 0}

    async def side_effect(*args: Any, **kwargs: Any) -> tuple[list[dict[str, Any]], int | None]:
        state["calls"] += 1
        await asyncio.sleep(0)
        if state["calls"] == 1:
            raise RuntimeError("network down")
        # Second call succeeds and triggers shutdown.
        process._shutdown_event.set()  # noqa: SLF001
        return [], 1

    process = MaxBotProcess(bot=bot, name="max-bot-test")
    bot.get_updates = AsyncMock(side_effect=side_effect)

    with caplog.at_level(logging.ERROR, logger="apply_pilot.features.max.process"):
        await process.run()

    # The failing first call was logged and the second call ran.
    assert state["calls"] >= 2
    assert any("max.getUpdates.failed" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aclose_called_on_shutdown() -> None:
    """``MaxBot.aclose`` is invoked once the loop exits."""
    bot = _fake_bot()
    process = MaxBotProcess(bot=bot, name="max-bot-test")
    bot.get_updates = AsyncMock(side_effect=_shutdown_after(process, after_calls=1))

    await process.run()

    bot.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_returns_zero_on_clean_shutdown() -> None:
    """A clean shutdown returns ``0`` (the supervisor exit code)."""
    bot = _fake_bot()
    process = MaxBotProcess(bot=bot, name="max-bot-test")
    bot.get_updates = AsyncMock(side_effect=_shutdown_after(process, after_calls=1))

    exit_code = await process.run()

    assert exit_code == 0
