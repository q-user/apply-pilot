"""Telegram command action handlers.

Each action is a small, self-contained class that knows how to handle one
Telegram command (``/reject``, ...). The :class:`TelegramBot` dispatcher
holds a reference to each action and routes incoming updates to the
appropriate handler based on the command name. Actions are injected
through the constructor so tests can swap in fakes and the action's
collaborators stay explicit.

This package follows the Vertical Slices Architecture: every command
lives next to the tests and supporting code that change together.
"""

from job_apply.features.telegram.actions.reject import (
    RejectActionHandler,
    RejectCommand,
    parse_reject_command,
)

__all__ = ["RejectActionHandler", "RejectCommand", "parse_reject_command"]
