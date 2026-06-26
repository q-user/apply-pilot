"""Apply-worker notifications (M5, issue #50).

When an :class:`~apply_pilot.features.apply_worker.models.ApplyJob`
reaches a terminal state (``succeeded`` / ``failed`` / ``dead_letter`` /
``cancelled``) the user gets a Telegram message with a short
status-specific summary. The notification fan-out lives behind the
:class:`ApplyNotifier` Protocol so the service can stay
infrastructure-free and tests can drive the slice with a recording
fake.

Cross-slice contract
--------------------

The :class:`TelegramApplyNotifier` is the production implementation.
It depends on:

* a :class:`~apply_pilot.features.telegram.repository.TelegramAccountRepository`
  — to resolve the local ``user_id`` to a Telegram chat id;
* a :class:`~apply_pilot.features.telegram.bot.TelegramBot` — to actually
  post the message. The bot is async; the notifier's public
  :meth:`notify` is sync so the (sync) service methods can call it
  after persisting. The sync-to-async bridge is handled inside
  :meth:`TelegramApplyNotifier.notify`: in an async context the bot
  call is scheduled as a task (fire-and-forget), in a sync context
  ``asyncio.run`` runs the coroutine to completion.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Protocol

from apply_pilot.features.apply_worker.models import ApplyJob, ApplyJobStatus
from apply_pilot.features.telegram.repository import TelegramAccountRepository

#: Number of leading hex chars of the job id surfaced to the user.
#: Eight is enough for a user to disambiguate two recent jobs without
#: spilling the full UUID into a Telegram message.
_SHORT_JOB_ID_CHARS: int = 8


class ApplyNotifier(Protocol):
    """The apply-worker's outbound-notification seam.

    The notifier is invoked by :class:`ApplyJobService` after a job
    transitions to a terminal state. The protocol is sync (``-> None``)
    so the service can call it from its own sync transition methods
    without changing their signatures. Implementations are free to
    bridge to an async transport internally.
    """

    def notify(self, user_id: uuid.UUID, *, job: ApplyJob, status: str) -> None: ...


class TelegramApplyNotifier:
    """Send a short Telegram message for a terminal :class:`ApplyJob`.

    The class is collaborator-injected: tests pass the in-memory
    :class:`~apply_pilot.features.telegram.repository.InMemoryTelegramAccountRepository`
    and a recording stand-in for
    :class:`~apply_pilot.features.telegram.bot.TelegramBot`; production
    wiring in :mod:`apply_pilot.features.apply_worker.api` (or the
    process entry-point) plugs in the SQLAlchemy-backed account repo
    and the real bot.
    """

    def __init__(
        self,
        telegram_account_repo: TelegramAccountRepository,
        telegram_bot: object,
    ) -> None:
        self._telegram_account_repo = telegram_account_repo
        # The bot is typed as ``object`` because the notifier only ever
        # calls ``send_message(chat_id, text)`` and the real
        # :class:`TelegramBot` is async. A tighter type would force
        # the tests' fake to be the production class; ``object`` keeps
        # the slice decoupled.
        self._telegram_bot = telegram_bot

    def notify(self, user_id: uuid.UUID, *, job: ApplyJob, status: str) -> None:
        """Build the message for ``status`` and send it to ``user_id``'s chat.

        Returns immediately when the user has no linked Telegram
        account — the apply-worker slice must keep working for users
        who interact only via the HTTP dashboard.
        """
        account = self._telegram_account_repo.find_by_user_id(user_id)
        if account is None:
            return None

        text = _build_message(status=status, job=job)
        chat_id = account.telegram_user_id
        # The real :meth:`TelegramBot.send_message` is async. The
        # notifier's public contract is sync, so the bridge runs here:
        # * in an async context (the worker process) we schedule the
        #   bot call as a task and let the event loop drive it;
        # * in a sync context (a request handler or a unit test) we
        #   fall back to ``asyncio.run`` so the message goes out
        #   before this method returns.
        coro = self._telegram_bot.send_message(chat_id, text)  # type: ignore[unresolved-attribute]
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(coro)
        else:
            loop.create_task(coro)
        return None


def _build_message(*, status: str, job: ApplyJob) -> str:
    """Return the user-facing text for a terminal ``status`` transition.

    The message is intentionally short — Telegram is a notification
    surface, not a dashboard. The full audit trail is in the
    :class:`~apply_pilot.features.apply_worker.models.ApplyStatusHistory`
    rows the service writes alongside the transition.
    """
    if status == ApplyJobStatus.SUCCEEDED.value:
        return f"✅ Application submitted! (job: {str(job.id)[:_SHORT_JOB_ID_CHARS]})"
    if status == ApplyJobStatus.FAILED.value:
        return f"❌ Application failed: {job.last_error or 'unknown error'}. Will retry."
    if status == ApplyJobStatus.DEAD_LETTER.value:
        return (
            f"💀 Application gave up after {job.attempts} attempts: "
            f"{job.last_error or 'unknown error'}"
        )
    if status == ApplyJobStatus.CANCELLED.value:
        return "🚫 Application cancelled."
    # Unknown status: surface it verbatim so a future state added to
    # the enum does not silently drop the notification.
    return f"ℹ️ Application status: {status} (job: {str(job.id)[:_SHORT_JOB_ID_CHARS]})"


__all__ = ["ApplyNotifier", "TelegramApplyNotifier"]
