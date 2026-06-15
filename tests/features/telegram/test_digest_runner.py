"""Tests for :class:`DigestRunner` — scheduled daily tick loop.

The runner is exercised with fakes for the sender, the clock and the
asyncio sleep so a single iteration can be observed end-to-end without
waiting wall-clock seconds.
"""

from __future__ import annotations

import asyncio
import signal
import uuid
from datetime import UTC, date, datetime
from typing import Any

import pytest

from job_apply.features.telegram.digest import DigestRunner
from job_apply.runtime.process import BaseProcess

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeSender:
    """Records ``send_to_all_users`` invocations and the ``on_date`` they used."""

    def __init__(self) -> None:
        self.calls: list[date | None] = []

    async def send_to_all_users(self, *, on_date: date | None = None, **_kwargs: Any) -> int:
        self.calls.append(on_date)
        return 0


class _ControllableClock:
    """A clock the runner can advance at will."""

    def __init__(self, start: datetime) -> None:
        self.now_value = start

    def __call__(self) -> datetime:
        return self.now_value

    def advance(self, seconds: float) -> None:
        # Naive but good enough for the test; the runner only reads
        # ``.hour`` and ``.date()``, both of which are stable under
        # linear time advancement.
        self.now_value = datetime.fromtimestamp(self.now_value.timestamp() + seconds, tz=UTC)


class _RecordingSleep:
    """A :func:`asyncio.sleep` substitute that records and yields to the loop.

    The fake actually awaits :func:`asyncio.sleep` for the requested
    delay (clamped to a small ceiling) so the event loop can run
    callbacks scheduled via ``call_later``. Without that yield the
    runner would spin in a tight loop and the test would hang.
    """

    _MAX_DELAY_SECONDS = 0.05

    def __init__(self) -> None:
        self.durations: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.durations.append(delay)
        # Clamp huge production-style delays (e.g. 24h between
        # digests) down to a fraction of a second so the test finishes
        # in a reasonable time.
        await asyncio.sleep(min(delay, self._MAX_DELAY_SECONDS))


# ---------------------------------------------------------------------------
# run_once
# ---------------------------------------------------------------------------


def test_digest_runner_is_base_process_subclass() -> None:
    """``DigestRunner`` must extend ``BaseProcess`` to wire into the runtime."""
    assert issubclass(DigestRunner, BaseProcess)


@pytest.mark.asyncio
async def test_digest_runner_run_once_invokes_sender_with_today() -> None:
    """``run_once`` calls the sender with the clock's date."""
    runner = DigestRunner(
        sender=_FakeSender(),  # type: ignore[arg-type]
        now=_ControllableClock(datetime(2026, 6, 15, 8, 0, tzinfo=UTC)),
        sleep=_RecordingSleep(),  # type: ignore[arg-type]
    )
    sender = runner.sender  # type: ignore[attr-defined]

    sent = await runner.run_once()

    assert sent == 0
    assert sender.calls == [date(2026, 6, 15)]


@pytest.mark.asyncio
async def test_digest_runner_run_once_returns_sender_count() -> None:
    """``run_once`` returns the count of digests dispatched."""

    class _CountingSender(_FakeSender):
        async def send_to_all_users(self, *, on_date: date | None = None, **_kwargs: Any) -> int:
            self.calls.append(on_date)
            return 3

    runner = DigestRunner(
        sender=_CountingSender(),  # type: ignore[arg-type]
        now=_ControllableClock(datetime(2026, 6, 15, 8, 0, tzinfo=UTC)),
        sleep=_RecordingSleep(),  # type: ignore[arg-type]
    )

    assert await runner.run_once() == 3


# ---------------------------------------------------------------------------
# run loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_digest_runner_loop_dispatches_once_when_deadline_is_past() -> None:
    """When the clock is past the digest hour, the loop dispatches immediately."""
    sender = _FakeSender()
    runner = DigestRunner(
        sender=sender,  # type: ignore[arg-type]
        now=_ControllableClock(datetime(2026, 6, 15, 10, 0, tzinfo=UTC)),
        sleep=_RecordingSleep(),  # type: ignore[arg-type]
        digest_hour_utc=9,
    )

    # Schedule a shutdown request one event-loop tick later, then run.
    loop = asyncio.get_running_loop()
    loop.call_later(0.1, runner._shutdown_event.set)  # noqa: SLF001 — test-only wiring

    exit_code = await runner.run()

    assert exit_code == 0
    # Exactly one dispatch happened before shutdown was observed.
    assert len(sender.calls) == 1
    assert sender.calls[0] == date(2026, 6, 15)


@pytest.mark.asyncio
async def test_digest_runner_does_not_dispatch_before_digest_hour() -> None:
    """Before the configured hour, the runner sleeps without dispatching."""
    sender = _FakeSender()
    sleep = _RecordingSleep()
    clock = _ControllableClock(datetime(2026, 6, 15, 7, 0, tzinfo=UTC))
    runner = DigestRunner(
        sender=sender,  # type: ignore[arg-type]
        now=clock,
        sleep=sleep,  # type: ignore[arg-type]
        digest_hour_utc=9,
    )

    loop = asyncio.get_running_loop()
    loop.call_later(0.05, runner._shutdown_event.set)  # noqa: SLF001
    await runner.run()

    # We slept (waiting for the deadline to arrive) but never dispatched.
    assert sender.calls == []
    assert sleep.durations  # at least one sleep


# ---------------------------------------------------------------------------
# signal handlers
# ---------------------------------------------------------------------------


def test_digest_runner_signals_include_sigterm() -> None:
    """The runner must catch SIGTERM (and SIGINT) so the loop exits cleanly."""
    runner = DigestRunner(
        sender=_FakeSender(),  # type: ignore[arg-type]
        now=_ControllableClock(datetime(2026, 6, 15, 8, 0, tzinfo=UTC)),
        sleep=_RecordingSleep(),  # type: ignore[arg-type]
    )
    assert signal.SIGTERM in runner.signals
    assert signal.SIGINT in runner.signals


def test_digest_runner_default_digest_hour_is_nine() -> None:
    """Default ``digest_hour_utc`` is 9 (matches the documented default)."""
    runner = DigestRunner(
        sender=_FakeSender(),  # type: ignore[arg-type]
        now=_ControllableClock(datetime(2026, 6, 15, 8, 0, tzinfo=UTC)),
        sleep=_RecordingSleep(),  # type: ignore[arg-type]
    )
    assert runner.digest_hour_utc == 9


def test_digest_runner_holds_collaborators() -> None:
    """The runner keeps its collaborators on simple attributes (DI-friendly)."""
    sender = _FakeSender()
    clock = _ControllableClock(datetime(2026, 6, 15, 8, 0, tzinfo=UTC))
    sleep = _RecordingSleep()
    runner = DigestRunner(
        sender=sender,  # type: ignore[arg-type]
        now=clock,
        sleep=sleep,  # type: ignore[arg-type]
        digest_hour_utc=10,
    )
    assert runner.sender is sender
    assert runner.now is clock
    assert runner.sleep is sleep
    assert runner.digest_hour_utc == 10
    # Unused import to keep linter quiet on the UUID reference.
    _ = uuid.uuid4()
