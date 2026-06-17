"""Telegram-channels slice (M7, issue #58).

Listens to a configured set of public Telegram channels, classifies posts
as vacancy / non-vacancy, normalises the accepted ones into the canonical
:class:`~job_apply.features.sources.models.Vacancy` model, and pushes
them through the same :class:`SourceService` ingest path every other
source uses.

Why a top-level slice and not a sub-module of ``features.telegram``
------------------------------------------------------------------

``features.telegram`` is the *user-facing* bot — the surface that
accepts commands, dispatches actions, and renders digests for the
candidate. ``telegram_channels`` is the *producer* surface — the
pipeline that ingests vacancies from public channels. Mixing the two
would couple the long-running scanner to the bot's command-handling
imports and the test surface for the bot's command parser. Keeping
them as sibling slices (mirroring the ``hh`` / ``sources`` split
introduced in issue #70) lets each one evolve independently.

The slice ships:

* :class:`TelegramChannelClient` — :class:`typing.Protocol` every
  transport implements. Tests inject :class:`InMemoryTelegramChannelClient`,
  production wiring will inject a real ``python-telegram-bot`` or
  TDLib-based client in a follow-up.
* :class:`TelegramChannelConfig` + :func:`get_telegram_channels_config`
  — env-driven configuration (the list of channels to watch, the poll
  interval).
* :class:`TelegramChannelClassifier` — keyword-driven "is this a
  vacancy post?" check.
* :class:`TelegramChannelSourceAdapter` — the
  :class:`~job_apply.features.sources.adapter.SourceAdapter` Protocol
  implementation. ``search()`` pulls from the client, ``apply()`` raises
  :class:`NotImplementedError` (channels are read-only sources).
* :class:`TelegramChannelScanner` — :class:`BaseProcess` that
  periodically calls ``search()`` and pipes the result through
  :meth:`~job_apply.features.sources.service.SourceService.ingest_vacancy_deduped`.
"""

from __future__ import annotations

from job_apply.features.telegram_channels.adapter import TelegramChannelSourceAdapter
from job_apply.features.telegram_channels.classifier import TelegramChannelClassifier
from job_apply.features.telegram_channels.client import (
    InMemoryTelegramChannelClient,
    TelegramChannelClient,
    TelegramChannelMessage,
)
from job_apply.features.telegram_channels.config import (
    TelegramChannelConfig,
    TelegramChannelsSettings,
    get_telegram_channels_settings,
)
from job_apply.features.telegram_channels.normalizer import TelegramChannelNormalizer
from job_apply.features.telegram_channels.scanner import TelegramChannelScanner

__all__ = [
    "InMemoryTelegramChannelClient",
    "TelegramChannelClassifier",
    "TelegramChannelClient",
    "TelegramChannelConfig",
    "TelegramChannelMessage",
    "TelegramChannelScanner",
    "TelegramChannelSourceAdapter",
    "TelegramChannelNormalizer",
    "TelegramChannelsSettings",
    "get_telegram_channels_settings",
]
