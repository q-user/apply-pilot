"""Long-running Telegram bot polling loop.

:class:`TelegramBotProcess` wraps the bot's polling loop in the project's
standard :class:`~job_apply.runtime.process.BaseProcess` so that SIGINT and
SIGTERM shut the bot down gracefully (drain in-flight ``getUpdates``,
close the HTTP client, exit 0).

The polling loop is intentionally minimal: it calls ``getUpdates`` with the
configured long-poll timeout, advances the offset past every received
update, and routes each update through :meth:`TelegramBot.handle_update`.
Webhooks are deliberately not implemented — long polling keeps the skeleton
simple and side-steps the need for an external ingress.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from typing import Any

from job_apply.features.telegram.bot import TelegramBot
from job_apply.runtime.process import BaseProcess

_LOGGER = logging.getLogger("job_apply.features.telegram.process")

# Wait a couple of seconds after a transient transport error before the next
# poll. Anything longer would noticeably delay recovery from a network blip;
# anything shorter would burn CPU in a tight loop while Telegram is down.
_ERROR_BACKOFF_SECONDS = 2.0


class TelegramBotProcess(BaseProcess):
    """A :class:`BaseProcess` that runs the Telegram bot's polling loop.

    The bot is injected so tests can swap it for a fake. ``name`` is passed
    straight through to :class:`BaseProcess` and is used in structured log
    lines (see ``process.start`` and ``process.shutdown`` events).
    """

    def __init__(self, bot: TelegramBot, *, name: str = "telegram-bot") -> None:
        super().__init__(name=name, signals=(signal.SIGINT, signal.SIGTERM))
        self._bot = bot
        # Track the last ``update_id`` we have acknowledged so the next poll
        # only sees new traffic. None means "we have never polled yet".
        self._offset: int | None = None

    @property
    def bot(self) -> TelegramBot:
        return self._bot

    async def run(self) -> int:
        """Run the polling loop until shutdown is requested.

        Returns 0 on a clean shutdown. The contract mirrors
        :meth:`BaseProcess.run`: callers wire this into an entry point
        (``apply-pilot-bot`` script, supervisor, ...) and rely on the
        return code to surface success vs. failure.
        """
        self.start()
        try:
            while not self.is_shutdown_set():
                try:
                    updates = await self._bot.get_updates(offset=self._offset)
                except asyncio.CancelledError:
                    # The runtime asks us to stop via task cancellation; let
                    # it propagate so the surrounding ``asyncio.run`` exits
                    # cleanly.
                    raise
                except Exception:
                    _LOGGER.exception(
                        "telegram.getUpdates.failed",
                        extra={"event": "telegram.getUpdates.failed"},
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
        """Advance the offset and send any reply produced by the dispatcher."""
        update_id = update.get("update_id")
        if isinstance(update_id, int):
            self._offset = update_id + 1
        else:
            _LOGGER.warning(
                "telegram.update.missing_id",
                extra={"event": "telegram.update.missing_id"},
            )

        try:
            response = self._bot.handle_update(update)
        except Exception:
            _LOGGER.exception(
                "telegram.handle_update.failed",
                extra={
                    "event": "telegram.handle_update.failed",
                    "update_id": update_id,
                },
            )
            return

        if response is None:
            return

        try:
            await self._bot.send_message(response.chat_id, response.text)
        except Exception:
            _LOGGER.exception(
                "telegram.sendMessage.failed",
                extra={
                    "event": "telegram.sendMessage.failed",
                    "chat_id": response.chat_id,
                    "update_id": update_id,
                },
            )


def main() -> int:
    """Synchronous entry point for the ``apply-pilot-bot`` console script.

    Reads settings from the environment via :func:`job_apply.config.get_telegram_settings`,
    configures logging, builds the bot, and runs the process inside a fresh
    asyncio event loop. The script returns the process exit code so
    supervisors (``docker``, ``systemd``, ``honcho``) can react accordingly.
    """
    # Imports are deferred so the import graph stays small for callers that
    # only want the bot dispatcher (notably the test suite).
    from job_apply.config import get_telegram_settings
    from job_apply.shared.logging import configure_logging

    configure_logging()
    settings = get_telegram_settings()
    bot = TelegramBot(settings=settings)
    process = TelegramBotProcess(bot=bot)
    return asyncio.run(process.run())


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["TelegramBotProcess", "main"]
