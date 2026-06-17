"""Writing-style memory vertical slice (M8, issue #66).

Public surface
--------------

* :class:`StyleMemoryEntry` — frozen DTO for a single style-memory
  row.
* :class:`StyleMemory` — frozen DTO for the user's aggregate read.
* :class:`StyleMemoryEntryModel` — SQLAlchemy ORM row.
* :class:`StyleMemoryService` — business logic (ingestion + read).
* :class:`StyleMemoryRepository` — Protocol contract.
* :class:`InMemoryStyleMemoryRepository` — fake for tests.
* :class:`SqlStyleMemoryRepository` — production implementation.
* :class:`StyleMemoryRead` — public DTO for the API.
* :func:`summarise_letter` — deterministic, LLM-free helper that
  produces a short ``style_summary`` for a cover letter.

The slice is fed by the ``/accept`` Telegram action: every accepted
cover letter is summarised and stored. The API surface is a single
``GET /writing-style-memory/me`` endpoint that returns the user's
aggregated summary.

Design notes
------------

* The slice is **append-only**: re-accepting a match produces a fresh
  row. LLM-based roll-up is a follow-up; the storage pipeline does
  not depend on the source of the summary.
* The deterministic summary keeps the slice self-contained — no
  external services, no new dependencies. The format is documented in
  :mod:`apply_pilot.features.writing_style_memory.summariser`.
"""

from __future__ import annotations

from apply_pilot.features.writing_style_memory.models import (
    StyleMemory,
    StyleMemoryEntry,
    StyleMemoryEntryModel,
)
from apply_pilot.features.writing_style_memory.repository import (
    DEFAULT_AGGREGATED_LIMIT,
    InMemoryStyleMemoryRepository,
    SqlStyleMemoryRepository,
    StyleMemoryRepository,
)
from apply_pilot.features.writing_style_memory.schemas import StyleMemoryRead
from apply_pilot.features.writing_style_memory.service import StyleMemoryService
from apply_pilot.features.writing_style_memory.summariser import summarise_letter

__all__ = [
    "DEFAULT_AGGREGATED_LIMIT",
    "InMemoryStyleMemoryRepository",
    "SqlStyleMemoryRepository",
    "StyleMemory",
    "StyleMemoryEntry",
    "StyleMemoryEntryModel",
    "StyleMemoryRead",
    "StyleMemoryRepository",
    "StyleMemoryService",
    "summarise_letter",
]
