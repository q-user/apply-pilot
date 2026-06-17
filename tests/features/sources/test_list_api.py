"""HTTP integration tests for the ``GET /vacancies`` list endpoint.

The route handler is wired against an in-memory vacancy repository via
FastAPI's ``dependency_overrides`` so the tests exercise the full request
lifecycle (param parsing, validation, service call, response serialisation)
without touching the real database.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from job_apply.features.sources.api import (
    get_vacancy_list_service,
)
from job_apply.features.sources.api import (
    router as sources_router,
)
from job_apply.features.sources.models import Vacancy
from job_apply.features.sources.repository import InMemoryVacancyRepository
from job_apply.features.sources.service import SourceService

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _vacancy(
    *,
    source: str = "hh",
    source_id: str | None = None,
    title: str = "Python Dev",
    salary_from: int | None = 100000,
    salary_to: int | None = 200000,
    location: str | None = "Moscow",
) -> Vacancy:
    """Build a ``Vacancy`` populated with sensible defaults.

    Mirrors what the real normaliser produces: ``salary_gross`` is
    always set to ``False`` and the currency defaults to ``RUR``.
    The in-memory repository stamps ``created_at`` on insert, so tests
    that need a deterministic timestamp must override it on the row
    returned by :func:`_stored_vacancy`.
    """
    return Vacancy(
        source=source,
        source_id=source_id or f"{source}-{uuid.uuid4()}",
        title=title,
        location=location,
        salary_from=salary_from,
        salary_to=salary_to,
        salary_currency="RUR",
        salary_gross=False,
        raw_data={"id": source_id or "raw", "name": title},
    )


def _stored_vacancy(
    repo: InMemoryVacancyRepository,
    *,
    source: str = "hh",
    source_id: str | None = None,
    title: str = "Python Dev",
    salary_from: int | None = 100000,
    salary_to: int | None = 200000,
    location: str | None = "Moscow",
    created_at: datetime | None = None,
) -> Vacancy:
    """Build and persist a ``Vacancy`` with an explicit ``created_at``.

    The in-memory :class:`~job_apply.features.sources.repository.InMemoryVacancyRepository`
    stamps ``created_at = now`` on insert, so any test that needs a
    deterministic timestamp must override it on the row that the
    repository actually returned. Tests that don't care about
    ``created_at`` can keep using the plain constructor.
    """
    vacancy = repo.upsert(
        _vacancy(
            source=source,
            source_id=source_id,
            title=title,
            salary_from=salary_from,
            salary_to=salary_to,
            location=location,
        )
    )
    if created_at is not None:
        vacancy.created_at = created_at
    return vacancy


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def repo() -> InMemoryVacancyRepository:
    return InMemoryVacancyRepository()


@pytest.fixture
def app(repo: InMemoryVacancyRepository) -> Iterator[FastAPI]:
    """Build a FastAPI app with the sources router wired to the in-memory repo.

    The service is built once per test, sharing the same in-memory repository
    that the test populates. This keeps the dependency graph stable: tests
    only override :func:`get_vacancy_list_service` to swap the persistence
    layer, not the service contract.
    """
    application = FastAPI()
    application.include_router(sources_router)
    service = SourceService(repository=repo)
    application.dependency_overrides[get_vacancy_list_service] = lambda: service
    try:
        yield application
    finally:
        application.dependency_overrides.clear()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# GET /vacancies — basic shape
# ---------------------------------------------------------------------------


class TestListVacancies:
    def test_list_returns_paginated_vacancies(
        self, client: TestClient, repo: InMemoryVacancyRepository
    ) -> None:
        """The endpoint must return items, total, limit, offset in the expected shape."""
        for i in range(3):
            repo.upsert(_vacancy(source_id=f"hh-{i}", title=f"V{i}"))

        response = client.get("/vacancies")

        assert response.status_code == 200
        body = response.json()
        assert set(body.keys()) >= {"items", "total", "limit", "offset"}
        assert body["total"] == 3
        assert body["limit"] == 20
        assert body["offset"] == 0
        assert len(body["items"]) == 3
        first = body["items"][0]
        assert {"id", "source", "title", "location", "salary_from"} <= set(first.keys())

    def test_list_filters_by_source(
        self, client: TestClient, repo: InMemoryVacancyRepository
    ) -> None:
        """The ``source`` query param must filter the results to that source."""
        repo.upsert(_vacancy(source="hh", source_id="hh-1", title="HH Dev"))
        repo.upsert(_vacancy(source="hh", source_id="hh-2", title="HH Lead"))
        repo.upsert(_vacancy(source="habr", source_id="habr-1", title="Habr Dev"))
        repo.upsert(_vacancy(source="telegram", source_id="tg-1", title="TG Dev"))

        response = client.get("/vacancies", params={"source": "habr"})

        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 1
        assert len(body["items"]) == 1
        assert body["items"][0]["source"] == "habr"
        assert body["items"][0]["title"] == "Habr Dev"

    def test_list_filters_by_salary_min(
        self, client: TestClient, repo: InMemoryVacancyRepository
    ) -> None:
        """``salary_min`` must keep only vacancies whose ``salary_from >= salary_min``."""
        repo.upsert(_vacancy(source_id="low", title="Low", salary_from=50_000))
        repo.upsert(_vacancy(source_id="mid", title="Mid", salary_from=150_000))
        repo.upsert(_vacancy(source_id="high", title="High", salary_from=350_000))

        response = client.get("/vacancies", params={"salary_min": 100_000})

        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 2
        titles = {item["title"] for item in body["items"]}
        assert titles == {"Mid", "High"}

    def test_list_filters_by_location(
        self, client: TestClient, repo: InMemoryVacancyRepository
    ) -> None:
        """``location`` must do a case-insensitive substring match."""
        repo.upsert(_vacancy(source_id="mow-1", title="Mow 1", location="Moscow"))
        repo.upsert(_vacancy(source_id="mow-2", title="Mow 2", location="moscow remote"))
        repo.upsert(_vacancy(source_id="spb", title="SPb", location="Saint Petersburg"))
        repo.upsert(_vacancy(source_id="none", title="None", location=None))

        response = client.get("/vacancies", params={"location": "Moscow"})

        assert response.status_code == 200
        body = response.json()
        # Both "Moscow" and "moscow remote" should match case-insensitively.
        assert body["total"] == 2
        titles = {item["title"] for item in body["items"]}
        assert titles == {"Mow 1", "Mow 2"}

    def test_list_filters_by_since(
        self, client: TestClient, repo: InMemoryVacancyRepository
    ) -> None:
        """``since`` (ISO datetime) must return only vacancies created after it."""
        old = datetime(2024, 1, 1, tzinfo=UTC)
        mid = datetime(2024, 6, 1, tzinfo=UTC)
        new = datetime(2025, 1, 1, tzinfo=UTC)
        _stored_vacancy(repo, source_id="old", title="Old", created_at=old)
        _stored_vacancy(repo, source_id="mid", title="Mid", created_at=mid)
        _stored_vacancy(repo, source_id="new", title="New", created_at=new)

        since = (datetime(2024, 5, 1, tzinfo=UTC)).isoformat()
        response = client.get("/vacancies", params={"since": since})

        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 2
        titles = {item["title"] for item in body["items"]}
        assert titles == {"Mid", "New"}

    def test_list_combines_filters(
        self, client: TestClient, repo: InMemoryVacancyRepository
    ) -> None:
        """All filters must combine as a logical AND."""
        # The one vacancy that passes every filter.
        _stored_vacancy(
            repo,
            source="hh",
            source_id="hh-1",
            title="Match",
            salary_from=200_000,
            location="Moscow",
        )
        # Filtered out by the source dimension.
        _stored_vacancy(
            repo,
            source="habr",
            source_id="habr-1",
            title="Wrong source",
            salary_from=200_000,
            location="Moscow",
        )
        # Filtered out by the salary floor.
        _stored_vacancy(
            repo,
            source="hh",
            source_id="hh-2",
            title="Wrong salary",
            salary_from=50_000,
            location="Moscow",
        )
        # Filtered out by the location substring.
        _stored_vacancy(
            repo,
            source="hh",
            source_id="hh-3",
            title="Wrong location",
            salary_from=200_000,
            location="Saint Petersburg",
        )

        since = (datetime(2024, 1, 1, tzinfo=UTC) - timedelta(days=1)).isoformat()
        response = client.get(
            "/vacancies",
            params={
                "source": "hh",
                "salary_min": 100_000,
                "location": "Moscow",
                "since": since,
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 1
        assert body["items"][0]["title"] == "Match"

    def test_list_respects_limit_and_offset(
        self, client: TestClient, repo: InMemoryVacancyRepository
    ) -> None:
        """``limit`` and ``offset`` must slice the result, and ``total`` stays full count."""
        for i in range(5):
            # Spread created_at so the ordering is deterministic.
            ts = datetime(2024, 1, 1, tzinfo=UTC) + timedelta(minutes=i)
            _stored_vacancy(repo, source_id=f"hh-{i}", title=f"V{i}", created_at=ts)

        # limit=2, offset=1 → middle 2 items; total still 5.
        response = client.get("/vacancies", params={"limit": 2, "offset": 1})

        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 5
        assert body["limit"] == 2
        assert body["offset"] == 1
        assert len(body["items"]) == 2
        # Ordered by created_at desc → V3, V2 (newest first, skip V4 at offset 0).
        assert body["items"][0]["title"] == "V3"
        assert body["items"][1]["title"] == "V2"

        # offset=4, limit=10 → just the oldest item.
        response = client.get("/vacancies", params={"limit": 10, "offset": 4})
        body = response.json()
        assert body["total"] == 5
        assert body["offset"] == 4
        assert len(body["items"]) == 1
        assert body["items"][0]["title"] == "V0"

    def test_list_returns_empty_for_no_matches(
        self, client: TestClient, repo: InMemoryVacancyRepository
    ) -> None:
        """No matching vacancies must produce an empty list with ``total=0``."""
        repo.upsert(_vacancy(source="hh", source_id="hh-1", title="A"))

        response = client.get("/vacancies", params={"source": "habr"})

        assert response.status_code == 200
        body = response.json()
        assert body == {"items": [], "total": 0, "limit": 20, "offset": 0}
