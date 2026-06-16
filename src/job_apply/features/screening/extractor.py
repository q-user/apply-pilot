"""Screening-question extractor (M2, issue #26).

The M2 capture flow is the bridge between the ``sources`` slice (which
parses raw hh.ru payloads into a canonical :class:`Vacancy`) and the
``screening`` slice (which stores the screening questions a candidate
is asked to answer). The two slices do not import each other directly
— the ``SourceService`` accepts an optional
:class:`ScreeningQuestionExtractor` and delegates the question capture
to it. This keeps the ``sources`` slice agnostic of the screening
schema while letting the screening slice own its own extractor.

Public surface
--------------

* :class:`ScreeningQuestionExtractor` — the Protocol the
  :class:`~job_apply.features.sources.service.SourceService` depends
  on. Any class that exposes ``extract_from_vacancy(vacancy, raw)``
  satisfies it (structurally; the Protocol is ``@runtime_checkable``).
* :class:`HhScreeningQuestionExtractor` — the default
  implementation, modelled on the hh.ru ``/vacancies/{id}`` response
  schema. Reads ``raw["questions"]`` and turns each entry into a
  :class:`ScreeningQuestion` row.

Field mapping
-------------

The hh.ru ``questions`` payload looks like::

    "questions": [
        {"id": "q1", "required": true,  "text": "Why us?"},
        {"id": "q2", "required": false, "text": "Years of Python?"},
    ]

The :class:`~job_apply.features.screening.models.ScreeningQuestion`
ORM model only stores ``question_text`` and ``question_order`` — the
``id`` and ``required`` flags are hh-internal metadata that the rest
of the application does not consume, so they are intentionally
dropped on the floor. Each accepted entry is turned into a row with:

* ``vacancy_id``  = the upserted vacancy's id
* ``question_text`` = the entry's ``text`` (whitespace-stripped)
* ``question_order`` = the entry's position in the array

Empty / non-string / non-dict entries are filtered out so the
repository never sees a malformed row.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from job_apply.features.screening.models import ScreeningQuestion
from job_apply.features.screening.repository import ScreeningQuestionRepository
from job_apply.features.sources.models import Vacancy


@runtime_checkable
class ScreeningQuestionExtractor(Protocol):
    """Capture screening questions for a freshly-upserted vacancy.

    The :class:`~job_apply.features.sources.service.SourceService`
    calls this after the vacancy is persisted; the extractor is
    responsible for both producing the :class:`ScreeningQuestion`
    rows and pushing them through its repository. The return value is
    the list of created rows in the order they were persisted.
    """

    def extract_from_vacancy(
        self, vacancy: Vacancy, raw: dict[str, Any]
    ) -> list[ScreeningQuestion]: ...


class HhScreeningQuestionExtractor:
    """Default :class:`ScreeningQuestionExtractor` for hh.ru payloads.

    Reads ``raw.get("questions", [])`` and turns each entry into a
    :class:`ScreeningQuestion` row. The extractor is the only
    component that knows the hh-specific field names; adding a new
    source (Habr Career, Telegram channel) means writing a sibling
    extractor class — the rest of the slice is unchanged.

    The extractor takes a :class:`ScreeningQuestionRepository`
    dependency so it persists in the same transaction model as the
    rest of the screening slice (in-memory dict or SQLAlchemy
    session, depending on the wiring).
    """

    def __init__(self, *, question_repo: ScreeningQuestionRepository) -> None:
        self._question_repo = question_repo

    @property
    def question_repo(self) -> ScreeningQuestionRepository:
        """Return the injected question repository (read-only)."""
        return self._question_repo

    def extract_from_vacancy(
        self, vacancy: Vacancy, raw: dict[str, Any]
    ) -> list[ScreeningQuestion]:
        """Build ScreeningQuestion rows from ``raw`` and persist them.

        The hh payload may be missing the ``questions`` field entirely
        (most vacancies do not have screening questions) or carry an
        empty list; both cases return ``[]`` without touching the
        repository. Malformed entries (non-dict, missing / empty
        ``text``, non-string ``text``) are filtered out so the model
        only ever sees well-formed rows.

        Returns:
            The list of created :class:`ScreeningQuestion` rows in the
            order they were persisted. The ``question_order`` column
            carries the original array index.
        """
        questions_raw = raw.get("questions")
        if not isinstance(questions_raw, list):
            return []

        rows: list[ScreeningQuestion] = []
        for index, entry in enumerate(questions_raw):
            if not isinstance(entry, dict):
                continue
            text = entry.get("text")
            if not isinstance(text, str):
                continue
            cleaned = text.strip()
            if not cleaned:
                continue
            rows.append(
                ScreeningQuestion(
                    vacancy_id=vacancy.id,
                    question_text=cleaned,
                    question_order=index,
                )
            )

        if not rows:
            return []
        return list(self._question_repo.create_many(rows))


__all__ = [
    "HhScreeningQuestionExtractor",
    "ScreeningQuestionExtractor",
]
