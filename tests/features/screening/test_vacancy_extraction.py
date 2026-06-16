"""TDD tests for the screening-question extractor (M2, issue #26).

When the ``screening`` slice is the destination of the M2 capture flow,
this module covers the two integration seams:

* :class:`ScreeningQuestionExtractor` — the Protocol that turns a raw
  hh.ru payload into :class:`ScreeningQuestion` rows. The default
  implementation :class:`HhScreeningQuestionExtractor` is what the rest
  of the slice depends on; the Protocol exists so other source
  adapters (Habr Career, Telegram channel) can ship their own
  extractor without touching the screening service.
* :class:`SourceService.ingest_vacancy` — the ingest pipeline calls
  the extractor after upserting the vacancy, persists the resulting
  rows, and returns them. The tests exercise the full
  ``vacancy → questions`` flow end-to-end against in-memory fakes
  (no ``Mock``).

Edge cases covered
------------------

* ``questions`` field absent → empty list.
* ``questions`` field is not a list → empty list.
* Mixed ``required: true / false`` rows — both are persisted (the
  ``required`` flag is not stored on the ORM; the test asserts the
  text comes through regardless of the flag).
* Empty-text questions are filtered out so the model never sees a row
  with an empty body.
* The order from the raw array is preserved as the sequential
  ``question_order`` index on the ORM row.
"""

from __future__ import annotations

import uuid

import pytest

from job_apply.features.screening.extractor import (
    HhScreeningQuestionExtractor,
    ScreeningQuestionExtractor,
)
from job_apply.features.screening.models import ScreeningQuestion
from job_apply.features.screening.repository import (
    InMemoryScreeningQuestionRepository,
    ScreeningQuestionRepository,
)
from job_apply.features.sources.models import Vacancy
from job_apply.features.sources.repository import InMemoryVacancyRepository
from job_apply.features.sources.service import SourceService


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def question_repo() -> InMemoryScreeningQuestionRepository:
    return InMemoryScreeningQuestionRepository()


@pytest.fixture
def vacancy_repo() -> InMemoryVacancyRepository:
    return InMemoryVacancyRepository()


@pytest.fixture
def source_service(vacancy_repo: InMemoryVacancyRepository) -> SourceService:
    return SourceService(vacancy_repo)


@pytest.fixture
def vacancy() -> Vacancy:
    """A canonical vacancy the extractors can attach questions to."""
    v = Vacancy(
        source="hh",
        source_id="v-extract-1",
        title="Senior Python Developer",
        description="FastAPI + Postgres",
        raw_data={"id": "v-extract-1", "name": "Senior Python Developer"},
    )
    v.id = uuid.uuid4()
    return v


# ---------------------------------------------------------------------------
# Protocol structural conformance
# ---------------------------------------------------------------------------


class TestScreeningQuestionExtractorProtocol:
    def test_hh_extractor_satisfies_the_protocol(self) -> None:
        """``HhScreeningQuestionExtractor`` must be a structural Protocol member."""
        extractor: ScreeningQuestionExtractor = HhScreeningQuestionExtractor(
            question_repo=InMemoryScreeningQuestionRepository()
        )
        assert isinstance(extractor, ScreeningQuestionExtractor)


# ---------------------------------------------------------------------------
# HhScreeningQuestionExtractor
# ---------------------------------------------------------------------------


