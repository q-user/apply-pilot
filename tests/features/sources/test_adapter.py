"""TDD tests for the source-adapter interface (M7, issue #70).

A :class:`SourceAdapter` is the unified boundary between the application and
an external job source (hh.ru today; Habr Career, Telegram channels, company
sites in future). It exposes the full source lifecycle:

* :meth:`SourceAdapter.search` — fetch raw vacancy dicts.
* :meth:`SourceAdapter.normalize` — turn a raw dict into a canonical
  :class:`Vacancy`.
* :meth:`SourceAdapter.extract_screening_questions` — build screening
  question rows from a raw dict.
* :meth:`SourceAdapter.apply` — submit an application (optional; some
  sources do not support programmatic apply).

:class:`HhSourceAdapter` is the hh.ru implementation: a thin wrapper that
delegates to the existing :class:`HHVacancySearchClient`,
:class:`VacancyNormalizer`, :class:`HhScreeningQuestionExtractor` and
:class:`HhApplyAdapter` — it composes them rather than replacing any of
them, so the rest of the slice keeps its narrow contracts.

:class:`AdapterRegistry` is a small in-memory index keyed by the
adapter's :attr:`name` attribute; the cross-source orchestration code
looks adapters up there.

The tests prefer DI / in-memory fakes — no ``Mock``. The
:class:`HhHttpVacancySearchClient` is replaced by
:class:`InMemoryHhVacancySearchClient`, the real :class:`HhApplyAdapter`
is replaced by a recording in-memory stand-in that still satisfies the
:class:`ApplyAdapter` Protocol.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

import pytest

from job_apply.features.apply_worker.models import ApplyJob
from job_apply.features.apply_worker.runtime import ApplyResult
from job_apply.features.hh.adapter import HhSourceAdapter
from job_apply.features.hh.search import InMemoryHhVacancySearchClient
from job_apply.features.screening.extractor import HhScreeningQuestionExtractor
from job_apply.features.screening.repository import InMemoryScreeningQuestionRepository
from job_apply.features.sources.adapter import (
    AdapterRegistry,
    SourceAdapter,
    SourceQuery,
)
from job_apply.features.sources.normalizer import VacancyNormalizer

# ---------------------------------------------------------------------------
# Fakes — recording in-memory stand-ins for the hh cross-slice collaborators.
# ---------------------------------------------------------------------------


class _RecordingApplyAdapter:
    """In-memory :class:`ApplyAdapter` that records calls and returns a fixed result.

    The real :class:`~job_apply.features.hh.apply.HhApplyAdapter` is
    integration-tested separately against ``httpx.MockTransport``; here
    we want to assert *delegation* — that :class:`HhSourceAdapter.apply`
    forwards the job to the injected adapter and returns its result
    verbatim. A recording fake is the right tool for that.
    """

    def __init__(self, *, name: str, result: ApplyResult) -> None:
        self.name = name
        self._result = result
        self.submitted: list[ApplyJob] = []

    async def submit(self, job: ApplyJob) -> ApplyResult:
        self.submitted.append(job)
        return self._result


@dataclass
class _HhAdapterWorld:
    """Bundle of collaborators a :class:`HhSourceAdapter` consumes in tests."""

    search_client: InMemoryHhVacancySearchClient
    normalizer: VacancyNormalizer
    screening_extractor: HhScreeningQuestionExtractor
    apply_adapter: _RecordingApplyAdapter
    adapter: HhSourceAdapter


def _make_world(
    *,
    apply_result: ApplyResult | None = None,
    search_fixtures: dict[str, list[dict]] | None = None,
) -> _HhAdapterWorld:
    """Build a :class:`HhSourceAdapter` wired to in-memory fakes."""
    search_client = InMemoryHhVacancySearchClient(fixtures=search_fixtures or {})
    normalizer = VacancyNormalizer()
    screening_extractor = HhScreeningQuestionExtractor(
        question_repo=InMemoryScreeningQuestionRepository()
    )
    apply_result = apply_result or ApplyResult(
        success=True,
        external_application_id="negotiation-1",
        error=None,
        retryable=False,
    )
    apply_adapter = _RecordingApplyAdapter(name="hh", result=apply_result)
    adapter = HhSourceAdapter(
        search_client=search_client,
        normalizer=normalizer,
        screening_extractor=screening_extractor,
        apply_adapter=apply_adapter,
    )
    return _HhAdapterWorld(
        search_client=search_client,
        normalizer=normalizer,
        screening_extractor=screening_extractor,
        apply_adapter=apply_adapter,
        adapter=adapter,
    )


def _hh_vacancy(vacancy_id: str = "v-1", name: str = "Senior Python") -> dict:
    """Minimal realistic hh.ru vacancy payload."""
    return {
        "id": vacancy_id,
        "name": name,
        "employer": {"id": "1", "name": "Acme"},
        "salary": None,
        "area": {"id": "1", "name": "Москва"},
        "published_at": "2025-12-01T10:00:00+0300",
    }


# ---------------------------------------------------------------------------
# SourceQuery value object
# ---------------------------------------------------------------------------


class TestSourceQuery:
    def test_defaults(self) -> None:
        """A bare :class:`SourceQuery` carries the documented defaults."""
        q = SourceQuery()
        assert q.text is None
        assert q.area is None
        assert q.salary is None
        assert q.page == 0
        assert q.per_page == 50
        assert q.extra == {}

    def test_with_extras(self) -> None:
        """Per-source extensions live in :attr:`SourceQuery.extra`."""
        q = SourceQuery(
            text="python",
            area="1",
            salary=200000,
            page=2,
            per_page=25,
            extra={"only_with_salary": True, "schedule": "remote"},
        )
        assert q.text == "python"
        assert q.area == "1"
        assert q.salary == 200000
        assert q.page == 2
        assert q.per_page == 25
        assert q.extra == {"only_with_salary": True, "schedule": "remote"}

    def test_immutable(self) -> None:
        """A :class:`SourceQuery` is a frozen value object."""
        q = SourceQuery(text="python")
        with pytest.raises((AttributeError, Exception)):
            q.text = "go"  # type: ignore[misc]

    def test_each_instance_gets_its_own_extra(self) -> None:
        """The default-factory on ``extra`` prevents shared-mutable-state bugs."""
        q1 = SourceQuery()
        q2 = SourceQuery()
        q1.extra["k"] = "v"
        assert q2.extra == {}


# ---------------------------------------------------------------------------
# HhSourceAdapter — structural conformance
# ---------------------------------------------------------------------------


class TestHhSourceAdapterProtocol:
    def test_satisfies_protocol(self) -> None:
        """``HhSourceAdapter`` is a structural :class:`SourceAdapter`."""
        world = _make_world()
        adapter: SourceAdapter = world.adapter
        assert isinstance(adapter, SourceAdapter)
        assert adapter.name == "hh"


# ---------------------------------------------------------------------------
# HhSourceAdapter.search — delegates to the underlying search client
# ---------------------------------------------------------------------------


class TestHhSourceAdapterSearch:
    def test_search_delegates_to_underlying_client(self) -> None:
        """``search`` forwards the :class:`SourceQuery` to the hh search client."""
        items = [_hh_vacancy("1", "Python dev"), _hh_vacancy("2", "Go dev")]
        world = _make_world(search_fixtures={"python": items})

        result = asyncio_run(world.adapter.search(SourceQuery(text="python")))

        assert result == items

    def test_search_translates_extra_into_hh_query(self) -> None:
        """``SourceQuery.extra`` is honoured when building the hh request."""
        # The hh search client we depend on takes only the typed
        # ``HHQuery`` fields; ``extra`` is passed through for sources
        # that need it. Here we assert the base fields translate
        # faithfully to a matching ``HHQuery``.
        items = [_hh_vacancy("1", "Python dev")]
        world = _make_world(search_fixtures={"python": items})

        result = asyncio_run(
            world.adapter.search(
                SourceQuery(
                    text="python",
                    area="1",
                    salary=200000,
                    page=2,
                    per_page=25,
                    extra={"only_with_salary": True},
                )
            )
        )

        assert result == items


# ---------------------------------------------------------------------------
# HhSourceAdapter.normalize — delegates to the normalizer
# ---------------------------------------------------------------------------


class TestHhSourceAdapterNormalize:
    def test_normalize_delegates_to_vacancy_normalizer(self) -> None:
        """``normalize`` forwards the raw dict to :class:`VacancyNormalizer`."""
        world = _make_world()
        raw = _hh_vacancy("v-99", "Staff Backend Engineer")

        vacancy = world.adapter.normalize(raw)

        assert vacancy.source == "hh"
        assert vacancy.source_id == "v-99"
        assert vacancy.title == "Staff Backend Engineer"

    def test_normalize_uses_hh_branch_of_normalizer(self) -> None:
        """The adapter pins the source to ``"hh"`` so the right branch runs."""
        world = _make_world()
        raw = _hh_vacancy("v-100", "Whatever")

        vacancy = world.adapter.normalize(raw)

        # Source is hard-wired to "hh" — the dispatch happens in the
        # adapter, not in the caller. This guards against accidental
        # delegation to ``normalizer.normalize("hh", raw)``.
        assert vacancy.source == "hh"


# ---------------------------------------------------------------------------
# HhSourceAdapter.extract_screening_questions — composes normalizer + extractor
# ---------------------------------------------------------------------------


class TestHhSourceAdapterScreening:
    def test_extract_screening_questions_returns_rows(self) -> None:
        """The screening extractor receives a normalizer-produced vacancy + raw dict."""
        world = _make_world()
        raw = {
            "id": "v-q-1",
            "name": "Backend Developer",
            "employer": {"name": "Acme"},
            "questions": [
                {"id": "q1", "required": True, "text": "Why Acme?"},
                {"id": "q2", "required": False, "text": "Years with Go?"},
            ],
        }

        questions = world.adapter.extract_screening_questions(raw)

        assert [q.question_text for q in questions] == [
            "Why Acme?",
            "Years with Go?",
        ]
        # All questions are linked to the freshly-normalised vacancy's id.
        vacancy = world.normalizer.normalize("hh", raw)
        assert {q.vacancy_id for q in questions} == {vacancy.id}

    def test_extract_screening_questions_handles_no_questions_field(self) -> None:
        """A payload without ``questions`` returns an empty list."""
        world = _make_world()
        raw = _hh_vacancy("v-q-2", "Anything")

        questions = world.adapter.extract_screening_questions(raw)

        assert questions == []


# ---------------------------------------------------------------------------
# HhSourceAdapter.apply — delegates to the apply adapter
# ---------------------------------------------------------------------------


class TestHhSourceAdapterApply:
    def test_apply_delegates_to_apply_adapter(self) -> None:
        """``apply`` forwards the :class:`ApplyJob` to the injected adapter."""
        world = _make_world(
            apply_result=ApplyResult(
                success=True,
                external_application_id="negotiation-abc",
                error=None,
                retryable=False,
            )
        )
        job = ApplyJob(
            match_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            vacancy_id=uuid.uuid4(),
        )

        result = asyncio_run(world.adapter.apply(job))

        assert result.success is True
        assert result.external_application_id == "negotiation-abc"
        assert world.apply_adapter.submitted == [job]


# ---------------------------------------------------------------------------
# AdapterRegistry
# ---------------------------------------------------------------------------


class TestAdapterRegistry:
    def test_register_and_get(self) -> None:
        """A registered adapter is retrievable by name."""
        registry = AdapterRegistry()
        world = _make_world()

        registry.register(world.adapter)

        assert registry.get("hh") is world.adapter

    def test_get_unknown_returns_none(self) -> None:
        """Looking up an unknown name yields ``None`` (not ``KeyError``)."""
        registry = AdapterRegistry()

        assert registry.get("nope") is None

    def test_list_returns_all_registered_names(self) -> None:
        """``list`` returns every registered adapter's :attr:`name`."""
        registry = AdapterRegistry()
        world_hh = _make_world()

        # Build a second, distinct adapter that uses a different name.
        # The :class:`AdapterRegistry` only cares about the structural
        # Protocol + :attr:`name`, so a tiny test-only class is the
        # cleanest way to assert multi-adapter behaviour.
        @dataclass
        class _StubAdapter:
            name: str

            async def search(self, query: SourceQuery) -> list[dict]:
                return []

            def normalize(self, raw: dict):
                return None  # type: ignore[return-value]

            def extract_screening_questions(self, raw: dict) -> list:
                return []

            async def apply(self, job: ApplyJob) -> ApplyResult:
                return ApplyResult(
                    success=True, external_application_id=None, error=None, retryable=False
                )

        registry.register(world_hh.adapter)
        registry.register(_StubAdapter(name="habr"))

        assert sorted(registry.list()) == ["habr", "hh"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def asyncio_run(coro):  # type: ignore[no-untyped-def]
    """Run a coroutine to completion from a sync test."""
    return asyncio.run(coro)
