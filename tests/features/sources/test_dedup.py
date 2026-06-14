"""Tests for vacancy deduplication logic (issue #24).

Two levels of dedup are exercised:

* **Source identity** — ``(source, source_id)`` composite key.  Mirrors the
  database's unique constraint and lets us short-circuit before any write.
* **Content hash** — SHA-256 over ``title + description + employer_name``;
  lets us catch the same vacancy scraped from two different sources.

The tests use the in-memory repository fake injected through the
``VacancyRepository`` Protocol (no ``Mock``), so the assertions are made
against real state.
"""

from __future__ import annotations

import pytest

from job_apply.features.sources.dedup import VacancyDeduplicator
from job_apply.features.sources.models import Vacancy
from job_apply.features.sources.repository import InMemoryVacancyRepository
from job_apply.features.sources.service import SourceService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def repo() -> InMemoryVacancyRepository:
    return InMemoryVacancyRepository()


@pytest.fixture
def deduplicator(repo: InMemoryVacancyRepository) -> VacancyDeduplicator:
    return VacancyDeduplicator(repo)


def _make(
    source: str = "hh",
    source_id: str = "v-001",
    title: str = "Python Developer",
    description: str = "FastAPI + Postgres",
    employer: str = "Acme Inc",
    content_hash: str | None = None,
) -> Vacancy:
    """Build a Vacancy with sensible defaults for tests.

    ``content_hash`` is explicit because the normaliser usually sets it; in
    tests we sometimes want a stable value to assert cross-source behaviour.
    """
    return Vacancy(
        source=source,
        source_id=source_id,
        title=title,
        description=description,
        employer_name=employer,
        content_hash=content_hash,
    )


# ---------------------------------------------------------------------------
# is_duplicate
# ---------------------------------------------------------------------------


