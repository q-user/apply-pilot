"""Back-compat shim — :func:`render_digest_message` moved to :mod:`messaging`."""

from apply_pilot.features.messaging.digest.render import render_digest_message

__all__ = ["render_digest_message"]
