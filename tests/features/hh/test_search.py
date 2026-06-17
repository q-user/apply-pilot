"""Tests for the hh.ru vacancy search adapter (issue #22).

These tests cover the public surface of the ``features.hh.search`` module:

* :class:`HHQuery` value object validation.
* :class:`InMemoryHhVacancySearchClient` fake used in higher-level tests.
* :class:`HhHttpVacancySearchClient` request shape (via ``httpx.MockTransport``)
  — no real network calls.

The cross-source service tests live in
``tests/features/sources/test_search_service.py``.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from apply_pilot.features.hh.search import (
    HhHttpVacancySearchClient,
    HHQuery,
    InMemoryHhVacancySearchClient,
)


def asyncio_run(coro):  # type: ignore[no-untyped-def]
    """Run a coroutine to completion from a sync test."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# HHQuery value object
# ---------------------------------------------------------------------------


class TestHHQuery:
    def test_validates_params(self) -> None:
        """All business-rule violations raise ``ValueError``."""
        with pytest.raises(ValueError, match="page"):
            HHQuery(page=-1)
        with pytest.raises(ValueError, match="per_page"):
            HHQuery(per_page=0)
        with pytest.raises(ValueError, match="per_page"):
            HHQuery(per_page=101)  # hh.ru hard cap
        with pytest.raises(ValueError, match="salary"):
            HHQuery(salary=-100)

    def test_accepts_valid_values(self) -> None:
        """All valid inputs are accepted and stored as-is."""
        q = HHQuery(
            text="python developer",
            area="Москва",
            salary=200000,
            page=2,
            per_page=100,
        )
        assert q.text == "python developer"
        assert q.area == "Москва"
        assert q.salary == 200000
        assert q.page == 2
        assert q.per_page == 100

    def test_default_values(self) -> None:
        """Default page/per_page match hh.ru's conventional values."""
        q = HHQuery()
        assert q.text is None
        assert q.area is None
        assert q.salary is None
        assert q.page == 0
        assert q.per_page == 50

    def test_to_query_params_omits_none(self) -> None:
        """None-valued filters are not serialised into the URL."""
        q = HHQuery(text="python")
        params = q.to_query_params()
        assert params == {"text": "python", "page": 0, "per_page": 50}
        assert "area" not in params
        assert "salary" not in params

    def test_to_query_params_full(self) -> None:
        """All fields are serialised when set."""
        q = HHQuery(text="python", area="1", salary=200000, page=3, per_page=25)
        params = q.to_query_params()
        assert params == {
            "text": "python",
            "area": "1",
            "salary": 200000,
            "page": 3,
            "per_page": 25,
        }


# ---------------------------------------------------------------------------
# In-memory client
# ---------------------------------------------------------------------------


def _hh_vacancy(vacancy_id: str, name: str) -> dict:
    """Build a minimal realistic hh.ru search-item payload."""
    return {
        "id": vacancy_id,
        "name": name,
        "employer": {"id": "1", "name": "Acme"},
        "salary": None,
        "area": {"id": "1", "name": "Москва"},
        "published_at": "2025-12-01T10:00:00+0300",
    }


class TestInMemoryClient:
    def test_returns_fixtures(self) -> None:
        """When fixtures are pre-loaded, all of them are returned."""
        items = [_hh_vacancy("1", "Python dev"), _hh_vacancy("2", "Go dev")]
        client = InMemoryHhVacancySearchClient(fixtures={"python": items})

        result = asyncio_run(client.search(HHQuery(text="python")))

        assert result == items

    def test_filters_by_text(self) -> None:
        """Only the fixture list matching the query text is returned."""
        python_items = [_hh_vacancy("1", "Python dev")]
        go_items = [_hh_vacancy("2", "Go dev")]
        client = InMemoryHhVacancySearchClient(fixtures={"python": python_items, "go": go_items})

        python_result = asyncio_run(client.search(HHQuery(text="python")))
        go_result = asyncio_run(client.search(HHQuery(text="go")))

        assert python_result == python_items
        assert go_result == go_items

    def test_unknown_text_returns_empty(self) -> None:
        """An unknown query text returns an empty list, not an error."""
        client = InMemoryHhVacancySearchClient(
            fixtures={"python": [_hh_vacancy("1", "Python dev")]}
        )

        result = asyncio_run(client.search(HHQuery(text="rust")))

        assert result == []

    def test_fetch_one_returns_matching_fixture(self) -> None:
        """``fetch_one`` looks up by hh vacancy id across all fixtures."""
        items = [_hh_vacancy("123", "Python dev")]
        client = InMemoryHhVacancySearchClient(fixtures={"python": items})

        result = asyncio_run(client.fetch_one("123"))

        assert result["id"] == "123"


# ---------------------------------------------------------------------------
# HhHttpVacancySearchClient — request shape via MockTransport
# ---------------------------------------------------------------------------


def _empty_hh_response() -> dict:
    """A minimal valid hh.ru search response with no items."""
    return {"items": [], "found": 0, "pages": 0, "page": 0, "per_page": 50}


class TestHhHttpClientRequestShape:
    def test_real_client_serializes_query(self) -> None:
        """The HTTP client serialises HHQuery into the right query string."""
        captured_url: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured_url.append(str(request.url))
            return httpx.Response(200, json=_empty_hh_response())

        async def runner() -> list[dict]:
            transport = httpx.MockTransport(handler)
            async with httpx.AsyncClient(transport=transport) as http_client:
                client = HhHttpVacancySearchClient(
                    client=http_client,
                    base_url="https://api.hh.ru/vacancies",
                )
                query = HHQuery(text="python", area="Москва", salary=200000, page=2, per_page=20)
                return await client.search(query)

        items = asyncio_run(runner())

        assert items == []
        assert len(captured_url) == 1
        url = captured_url[0]
        # The base URL is preserved.
        assert url.startswith("https://api.hh.ru/vacancies")
        # The query params are serialised in some order; check each one.
        for needle in (
            "text=python",
            "area=%D0%9C%D0%BE%D1%81%D0%BA%D0%B2%D0%B0",  # url-encoded "Москва"
            "salary=200000",
            "page=2",
            "per_page=20",
        ):
            assert needle in url, f"Expected {needle!r} in {url!r}"

    def test_real_client_sends_user_agent(self) -> None:
        """hh.ru requires a User-Agent; we send ``ApplyPilot/0.1``."""
        captured_headers: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured_headers.append(dict(request.headers))
            return httpx.Response(200, json=_empty_hh_response())

        async def runner() -> None:
            transport = httpx.MockTransport(handler)
            async with httpx.AsyncClient(transport=transport) as http_client:
                client = HhHttpVacancySearchClient(
                    client=http_client,
                    base_url="https://api.hh.ru/vacancies",
                )
                await client.search(HHQuery(text="python"))

        asyncio_run(runner())

        assert len(captured_headers) == 1
        assert captured_headers[0]["user-agent"] == "ApplyPilot/0.1"
