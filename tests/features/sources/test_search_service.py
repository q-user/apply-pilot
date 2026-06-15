"""Tests for VacancySearchService — the cross-source search+ingest pipeline.

The service composes an ``HHVacancySearchClient`` with a ``SourceService``
(itself backed by a normaliser + repository + deduplicator). These tests
use the in-memory implementations on every boundary so the assertions
remain about the *service behaviour*, not any single collaborator.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from job_apply.features.hh.search import (
    HHQuery,
    HHVacancySearchClient,
    InMemoryHhVacancySearchClient,
)
from job_apply.features.sources.repository import InMemoryVacancyRepository
from job_apply.features.sources.search_service import (
    IngestResult,
    VacancySearchService,
)
from job_apply.features.sources.service import SourceService


def _hh_vacancy(vacancy_id: str, name: str = "Python developer") -> dict:
    """Build a minimal realistic hh.ru search-item payload."""
    return {
        "id": vacancy_id,
        "name": name,
        "employer": {"id": "1", "name": "Acme"},
        "salary": {"from": 200000, "to": 300000, "currency": "RUR", "gross": True},
        "area": {"id": "1", "name": "Москва"},
        "published_at": "2025-12-01T10:00:00+0300",
    }


# ---------------------------------------------------------------------------
# IngestResult
# ---------------------------------------------------------------------------


class TestIngestResult:
    def test_frozen_dataclass(self) -> None:
        """``IngestResult`` is immutable."""
        result = IngestResult(fetched=10, persisted=7, skipped=3)
        with pytest.raises((AttributeError, Exception)):
            result.fetched = 11  # type: ignore[misc]

    def test_counts_sum_to_fetched(self) -> None:
        """Persisted + skipped always equals fetched (within a single batch)."""
        result = IngestResult(fetched=10, persisted=7, skipped=3)
        assert result.persisted + result.skipped == result.fetched


# ---------------------------------------------------------------------------
# Service — composition
# ---------------------------------------------------------------------------


@pytest.fixture
def repo() -> InMemoryVacancyRepository:
    return InMemoryVacancyRepository()


@pytest.fixture
def source_service(repo: InMemoryVacancyRepository) -> SourceService:
    return SourceService(repo)


def _service(client: HHVacancySearchClient, source_service: SourceService) -> VacancySearchService:
    return VacancySearchService(client=client, source_service=source_service)


class TestServiceSearchAndIngest:
    def test_search_and_ingest(
        self, source_service: SourceService, repo: InMemoryVacancyRepository
    ) -> None:
        """First call persists all new vacancies; counts match the fetch."""
        items = [
            _hh_vacancy("1", "Python dev"),
            _hh_vacancy("2", "Go dev"),
            _hh_vacancy("3", "Rust dev"),
        ]
        client: HHVacancySearchClient = InMemoryHhVacancySearchClient(fixtures={"python": items})
        service = _service(client, source_service)

        result = asyncio_run(service.search_and_ingest(uuid.uuid4(), HHQuery(text="python")))

        assert isinstance(result, IngestResult)
        assert result.fetched == 3
        assert result.persisted == 3
        assert result.skipped == 0
        assert len(repo.list_by_source("hh")) == 3

    def test_skips_duplicates(
        self, source_service: SourceService, repo: InMemoryVacancyRepository
    ) -> None:
        """The same query a second time produces all duplicates."""
        items = [_hh_vacancy("1", "Python dev"), _hh_vacancy("2", "Go dev")]
        client: HHVacancySearchClient = InMemoryHhVacancySearchClient(fixtures={"python": items})
        service = _service(client, source_service)
        user_id = uuid.uuid4()

        first = asyncio_run(service.search_and_ingest(user_id, HHQuery(text="python")))
        second = asyncio_run(service.search_and_ingest(user_id, HHQuery(text="python")))

        assert first.fetched == 2 and first.persisted == 2 and first.skipped == 0
        assert second.fetched == 2 and second.persisted == 0 and second.skipped == 2
        # Repository still has only the two canonical rows.
        assert len(repo.list_by_source("hh")) == 2

    def test_empty_search(
        self, source_service: SourceService, repo: InMemoryVacancyRepository
    ) -> None:
        """A search that returns no items is a no-op, not an error."""
        client: HHVacancySearchClient = InMemoryHhVacancySearchClient(fixtures={})
        service = _service(client, source_service)

        result = asyncio_run(service.search_and_ingest(uuid.uuid4(), HHQuery(text="python")))

        assert result == IngestResult(fetched=0, persisted=0, skipped=0)
        assert len(repo.list_by_source("hh")) == 0


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def asyncio_run(coro):  # type: ignore[no-untyped-def]
    """Run a coroutine to completion from a sync test."""
    return asyncio.run(coro)
