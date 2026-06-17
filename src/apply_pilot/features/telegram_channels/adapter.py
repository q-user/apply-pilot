"""Telegram-channel :class:`SourceAdapter` (M7, issue #58).

:class:`TelegramChannelSourceAdapter` is the slice's
:class:`~apply_pilot.features.sources.adapter.SourceAdapter`
implementation. It composes four narrow collaborators:

* :class:`TelegramChannelClient` — the transport. Tests inject
  :class:`InMemoryTelegramChannelClient`; production wiring will
  plug in a real ``python-telegram-bot`` or TDLib-based client in
  a follow-up.
* :class:`TelegramChannelClassifier` — keyword-driven
  vacancy/non-vacancy pre-filter.
* :class:`TelegramChannelNormalizer` — pure mapper from a raw
  message dict to a canonical :class:`Vacancy`.
* ``channels`` — the static :class:`TelegramChannelConfig` list
  the slice watches.

The cross-source orchestration code (the future
:class:`VacancySearchService`, the :class:`ApplyWorker`) only ever
sees a :class:`SourceAdapter` Protocol, so this is the only place
the slice's collaborators are wired together.

Why a separate adapter and not a normaliser branch
-------------------------------------------------

A future ``ApplyWorker`` invocation on a Telegram-channel vacancy
will land in :meth:`apply`. Channels are read-only sources — there
is no programmatic apply via the channel — so the adapter raises
:class:`NotImplementedError` and the worker dead-letters the job
(this is the contract :class:`SourceAdapter` already documents).
Telegram channel ingest never goes through the apply path, so
implementing it here as a non-op is the cheapest way to keep the
cross-source orchestration uniform.
"""

from __future__ import annotations

import logging
from typing import Any

from apply_pilot.features.apply_worker.models import ApplyJob
from apply_pilot.features.apply_worker.runtime import ApplyResult
from apply_pilot.features.screening.models import ScreeningQuestion
from apply_pilot.features.sources.adapter import SourceQuery
from apply_pilot.features.sources.models import Vacancy
from apply_pilot.features.telegram_channels.classifier import TelegramChannelClassifier
from apply_pilot.features.telegram_channels.client import TelegramChannelClient
from apply_pilot.features.telegram_channels.config import TelegramChannelConfig
from apply_pilot.features.telegram_channels.normalizer import (
    SOURCE_NAME,
    TelegramChannelNormalizer,
)

_LOG_PREFIX = "apply_pilot.features.telegram_channels.adapter."


class TelegramChannelSourceAdapter:
    """Telegram-channel :class:`SourceAdapter` implementation.

    Translating the cross-source :class:`SourceQuery` into a
    Telegram-channel query is a no-op — the slice does not honour
    pagination, salary floors, or text filters, because the channel
    transport is a long-poll feed, not a search API. The slice's
    natural filter is "every new message since the last poll", and
    the classifier is the only per-message filter that runs.

    Attributes
    ----------
    name:
        Stable source identifier (``"telegram_channel"``). The
        :class:`AdapterRegistry` looks the adapter up under this
        key, and the
        :attr:`~apply_pilot.features.sources.models.Vacancy.source`
        column carries it for every persisted row.
    """

    name: str = SOURCE_NAME

    def __init__(
        self,
        *,
        client: TelegramChannelClient,
        classifier: TelegramChannelClassifier,
        normalizer: TelegramChannelNormalizer,
        channels: list[TelegramChannelConfig] | None = None,
    ) -> None:
        self._client = client
        self._classifier = classifier
        self._normalizer = normalizer
        # Copy so callers cannot mutate the slice's channel list
        # after the adapter is constructed.
        self._channels: list[TelegramChannelConfig] = list(channels or [])
        self._logger = logging.getLogger(_LOG_PREFIX + "TelegramChannelSourceAdapter")

    # ------------------------------------------------------------------
    # Read-only collaborators
    # ------------------------------------------------------------------

    @property
    def client(self) -> TelegramChannelClient:
        """Return the injected Telegram-channel client (read-only)."""
        return self._client

    @property
    def classifier(self) -> TelegramChannelClassifier:
        """Return the injected classifier (read-only)."""
        return self._classifier

    @property
    def normalizer(self) -> TelegramChannelNormalizer:
        """Return the injected normaliser (read-only)."""
        return self._normalizer

    @property
    def channels(self) -> list[TelegramChannelConfig]:
        """Return a copy of the configured channel list (read-only)."""
        return list(self._channels)

    # ------------------------------------------------------------------
    # SourceAdapter
    # ------------------------------------------------------------------

    async def search(self, query: SourceQuery) -> list[dict[str, Any]]:
        """Fetch raw vacancy dicts from every configured channel.

        The ``query`` argument is accepted for Protocol compatibility
        but is not used — Telegram channels are a long-poll feed, not
        a search API. The classifier is the only per-message filter
        that runs.

        Returns:
            One raw dict per message that the classifier accepted.
            The dicts are the
            :meth:`~apply_pilot.features.telegram_channels.client.TelegramChannelMessage.to_raw_dict`
            shape, ready to be fed to
            :meth:`TelegramChannelSourceAdapter.normalize` or
            :meth:`~apply_pilot.features.sources.service.SourceService.ingest_vacancy_deduped`.
        """
        raws: list[dict[str, Any]] = []
        for channel in self._channels:
            messages = await self._client.fetch_new_messages(channel.identifier)
            for message in messages:
                if not self._classifier.is_vacancy_post(message.text):
                    continue
                raws.append(message.to_raw_dict())
        return raws

    def normalize(self, raw: dict[str, Any]) -> Vacancy:
        """Map ``raw`` to a canonical :class:`Vacancy`.

        Delegates to the slice's
        :class:`TelegramChannelNormalizer`; the adapter does not
        add any field of its own.
        """
        return self._normalizer.normalize(raw)

    def extract_screening_questions(self, raw: dict[str, Any]) -> list[ScreeningQuestion]:
        """Return an empty list — Telegram channels do not carry screening questions.

        The Protocol mandates the method; the slice simply has
        nothing to extract.
        """
        return []

    async def apply(self, job: ApplyJob) -> ApplyResult:
        """Reject — channels are read-only sources.

        Raises:
            NotImplementedError: Always. The :class:`ApplyWorker`
                catches this exception and dead-letters the job, so
                the slice does not need a separate "is_applyable"
                flag.
        """
        raise NotImplementedError(
            "telegram_channel source does not support programmatic apply; "
            "the user must apply through the original channel post."
        )


__all__ = ["TelegramChannelSourceAdapter"]
