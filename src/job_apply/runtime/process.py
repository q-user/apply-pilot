"""Background process lifecycle skeleton and run-forever helper."""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Callable, Coroutine, Mapping
from typing import Any

#: Signature of a coroutine factory consumed by :class:`BaseProcess` and
#: :func:`run_forever`. The factory must return a coroutine (the result of
#: calling an ``async def`` function), which is the only object that can be
#: passed to :func:`asyncio.create_task`.
CoroFactory = Callable[[], Coroutine[Any, Any, None]]

#: Signature of a callable that registers a signal handler. ``(sig, callback)``
#: is the same shape as :meth:`asyncio.AbstractEventLoop.add_signal_handler`
#: and is the DI seam used to test signal registration without touching the
#: real loop or :mod:`signal` module.
SignalRegistrar = Callable[[int, Callable[[], None]], None]

#: Read-only view of the signal -> handler map populated on context entry.
HandlerMap = Mapping[int, Callable[[], None]]


def _default_signal_registrar(loop: asyncio.AbstractEventLoop) -> SignalRegistrar:
    """Default registrar: install handlers via the running asyncio loop.

    Falls back to :func:`signal.signal` on platforms where the asyncio API is
    not available (e.g. Windows or non-main threads).
    """

    def _register(sig: int, callback: Callable[[], None]) -> None:
        try:
            loop.add_signal_handler(sig, callback)
        except NotImplementedError:  # pragma: no cover - Windows-specific
            signal.signal(sig, lambda *_: callback())

    return _register


class BaseProcess:
    """Async context manager that runs a coroutine with signal-driven shutdown.

    On context entry the process registers a handler for every signal in
    ``shutdown_signals`` (defaults to ``SIGINT`` and ``SIGTERM``) using
    ``signal_registrar`` (defaults to the running loop's
    :meth:`~asyncio.AbstractEventLoop.add_signal_handler`). Each handler
    sets :attr:`stop_event`, which long-running coroutines can watch to
    cooperatively shut down.

    Inside the ``async with`` block, :meth:`run_forever` awaits the user
    coroutine factory; the process exits once that coroutine returns.
    """

    def __init__(
        self,
        coro_factory: CoroFactory,
        *,
        shutdown_signals: tuple[int, ...] = (signal.SIGINT, signal.SIGTERM),
        signal_registrar: SignalRegistrar | None = None,
    ) -> None:
        self._coro_factory = coro_factory
        self._shutdown_signals = shutdown_signals
        self._signal_registrar = signal_registrar
        self._handlers: dict[int, Callable[[], None]] = {}
        self._stop = asyncio.Event()

    @property
    def stop_event(self) -> asyncio.Event:
        """Event that fires when shutdown is requested."""
        return self._stop

    @property
    def handlers(self) -> HandlerMap:
        """Read-only copy of the signal -> handler map populated on entry."""
        return dict(self._handlers)

    async def __aenter__(self) -> BaseProcess:
        loop = asyncio.get_running_loop()
        registrar = self._signal_registrar or _default_signal_registrar(loop)
        for sig in self._shutdown_signals:
            handler = self._stop.set
            self._handlers[sig] = handler
            registrar(sig, handler)
        return self

    async def __aexit__(self, *exc: object) -> None:
        # Idempotent: ensures any coroutine waiting on stop_event returns even
        # if no signal was delivered (e.g. the coroutine raised and we are
        # unwinding the context).
        self._stop.set()

    async def run_forever(self) -> None:
        """Invoke the coroutine factory and await its completion."""
        await self._coro_factory()


async def run_forever(
    coro_factory: CoroFactory,
    *,
    shutdown_event: asyncio.Event | None = None,
) -> None:
    """Run ``coro_factory`` once; return when either it completes or
    ``shutdown_event`` is set.

    The coroutine and a watcher for the shutdown event are scheduled
    concurrently. The function returns as soon as either finishes; pending
    tasks are cancelled so the call does not leak coroutines. Exceptions
    raised by the coroutine are re-raised to the caller.
    """
    stop = shutdown_event or asyncio.Event()
    coro_task = asyncio.create_task(coro_factory())
    stop_task = asyncio.create_task(stop.wait())
    try:
        done, pending = await asyncio.wait(
            {coro_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
    except BaseException:
        coro_task.cancel()
        stop_task.cancel()
        raise
    for task in pending:
        task.cancel()
    if coro_task in done:
        exc = coro_task.exception()
        if exc is not None:
            raise exc
