"""Channel-agnostic messaging protocols.

Defines the :class:`MessagingAccountRepository` Protocol so both the
Telegram and MAX slices can plug in their own account repository
implementation while the action handlers stay channel-agnostic.

Method-name rationale
---------------------

The Telegram concrete repos expose ``find_by_telegram_user_id`` and
``SqlAlchemyTelegramAccountRepository`` does the same. The MAX bot
will have a parallel ``find_by_max_user_id``. To keep the action
handlers channel-agnostic we declare a canonical method name on the
Protocol — ``find_by_external_user_id`` — and rely on structural
typing: each concrete repository adds the method as a thin alias to
its channel-specific lookup. This avoids a wide rename of the
existing Telegram repos while still letting the messaging code
depend on a single, stable interface.
"""

from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable


@runtime_checkable
class _MessagingAccount(Protocol):
    """The minimal account surface the action handlers depend on.

    Both :class:`apply_pilot.features.telegram.models.TelegramAccount`
    and the future ``MAXAccount`` model expose ``user_id`` (the local
    user the channel account is linked to), which is all the action
    handlers read. Duck typing covers the rest.
    """

    user_id: uuid.UUID


class MessagingAccountRepository(Protocol):
    """Channel-agnostic account repository used by the action handlers.

    The Protocol is intentionally narrow: every method listed here is
    used by at least one in-tree consumer in the ``messaging`` module.
    The :class:`apply_pilot.features.telegram.digest.DigestSender`
    uses the Telegram-specific ``list_all`` (via the concrete
    :class:`TelegramAccountRepository`) for broadcasting — that method
    is not part of this Protocol because the action handlers never
    enumerate accounts.

    Concrete repos satisfy the Protocol structurally: the Telegram
    repos add ``find_by_external_user_id`` as an alias to
    ``find_by_telegram_user_id``; the future MAX repos will add the
    same method as an alias to ``find_by_max_user_id``.
    """

    def find_by_external_user_id(
        self, external_user_id: int
    ) -> _MessagingAccount | None:
        """Return the linked account for ``external_user_id`` or ``None``."""
        ...

    def find_by_user_id(self, user_id: uuid.UUID) -> _MessagingAccount | None:
        """Return the linked account for the local ``user_id`` or ``None``."""
        ...


__all__ = ["MessagingAccountRepository"]