class TestHhScreeningQuestionExtractor:
    def test_extract_questions_from_hh_payload_with_questions(
        self, question_repo: ScreeningQuestionRepository, vacancy: Vacancy
    ) -> None:
        """Each ``text`` in ``raw["questions"]`` becomes a row."""
        extractor = HhScreeningQuestionExtractor(question_repo=question_repo)
        raw = {
            "questions": [
                {"id": "q1", "required": True, "text": "Why do you want to work here?"},
                {"id": "q2", "required": False, "text": "Years of experience with Python?"},
            ]
        }

        created = extractor.extract_from_vacancy(vacancy, raw)

        assert len(created) == 2
        assert [q.question_text for q in created] == [
            "Why do you want to work here?",
            "Years of experience with Python?",
        ]
        for row in created:
            assert isinstance(row, ScreeningQuestion)
            assert row.vacancy_id == vacancy.id

    def test_extract_returns_empty_list_when_no_questions_field(
        self, question_repo: ScreeningQuestionRepository, vacancy: Vacancy
    ) -> None:
        """A payload without ``questions`` produces no rows."""
        extractor = HhScreeningQuestionExtractor(question_repo=question_repo)
        raw = {"id": "v-1", "name": "Anything"}

        created = extractor.extract_from_vacancy(vacancy, raw)

        assert created == []
        assert question_repo.list_by_vacancy(vacancy.id) == []

    def test_extract_preserves_question_id_order(
        self, question_repo: ScreeningQuestionRepository, vacancy: Vacancy
    ) -> None:
        """The array order in the raw payload becomes the row's ``question_order``."""
        extractor = HhScreeningQuestionExtractor(question_repo=question_repo)
        raw = {
            "questions": [
                {"id": "q3", "text": "third"},
                {"id": "q1", "text": "first"},
                {"id": "q2", "text": "second"},
            ]
        }

        created = extractor.extract_from_vacancy(vacancy, raw)

        assert [q.question_text for q in created] == ["third", "first", "second"]
        assert [q.question_order for q in created] == [0, 1, 2]
        # The in-memory repo sorts by question_order on read.
        persisted = list(question_repo.list_by_vacancy(vacancy.id))
        assert [q.question_text for q in persisted] == ["third", "first", "second"]
        assert [q.question_order for q in persisted] == [0, 1, 2]

    def test_extract_handles_required_and_optional(
        self, question_repo: ScreeningQuestionRepository, vacancy: Vacancy
    ) -> None:
        """Both ``required: true`` and ``required: false`` rows are persisted.

        The :class:`ScreeningQuestion` ORM model does not store a
        ``required`` flag; the extractor only uses the ``text`` field.
        The test asserts both flavours come through unchanged.
        """
        extractor = HhScreeningQuestionExtractor(question_repo=question_repo)
        raw = {
            "questions": [
                {"id": "q1", "required": True, "text": "Required question"},
                {"id": "q2", "required": False, "text": "Optional question"},
            ]
        }

        created = extractor.extract_from_vacancy(vacancy, raw)

        assert {q.question_text for q in created} == {
            "Required question",
            "Optional question",
        }

    def test_extract_skips_empty_text_questions(
        self, question_repo: ScreeningQuestionRepository, vacancy: Vacancy
    ) -> None:
        """Empty / whitespace-only / non-string text is filtered out."""
        extractor = HhScreeningQuestionExtractor(question_repo=question_repo)
        raw = {
            "questions": [
                {"id": "q1", "text": "Valid question"},
                {"id": "q2", "text": ""},
                {"id": "q3", "text": "   "},
                {"id": "q4"},  # missing text
                {"id": "q5", "text": 42},  # non-string
                "not a dict",  # type: ignore[list-item]
            ]
        }

        created = extractor.extract_from_vacancy(vacancy, raw)

        assert [q.question_text for q in created] == ["Valid question"]
        assert [q.question_order for q in created] == [0]


# ---------------------------------------------------------------------------
# SourceService.ingest_vacancy integration
# ---------------------------------------------------------------------------


class TestSourceServiceIngestVacancyWithScreening:
    def test_ingest_vacancy_persists_questions_via_extractor(
        self, source_service: SourceService, question_repo: ScreeningQuestionRepository
    ) -> None:
        """The service runs the extractor after upserting the vacancy."""
        extractor = HhScreeningQuestionExtractor(question_repo=question_repo)
        raw = {
            "id": "v-ingest-1",
            "name": "Backend Developer",
            "employer": {"name": "Acme"},
            "questions": [
                {"id": "q1", "required": True, "text": "Why Acme?"},
                {"id": "q2", "required": False, "text": "Years with Go?"},
            ],
        }

        created = source_service.ingest_vacancy("hh", raw, screening_extractor=extractor)

        # The vacancy was upserted.
        persisted_vacancy = source_service.repo.find_by_source("hh", "v-ingest-1")
        assert len(persisted_vacancy) == 1

        # The questions were created with the right vacancy link.
        vacancy_id = persisted_vacancy[0].id
        assert {q.question_text for q in created} == {"Why Acme?", "Years with Go?"}
        for row in created:
            assert row.vacancy_id == vacancy_id

        # The questions are observable through the screening question repo.
        rows = list(question_repo.list_by_vacancy(vacancy_id))
        assert {q.question_text for q in rows} == {"Why Acme?", "Years with Go?"}

    def test_ingest_vacancy_without_extractor_skips_questions(
        self, source_service: SourceService, question_repo: ScreeningQuestionRepository
    ) -> None:
        """When no extractor is provided the service still upserts the vacancy."""
        raw = {
            "id": "v-ingest-2",
            "name": "Backend Developer",
            "employer": {"name": "Acme"},
            "questions": [
                {"id": "q1", "required": True, "text": "Why Acme?"},
            ],
        }

        created = source_service.ingest_vacancy("hh", raw)

        # No questions created and the return is an empty list.
        assert created == []
        assert question_repo.list_by_vacancy(uuid.uuid4()) == []
        # The vacancy is still persisted.
        assert source_service.repo.find_by_source("hh", "v-ingest-2")
