"""Long-running MAX bot polling loop.

:class:`MaxBotProcess` wraps the bot's polling loop in the project's
standard :class:`~apply_pilot.runtime.process.BaseProcess` so that SIGINT and
SIGTERM shut the bot down gracefully (drain in-flight ``getUpdates``,
close the HTTP client, exit 0).

The polling loop is intentionally minimal: it calls ``getUpdates`` with the
configured long-poll timeout, advances the ``marker`` past every received
batch, and routes each update through :meth:`MaxBot.handle_update`. MAX
assigns the marker server-side (an opaque int64) — unlike Telegram's
client-side ``update_id`` — so the loop simply echoes back whatever the
last response carried.

The action handlers (``AcceptActionHandler`` and friends) are wired with
``__new__`` to bypass their ``__init__`` because the real wiring needs the
match service integration that lands in a follow-up. That keeps the
console-script entry point bootable for a smoke test (``MAX_BOT_TOKEN``
fail-fast, log configured, process starts) without pretending the action
surface is production-ready.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from typing import TYPE_CHECKING, Any

from apply_pilot.runtime.process import BaseProcess

if TYPE_CHECKING:
    # ``MaxBot`` is referenced in a type annotation only. Importing it at
    # runtime would pull in ``apply_pilot.features.messaging.dto`` which
    # transitively triggers a pre-existing circular import in the
    # ``matches`` ↔ ``apply_worker`` graph. The annotation is a string at
    # runtime (thanks to ``from __future__ import annotations``) so the
    # class definition does not need the live symbol.
    from apply_pilot.features.max.bot import MaxBot

_LOGGER = logging.getLogger("apply_pilot.features.max.process")

# Wait a couple of seconds after a transient transport error before the next
# poll. Anything longer would noticeably delay recovery from a network blip;
# anything shorter would burn CPU in a tight loop while MAX is down.
_ERROR_BACKOFF_SECONDS = 2.0


class MaxBotProcess(BaseProcess):
    """A :class:`BaseProcess` that runs the MAX bot's polling loop.

    The bot is injected so tests can swap it for a fake. ``name`` is passed
    straight through to :class:`BaseProcess` and is used in structured log
    lines (see ``process.start`` and ``process.shutdown`` events).
    """

    def __init__(self, bot: MaxBot, *, name: str = "max-bot") -> None:
        super().__init__(name=name, signals=(signal.SIGINT, signal.SIGTERM))
        self._bot = bot
        # Track the last ``marker`` the server has handed us. None means
        # "we have never polled yet" — the MAX API treats that as "start
        # from the current tail". Unlike Telegram's ``update_id``, the
        # marker is server-assigned and we just echo it back.
        self._marker: int | None = None

    @property
    def bot(self) -> MaxBot:
        return self._bot

    async def run(self) -> int:
        """Run the polling loop until shutdown is requested.

        Returns 0 on a clean shutdown. The contract mirrors
        :meth:`BaseProcess.run`: callers wire this into an entry point
        (``apply-pilot-max-bot`` script, supervisor, ...) and rely on the
        return code to surface success vs. failure.
        """
        self.start()
        try:
            while not self.is_shutdown_set():
                try:
                    updates, new_marker = await self._bot.get_updates(marker=self._marker)
                except asyncio.CancelledError:
                    # The runtime asks us to stop via task cancellation; let
                    # it propagate so the surrounding ``asyncio.run`` exits
                    # cleanly.
                    raise
                except Exception:
                    _LOGGER.exception(
                        "max.getUpdates.failed",
                        extra={"event": "max.getUpdates.failed", "bot": "max"},
                    )
                    # Sleep on the shutdown event so an in-flight error does
                    # not turn into a hot retry loop, but bail out
                    # immediately if shutdown arrives during the backoff.
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(
                            self._wait_for_shutdown_no_log(),
                            timeout=_ERROR_BACKOFF_SECONDS,
                        )
                    continue

                # Advance the marker BEFORE dispatch so a crash mid-dispatch
                # cannot replay the same batch on the next poll. The MAX
                # API's marker is opaque to us — we only need to echo back
                # the most recent value the server handed us.
                if new_marker is not None:
                    self._marker = new_marker

                for update in updates:
                    await self._dispatch_update(update)
            return 0
        finally:
            await self._bot.aclose()
            self.stop()

    async def _wait_for_shutdown_no_log(self) -> None:
        """Wait for the shutdown event without emitting a log line.

        ``BaseProcess.wait_for_shutdown`` logs a ``process.shutdown`` event
        unconditionally; using it from an error-recovery branch would emit
        misleading noise while the worker is still alive. This wrapper
        delegates to the underlying event without the side effect.
        """
        await asyncio.wait(
            {asyncio.create_task(self._shutdown_event.wait())},
            return_when=asyncio.FIRST_COMPLETED,
        )

    async def _dispatch_update(self, update: dict[str, Any]) -> None:
        """Route a single update through the bot and send any reply it produces.

        Both the dispatch and the outbound send swallow exceptions and
        log structured events: a single bad payload must not bring the
        polling loop down.
        """
        try:
            response = await self._bot.handle_update(update)
        except Exception:
            _LOGGER.exception(
                "max.handle_update.failed",
                extra={"event": "max.handle_update.failed"},
            )
            return

        if response is None:
            return

        try:
            await self._bot.send_message(response.chat_id, response.text)
        except Exception:
            _LOGGER.exception(
                "max.sendMessage.failed",
                extra={
                    "event": "max.sendMessage.failed",
                    "chat_id": response.chat_id,
                },
            )


def main() -> int:
    """Synchronous entry point for the ``apply-pilot-max-bot`` console script.

    Reads settings from the environment via :func:`apply_pilot.config.get_max_settings`,
    configures logging, builds the bot with the in-memory account repository
    and linking service, and runs the process inside a fresh asyncio event
    loop. The action handlers are passed as bare ``__new__``-constructed
    instances because the real wiring needs the match service integration
    that lands in a follow-up PR — see issue #187 for context. The script
    returns the process exit code so supervisors (``docker``, ``systemd``,
    ``honcho``) can react accordingly.
    """
    # Imports are deferred so the import graph stays small for callers that
    # only want the bot dispatcher (notably the test suite) and so the
    # action-handler modules are not pulled in unless we are actually
    # booting the console script.
    #
    # Some of these imports transitively trigger a pre-existing circular
    # import in the ``matches`` ↔ ``apply_worker`` graph. The
    # ``MAX_BOT_TOKEN`` fail-fast check below must therefore run BEFORE
    # any of the unsafe imports — otherwise the test that expects
    # ``ValueError`` on a missing token would instead see ``ImportError``.
    from apply_pilot.config import get_max_settings
    from apply_pilot.shared.logging import configure_logging

    configure_logging()
    # ``get_max_settings`` raises ``ValueError`` eagerly if ``MAX_BOT_TOKEN``
    # is unset so misconfiguration surfaces at process start, not at the
    # first failed HTTP call.
    settings = get_max_settings()

    # Imports that transitively reach ``apply_pilot.features.messaging``
    # (and therefore the circular-import chain) are deferred until after
    # the config check above.
    from apply_pilot.features.max import InMemoryMaxAccountRepository
    from apply_pilot.features.max.bot import MaxBot
    from apply_pilot.features.max.linking import MaxLinkingService
    from apply_pilot.features.messaging.actions.accept import AcceptActionHandler
    from apply_pilot.features.messaging.actions.defer import DeferActionHandler
    from apply_pilot.features.messaging.actions.regenerate import (
        RegenerateActionHandler,
    )
    from apply_pilot.features.messaging.actions.reject import RejectActionHandler
    from apply_pilot.features.messaging.actions.review import ReviewActionHandler

    repo = InMemoryMaxAccountRepository()
    linking = MaxLinkingService()
    # The action handlers are not wired fully yet — the real wiring lands
    # in a follow-up PR after the match service integration is settled.
    # Bypassing ``__init__`` via ``__new__`` gives us a bare instance that
    # satisfies ``MaxBot``'s constructor signature without dragging in the
    # match service dependency graph.
    accept = AcceptActionHandler.__new__(AcceptActionHandler)
    defer = DeferActionHandler.__new__(DeferActionHandler)
    reject = RejectActionHandler.__new__(RejectActionHandler)
    review = ReviewActionHandler.__new__(ReviewActionHandler)
    regenerate = RegenerateActionHandler.__new__(RegenerateActionHandler)
    bot = MaxBot(
        settings=settings,
        account_repo=repo,
        linking_service=linking,
        accept_handler=accept,
        defer_handler=defer,
        reject_handler=reject,
        review_handler=review,
        regenerate_handler=regenerate,
    )
    process = MaxBotProcess(bot=bot)
    return asyncio.run(process.run())


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["MaxBotProcess", "main"]
