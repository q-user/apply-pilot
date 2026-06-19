"""Channel-agnostic messaging code shared between Telegram and MAX bots.

This module holds the pieces of the messaging integration that do not
depend on the underlying transport: the action handlers
(``/accept``, ``/defer``, ``/reject``, ``/regenerate``, ``/review``),
the :class:`SendMessageRequest` DTO, the pure digest renderer, and
the :class:`MessagingAccountRepository` Protocol that lets the action
handlers resolve a local user from a channel-specific user id.

Public surface:

* :class:`MessagingAccountRepository` — channel-agnostic account repo
  Protocol.
* :class:`SendMessageRequest` — the DTO action handlers return.
* :func:`render_digest_message` — pure Markdown digest renderer.
* Action classes — :class:`AcceptActionHandler`,
  :class:`DeferActionHandler`, :class:`RejectActionHandler`,
  :class:`RegenerateActionHandler`, :class:`ReviewActionHandler`.
* Command parsers — :func:`parse_accept_command` and friends.
"""

from apply_pilot.features.messaging.actions.accept import (
    ACCEPT_HELP_TEXT,
    AcceptActionHandler,
    AcceptCommand,
    parse_accept_command,
)
from apply_pilot.features.messaging.actions.defer import (
    DEFER_HELP_TEXT,
    DeferActionHandler,
    DeferCommand,
    parse_defer_command,
)
from apply_pilot.features.messaging.actions.regenerate import (
    REGENERATE_HELP_TEXT,
    RegenerateActionHandler,
    RegenerateCommand,
    parse_regenerate_command,
)
from apply_pilot.features.messaging.actions.reject import (
    REJECT_HELP_TEXT,
    RejectActionHandler,
    RejectCommand,
    parse_reject_command,
)
from apply_pilot.features.messaging.actions.review import (
    REVIEW_HELP_TEXT,
    ReviewActionHandler,
    ReviewCommand,
    parse_review_command,
    render_review_card,
)
from apply_pilot.features.messaging.digest.render import render_digest_message
from apply_pilot.features.messaging.dto import SendMessageRequest
from apply_pilot.features.messaging.protocols import MessagingAccountRepository

__all__ = [
    "ACCEPT_HELP_TEXT",
    "DEFER_HELP_TEXT",
    "AcceptActionHandler",
    "AcceptCommand",
    "DeferActionHandler",
    "DeferCommand",
    "MessagingAccountRepository",
    "REGENERATE_HELP_TEXT",
    "REJECT_HELP_TEXT",
    "REVIEW_HELP_TEXT",
    "RegenerateActionHandler",
    "RegenerateCommand",
    "RejectActionHandler",
    "RejectCommand",
    "ReviewActionHandler",
    "ReviewCommand",
    "SendMessageRequest",
    "render_digest_message",
    "parse_accept_command",
    "parse_defer_command",
    "parse_regenerate_command",
    "parse_reject_command",
    "parse_review_command",
    "render_review_card",
]
