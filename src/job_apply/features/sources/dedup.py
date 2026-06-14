"""Vacancy deduplication logic (issue #24).

Two levels of dedup run before any write hits the database:

1. **Source identity** — ``(source, source_id)`` composite key. The database
   also enforces this with a unique constraint, but catching it here avoids
   wasted writes and noisy constraint-violation logs.
2. **Content hash** — SHA-256 over the normalised title, description, and
   employer name. This detects the same vacancy scraped from two different
   sources (e.g. ``hh`` and ``habr``).

The deduplicator depends on :class:`VacancyRepository` via structural
typing, so it works equally well with the in-memory fake and the
SQLAlchemy-backed production implementation.
"""

from __future__ import annotations

import logging

from job_apply.features.sources.models import Vacancy
from job_apply.features.sources.repository import VacancyRepository

logger = logging.getLogger(__name__)


class VacancyDeduplicator:
    """Detect duplicate vacancies before they hit the database.

    The deduplicator is intentionally async: even though the current
    repository implementations are synchronous, the async signature makes
    it easy to swap in an async repository (e.g. ``sqlalchemy[asyncio]``)
    without changing the public surface.
    """

    def __init__(self, vacancy_repo: VacancyRepository) -> None:
        self._repo = vacancy_repo

    async def is_duplicate(self, vacancy: Vacancy) -> bool:
        """Return ``True`` if ``vacancy`` is already known.

        A vacancy is a duplicate if either of these matches an existing row:

        * ``(vacancy.source, vacancy.source_id)`` — primary identity.
        * ``vacancy.content_hash`` (when not ``None``) — cross-source identity.
        """
        if self._repo.find_by_source(vacancy.source, vacancy.source_id) is not None:
            return True
        if vacancy.content_hash is not None:
            return bool(self._repo.find_by_content_hash(vacancy.content_hash))
        return False

    async def find_duplicates(self, vacancy: Vacancy) -> list[Vacancy]:
        """Return existing vacancies sharing ``vacancy``'s content hash.

        Empty input (``content_hash is None``) is treated as "no matches":
        the SQLAlchemy column is nullable and the in-memory fake would scan
        the whole table for ``None``-hash rows, which is rarely what we
        want.
        """
        if vacancy.content_hash is None:
            return []
        return self._repo.find_by_content_hash(vacancy.content_hash)

    async def deduplicate_batch(
        self, vacancies: list[Vacancy]
    ) -> tuple[list[Vacancy], list[Vacancy]]:
        """Split a batch into ``(new, duplicates)``.

        Duplicates are vacancies that match another vacancy in the batch
        **or** the repository by either ``(source, source_id)`` or
        ``content_hash``. ``content_hash`` is only consulted when set;
        ``None`` falls back to source-identity checks only.
        """
        new: list[Vacancy] = []
        duplicates: list[Vacancy] = []
        seen_source_ids: set[tuple[str, str]] = set()
        seen_hashes: set[str] = set()

        for vacancy in vacancies:
            key = (vacancy.source, vacancy.source_id)

            # In-batch dedup by (source, source_id) — also catches repo matches.
            if key in seen_source_ids or self._repo.find_by_source(*key) is not None:
                duplicates.append(vacancy)
                continue

            # In-batch / repo dedup by content_hash (cross-source).
            if vacancy.content_hash is not None and (
                vacancy.content_hash in seen_hashes
                or bool(self._repo.find_by_content_hash(vacancy.content_hash))
            ):
                duplicates.append(vacancy)
                continue

            new.append(vacancy)
            seen_source_ids.add(key)
            if vacancy.content_hash is not None:
                seen_hashes.add(vacancy.content_hash)

        return new, duplicates


__all__ = ["VacancyDeduplicator"]
