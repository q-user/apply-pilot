"""Telegram command action handlers.

Each action is a small, self-contained class that knows how to handle one
Telegram command (``/accept``, ``/reject``). The :class:`TelegramBot`
dispatcher holds a reference to each action and routes incoming updates
to the appropriate handler based on the command name. Actions are
injected through the constructor so tests can swap in fakes and the
action's collaborators stay explicit.

This package follows the Vertical Slices Architecture: every command
lives next to the tests and supporting code that change together.
"""

from job_apply.features.telegram.actions.accept import (
    ACCEPT_HELP_TEXT,
    AcceptActionHandler,
    AcceptCommand,
    parse_accept_command,
)
from job_apply.features.telegram.actions.defer import (
    DEFER_HELP_TEXT,
    DeferActionHandler,
    DeferCommand,
    parse_defer_command,
)
from job_apply.features.telegram.actions.regenerate import (
    REGENERATE_HELP_TEXT,
    RegenerateActionHandler,
    RegenerateCommand,
    parse_regenerate_command,
)
from job_apply.features.telegram.actions.reject import (
    REJECT_HELP_TEXT,
    RejectActionHandler,
    RejectCommand,
    parse_reject_command,
)
from job_apply.features.telegram.actions.review import (
    REVIEW_HELP_TEXT,
    ReviewActionHandler,
    ReviewCommand,
    parse_review_command,
    render_review_card,
)

__all__ = [
    "ACCEPT_HELP_TEXT",
    "AcceptActionHandler",
    "AcceptCommand",
    "DEFER_HELP_TEXT",
    "DeferActionHandler",
    "DeferCommand",
    "REGENERATE_HELP_TEXT",
    "RegenerateActionHandler",
    "RegenerateCommand",
    "REJECT_HELP_TEXT",
    "REVIEW_HELP_TEXT",
    "RejectActionHandler",
    "RejectCommand",
    "ReviewActionHandler",
    "ReviewCommand",
    "parse_accept_command",
    "parse_defer_command",
    "parse_regenerate_command",
    "parse_reject_command",
    "parse_review_command",
    "render_review_card",
]
