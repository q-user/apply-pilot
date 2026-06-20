"""Apply-worker notifier for the MAX channel (M9, issue #188).

:class:`MaxApplyNotifier` is the MAX-side twin of
:class:`apply_pilot.features.apply_worker.notifications.TelegramApplyNotifier`.
It satisfies the same :class:`ApplyNotifier` Protocol — a single
``notify(user_id, *, job, status)`` entry point — so the apply worker
can fan out terminal-state notifications to Telegram and MAX users
simultaneously without branching on the channel.

Differences from the Telegram notifier are deliberate and small:

* The :class:`MaxBot` ``send_message`` coroutine is awaited directly
  because the apply worker already runs in an event loop (it is itself
  async at the boundary that calls the notifier). The Telegram notifier
  needs a sync-to-async bridge because it is invoked from sync service
  methods; the MAX notifier has no such constraint.
* The message body is the short ``f"Apply job {job.id} — {status}"``
  format agreed in the issue: the MAX side does not (yet) render
  per-status emoji, and a verbose template would duplicate the audit
  log surface the worker already writes.
* When the user has no linked MAX account the notifier is a no-op — the
  same invariant the Telegram notifier upholds.
* Send failures are caught and logged, never re-raised: a notification
  transport is best-effort and must not break the apply worker's
  state-machine transitions.
"""

from __future__ import annotations

import logging
import uuid

from apply_pilot.features.apply_worker.models import ApplyJob
from apply_pilot.features.max.bot import MaxBot
from apply_pilot.features.max.repository import MaxAccountRepository

#: Module-level logger for the MAX notifier. The dotted name keeps the
#: log records consistent with the rest of the slice so operators can
#: ``grep`` ``apply_pilot.features.max.notifier`` in production.
_LOGGER = logging.getLogger("apply_pilot.features.max.notifier")


class MaxApplyNotifier:
    """Send a short MAX message for a terminal :class:`ApplyJob`.

    The notifier is collaborator-injected: tests pass the in-memory
    :class:`~apply_pilot.features.max.repository.InMemoryMaxAccountRepository`
    and a recording stand-in for :class:`MaxBot`; production wiring
    plugs in the SQLAlchemy-backed account repo and the live bot.
    """

    def __init__(
        self,
        max_account_repo: MaxAccountRepository,
        max_bot: MaxBot,
    ) -> None:
        self._max_account_repo = max_account_repo
        self._max_bot = max_bot

    async def notify(self, user_id: uuid.UUID, *, job: ApplyJob, status: str) -> None:
        """Send ``f"Apply job {job.id} — {status}"`` to ``user_id``'s MAX chat.

        Returns immediately when the user has no linked MAX account —
        the apply worker must keep working for users who interact only
        via the HTTP dashboard or Telegram. Send errors are caught and
        logged so a transport hiccup never breaks the worker's
        state-machine transitions.
        """
        account = self._max_account_repo.find_by_user_id(user_id)
        if account is None:
            return None

        text = f"Apply job {job.id} — {status}"
        try:
            await self._max_bot.send_message(account.max_user_id, text)
        except Exception:
            _LOGGER.exception(
                "max.apply_notification.failed",
                extra={
                    "event": "max.apply_notification.failed",
                    "user_id": str(user_id),
                    "job_id": str(job.id),
                },
            )
        return None


__all__ = ["MaxApplyNotifier"]
