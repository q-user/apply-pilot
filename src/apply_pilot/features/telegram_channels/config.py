"""Telegram-channels configuration (M7, issue #58).

The slice reads its configuration from environment variables so the
production wiring (an entry-point script, a Docker container) can
control which channels to watch and how often to poll without code
changes. The schema is intentionally small:

* ``TELEGRAM_CHANNELS`` — comma-separated list of channel identifiers
  (``@username`` for public channels, ``-100…`` for numeric ids).
  Empty / unset means "watch nothing".
* ``TELEGRAM_CHANNELS_POLL_INTERVAL`` — seconds between polls. Default
  ``60``. Validation rejects non-positive values so a typo never
  turns into a tight loop in production.

Why environment variables (and not a database table)
----------------------------------------------------

A database table would let admins change the channel list at runtime
without a restart, which is nice, but the slice does not (yet)
expose an admin endpoint to manage that table. Environment variables
are the project's existing convention (see
:class:`apply_pilot.config.TelegramSettings` for the bot token); a
follow-up issue can add the admin endpoint and migrate the source of
truth to the database without changing the slice's public surface.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

#: Default poll interval (seconds). One minute is a sensible
#: compromise: frequent enough to feel "live" without hammering the
#: Telegram API or burning CPU.
DEFAULT_POLL_INTERVAL_SECONDS: float = 60.0


@dataclass(frozen=True, slots=True)
class TelegramChannelConfig:
    """A single Telegram channel the slice watches.

    Attributes:
        identifier: Channel handle (``@username``) or numeric id
            (``-100…`` for supergroup channels).
        display_name: Optional human-friendly label; not used by the
            slice, exposed for dashboards / logs / follow-up wiring.
    """

    identifier: str
    display_name: str | None = None

    def __post_init__(self) -> None:
        if not self.identifier or not self.identifier.strip():
            raise ValueError(
                "TelegramChannelConfig.identifier must be a non-empty string "
                "(e.g. '@jobs' or '-1001234567890')."
            )


@dataclass(frozen=True, slots=True)
class TelegramChannelsSettings:
    """Settings for the Telegram-channels slice.

    The frozen dataclass is the slice's public surface; build it via
    :func:`get_telegram_channels_settings` (env-driven) or directly
    in tests.
    """

    channels: list[TelegramChannelConfig] = field(default_factory=list)
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS

    def __post_init__(self) -> None:
        if self.poll_interval_seconds <= 0:
            raise ValueError(
                f"poll_interval_seconds must be positive, got {self.poll_interval_seconds!r}"
            )


def get_telegram_channels_settings() -> TelegramChannelsSettings:
    """Build :class:`TelegramChannelsSettings` from the environment.

    Reads:

    * ``TELEGRAM_CHANNELS`` — comma-separated list of channel
      identifiers. Empty / unset yields an empty channel list (the
      scanner is a no-op in that case).
    * ``TELEGRAM_CHANNELS_POLL_INTERVAL`` — positive float (seconds).
      Defaults to :data:`DEFAULT_POLL_INTERVAL_SECONDS`.

    Raises:
        ValueError: If the poll interval env var is not parseable or
            is non-positive.
    """
    raw_channels = os.getenv("TELEGRAM_CHANNELS", "").strip()
    channels: list[TelegramChannelConfig] = []
    if raw_channels:
        for entry in raw_channels.split(","):
            identifier = entry.strip()
            if not identifier:
                # Trailing commas / extra spaces are silently dropped
                # so a sloppy env var still parses.
                continue
            channels.append(TelegramChannelConfig(identifier=identifier))

    raw_interval = os.getenv("TELEGRAM_CHANNELS_POLL_INTERVAL", "").strip()
    if raw_interval:
        try:
            poll_interval = float(raw_interval)
        except ValueError as exc:
            raise ValueError(
                f"TELEGRAM_CHANNELS_POLL_INTERVAL must be a positive number "
                f"(seconds), got {raw_interval!r}."
            ) from exc
    else:
        poll_interval = DEFAULT_POLL_INTERVAL_SECONDS

    return TelegramChannelsSettings(
        channels=channels,
        poll_interval_seconds=poll_interval,
    )


__all__ = [
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "TelegramChannelConfig",
    "TelegramChannelsSettings",
    "get_telegram_channels_settings",
]
