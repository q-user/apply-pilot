"""Back-compat shim — :class:`SendMessageRequest` moved to :mod:`messaging`."""

from apply_pilot.features.messaging.dto import SendMessageRequest

__all__ = ["SendMessageRequest"]
