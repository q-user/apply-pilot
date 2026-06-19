"""Channel-agnostic digest rendering.

The :func:`render_digest_message` function lives here because it has
no channel-specific code — it just turns a :class:`UserStats` value
object into a Markdown string. The channel-specific sender (Telegram
or MAX) imports it from this module to keep the rendering logic in
one place.
"""

from apply_pilot.features.messaging.digest.render import render_digest_message

__all__ = ["render_digest_message"]
