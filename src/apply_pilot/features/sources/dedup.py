# ruff: noqa: SIM114
"""Vacancy deduplication logic (issue #24).

Two layers of dedup run before any write hits the database:

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

from apply_pilot.features.sources.models import Vacancy
from apply_pilot.features.sources.repository import VacancyRepository

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
        if self._repo.find_by_source(vacancy.source, vacancy.source_id):
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
        """Split a batch into ``(new, duplicates)`` (Fix #260).

        Single round-trip via ``VacancyRepository.find_existing_in_batch``
        replaces the prior per-vacancy N+1 loop. ``content_hash`` is still
        consulted per-vacancy because the in-memory fake has no batch
        index on hash.
        """
        if not vacancies:
            return [], []
        new: list[Vacancy] = []
        duplicates: list[Vacancy] = []
        source_ids = [v.source_id for v in vacancies if v.source_id]
        existing_source_ids: set[str] = (
            self._repo.find_existing_in_batch(source_ids) if source_ids else set()
        )
        seen_source_ids: set[tuple[str, str]] = set()
        seen_hashes: set[str] = set()

        for vacancy in vacancies:
            source_key = (vacancy.source, vacancy.source_id) if vacancy.source_id else None
            content_hash = vacancy.content_hash
            is_dup = False
            if source_key and source_key in seen_source_ids:  # noqa: SIM114
                is_dup = True
            elif vacancy.source_id and vacancy.source_id in existing_source_ids:
                is_dup = True
            elif content_hash is not None and content_hash in seen_hashes:
                is_dup = True
            elif content_hash is not None and self._repo.find_by_content_hash(content_hash):
                is_dup = True
            if is_dup:
                duplicates.append(vacancy)
            else:
                new.append(vacancy)
                if source_key:
                    seen_source_ids.add(source_key)
                if content_hash is not None:
                    seen_hashes.add(content_hash)
        return new, duplicates