class TestIsDuplicate:
    @pytest.mark.asyncio
    async def test_new_vacancy_is_not_duplicate(self, deduplicator: VacancyDeduplicator) -> None:
        result = await deduplicator.is_duplicate(_make())
        assert result is False

    @pytest.mark.asyncio
    async def test_same_source_id_is_duplicate(
        self, repo: InMemoryVacancyRepository, deduplicator: VacancyDeduplicator
    ) -> None:
        # Pre-seed the repo with a known vacancy.
        repo.upsert(_make(source="hh", source_id="v-001", content_hash="hash-a"))

        # New instance with same identity but different content.
        result = await deduplicator.is_duplicate(
            _make(source="hh", source_id="v-001", content_hash="hash-b")
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_cross_source_same_content_hash_is_duplicate(
        self, repo: InMemoryVacancyRepository, deduplicator: VacancyDeduplicator
    ) -> None:
        # Pre-seed with an hh vacancy carrying a known content hash.
        repo.upsert(_make(source="hh", source_id="v-001", content_hash="same-hash"))

        # Different source, different source_id, but identical content_hash.
        result = await deduplicator.is_duplicate(
            _make(source="habr", source_id="v-999", content_hash="same-hash")
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_different_source_and_different_hash_is_not_duplicate(
        self, repo: InMemoryVacancyRepository, deduplicator: VacancyDeduplicator
    ) -> None:
        repo.upsert(_make(source="hh", source_id="v-001", content_hash="hash-a"))

        result = await deduplicator.is_duplicate(
            _make(source="habr", source_id="v-002", content_hash="hash-b")
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_content_hash_none_falls_back_to_source_identity(
        self, repo: InMemoryVacancyRepository, deduplicator: VacancyDeduplicator
    ) -> None:
        # Vacancy with no content_hash already in the repo.
        repo.upsert(_make(source="hh", source_id="v-001", content_hash=None))

        # Same identity, also no content_hash → still a duplicate.
        result = await deduplicator.is_duplicate(
            _make(source="hh", source_id="v-001", content_hash=None)
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_empty_repo_returns_false(self, deduplicator: VacancyDeduplicator) -> None:
        result = await deduplicator.is_duplicate(_make(content_hash="hash-a"))
        assert result is False


# ---------------------------------------------------------------------------
# find_duplicates
# ---------------------------------------------------------------------------


class TestFindDuplicates:
    @pytest.mark.asyncio
    async def test_returns_matching_by_content_hash(
        self, repo: InMemoryVacancyRepository, deduplicator: VacancyDeduplicator
    ) -> None:
        repo.upsert(_make(source="hh", source_id="v-001", content_hash="dup"))
        repo.upsert(_make(source="habr", source_id="v-002", content_hash="dup"))
        repo.upsert(_make(source="hh", source_id="v-003", content_hash="other"))

        result = await deduplicator.find_duplicates(
            _make(source="hh", source_id="v-004", content_hash="dup")
        )

        ids = {v.source_id for v in result}
        assert ids == {"v-001", "v-002"}

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_match(
        self, repo: InMemoryVacancyRepository, deduplicator: VacancyDeduplicator
    ) -> None:
        repo.upsert(_make(source="hh", source_id="v-001", content_hash="hash-a"))

        result = await deduplicator.find_duplicates(
            _make(source="hh", source_id="v-002", content_hash="hash-b")
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_none_content_hash_returns_empty(
        self, repo: InMemoryVacancyRepository, deduplicator: VacancyDeduplicator
    ) -> None:
        repo.upsert(_make(source="hh", source_id="v-001", content_hash="hash-a"))
        result = await deduplicator.find_duplicates(_make(content_hash=None))
        assert result == []


# ---------------------------------------------------------------------------
# deduplicate_batch
# ---------------------------------------------------------------------------


class TestDeduplicateBatch:
    @pytest.mark.asyncio
    async def test_splits_new_and_duplicates(
        self, repo: InMemoryVacancyRepository, deduplicator: VacancyDeduplicator
    ) -> None:
        repo.upsert(_make(source="hh", source_id="v-001", content_hash="dup"))

        batch = [
            _make(source="hh", source_id="v-001", content_hash="dup"),  # dup
            _make(source="hh", source_id="v-002", content_hash="new"),  # new
        ]
        new, duplicates = await deduplicator.deduplicate_batch(batch)

        assert len(new) == 1
        assert new[0].source_id == "v-002"
        assert len(duplicates) == 1
        assert duplicates[0].source_id == "v-001"

    @pytest.mark.asyncio
    async def test_cross_source_duplicate_detected_in_batch(
        self, repo: InMemoryVacancyRepository, deduplicator: VacancyDeduplicator
    ) -> None:
        repo.upsert(_make(source="hh", source_id="v-001", content_hash="same"))

        batch = [
            _make(source="habr", source_id="v-999", content_hash="same"),  # cross-source dup
        ]
        new, duplicates = await deduplicator.deduplicate_batch(batch)

        assert new == []
        assert len(duplicates) == 1

    @pytest.mark.asyncio
    async def test_in_batch_duplicates_are_deduplicated(
        self, deduplicator: VacancyDeduplicator
    ) -> None:
        # Two vacancies in the same batch with identical identity.
        batch = [
            _make(source="hh", source_id="v-001", content_hash="h"),
            _make(source="hh", source_id="v-001", content_hash="h"),
        ]
        new, duplicates = await deduplicator.deduplicate_batch(batch)

        assert len(new) == 1
        assert len(duplicates) == 1

    @pytest.mark.asyncio
    async def test_in_batch_cross_source_duplicates_deduplicated(
        self, deduplicator: VacancyDeduplicator
    ) -> None:
        # Two vacancies in the same batch, different sources, same content_hash.
        batch = [
            _make(source="hh", source_id="v-001", content_hash="h"),
            _make(source="habr", source_id="v-002", content_hash="h"),
        ]
        new, duplicates = await deduplicator.deduplicate_batch(batch)

        assert len(new) == 1
        assert len(duplicates) == 1
        # The one with content_hash='h' is the dup of the one before it in the batch.
        assert duplicates[0].source == "habr"

    @pytest.mark.asyncio
    async def test_empty_batch_returns_empty(self, deduplicator: VacancyDeduplicator) -> None:
        new, duplicates = await deduplicator.deduplicate_batch([])
        assert new == []
        assert duplicates == []

    @pytest.mark.asyncio
    async def test_all_new_when_repo_empty(self, deduplicator: VacancyDeduplicator) -> None:
        batch = [
            _make(source="hh", source_id="v-001", content_hash="a"),
            _make(source="habr", source_id="v-002", content_hash="b"),
            _make(source="tg", source_id="v-003", content_hash="c"),
        ]
        new, duplicates = await deduplicator.deduplicate_batch(batch)
        assert len(new) == 3
        assert duplicates == []

    @pytest.mark.asyncio
    async def test_all_duplicates_when_repo_matches_all(
        self, repo: InMemoryVacancyRepository, deduplicator: VacancyDeduplicator
    ) -> None:
        repo.upsert(_make(source="hh", source_id="v-001", content_hash="a"))
        repo.upsert(_make(source="habr", source_id="v-002", content_hash="b"))

        batch = [
            _make(source="hh", source_id="v-001", content_hash="a"),
            _make(source="habr", source_id="v-002", content_hash="b"),
        ]
        new, duplicates = await deduplicator.deduplicate_batch(batch)
        assert new == []
        assert len(duplicates) == 2


# ---------------------------------------------------------------------------
# SourceService.ingest_batch integration
# ---------------------------------------------------------------------------


class TestSourceServiceIngestBatch:
    @pytest.mark.asyncio
    async def test_ingest_batch_persists_new_and_skips_duplicates(
        self, repo: InMemoryVacancyRepository
    ) -> None:
        # Pre-seed the repo with a known row.
        repo.upsert(_make(source="hh", source_id="v-001", content_hash="dup"))

        service = SourceService(repo)
        batch = [
            _make(source="hh", source_id="v-001", content_hash="dup"),  # duplicate
            _make(source="hh", source_id="v-002", content_hash="new-1"),  # new
            _make(source="habr", source_id="v-003", content_hash="new-2"),  # new
        ]

        new, duplicates = await service.ingest_batch(batch)

        assert len(new) == 2
        assert {v.source_id for v in new} == {"v-002", "v-003"}
        assert len(duplicates) == 1
        assert duplicates[0].source_id == "v-001"

        # The two new rows made it into the repo, the existing one is untouched.
        assert len(list(repo.list_by_source("hh"))) == 2
        assert len(list(repo.list_by_source("habr"))) == 1

    @pytest.mark.asyncio
    async def test_ingest_batch_logs_skip_count(
        self, repo: InMemoryVacancyRepository, caplog: pytest.LogCaptureFixture
    ) -> None:
        repo.upsert(_make(source="hh", source_id="v-001", content_hash="dup"))

        service = SourceService(repo)
        batch = [
            _make(source="hh", source_id="v-001", content_hash="dup"),
            _make(source="hh", source_id="v-002", content_hash="new"),
        ]

        with caplog.at_level("INFO", logger="job_apply.features.sources.service"):
            await service.ingest_batch(batch)

        assert any("persisted=1 skipped=1" in record.message for record in caplog.records)

    @pytest.mark.asyncio
    async def test_ingest_vacancy_deduped_returns_none_for_duplicate(
        self, repo: InMemoryVacancyRepository
    ) -> None:
        repo.upsert(_make(source="hh", source_id="v-001", content_hash="dup"))

        service = SourceService(repo)
        # Same identity, but constructed with a *different* content_hash to make
        # sure the (source, source_id) branch is what's catching it.
        result = await service.ingest_vacancy_deduped(
            "hh",
            {
                "id": "v-001",
                "name": "Anything",
                "description": "irrelevant",
                "employer": {"name": "Whoever"},
            },
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_ingest_vacancy_deduped_returns_vacancy_when_new(
        self, repo: InMemoryVacancyRepository
    ) -> None:
        service = SourceService(repo)
        result = await service.ingest_vacancy_deduped(
            "hh",
            {
                "id": "v-new",
                "name": "New Role",
                "description": "Fresh",
                "employer": {"name": "Acme"},
            },
        )

        assert result is not None
        assert result.source_id == "v-new"
        assert result.id is not None

    @pytest.mark.asyncio
    async def test_ingest_batch_empty_returns_empty(self, repo: InMemoryVacancyRepository) -> None:
        service = SourceService(repo)
        new, duplicates = await service.ingest_batch([])
        assert new == []
        assert duplicates == []
