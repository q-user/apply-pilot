"""FastAPI router for the ``screening`` slice (M3, issue #34).

Endpoints
---------

* ``POST /screening/questions/{vacancy_id}`` — attach one or more
  questions to a vacancy. Body: ``{"questions": ["...", "..."]}``.
* ``POST /screening/questions/{question_id}/answer`` — generate the
  caller's answer for one question. Idempotent: a second call
  updates the same row.
* ``GET /screening/answers`` — list the caller's answers, optionally
  filtered by ``vacancy_id`` (``?vacancy_id=<uuid>``).

All endpoints require a valid bearer token; the user id is derived
from the token. The service enforces ownership through the
``user_id`` it receives from the router — every answer is stamped
with the calling user.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from job_apply.db import get_db
from job_apply.features.scoring.llm import (
    HttpLLMClient,
    LLMClient,
    LLMSettings,
    get_llm_settings,
)
from job_apply.features.screening.models import (
    ScreeningQuestion,
    ScreeningQuestionAnswer,
)
from job_apply.features.screening.repository import (
    SqlScreeningAnswerRepository,
    SqlScreeningQuestionRepository,
)
from job_apply.features.screening.schemas import (
    AddQuestionsRequest,
    ScreeningQuestionAnswerRead,
    ScreeningQuestionRead,
)
from job_apply.features.screening.service import (
    ScreeningQuestionNotFoundError,
    ScreeningService,
)
from job_apply.features.sources.repository import SqlVacancyRepository
from job_apply.features.users.repository import SqlAlchemyUsersRepository
from job_apply.features.users.security import InvalidTokenError, default_token_store

_LOGGER = logging.getLogger("job_apply.features.screening.api")

router = APIRouter(prefix="/screening", tags=["screening"])

# ``auto_error=False`` lets us return our own 401 with a stable JSON
# shape instead of FastAPI's default ``{"detail": "Not authenticated"}``.
_bearer_scheme = HTTPBearer(auto_error=False)


def _http_error(status_code: int, code: str, message: str) -> HTTPException:
    """Return a JSON-shaped 4xx error that the API contract promises."""
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _resolve_user_id(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),  # noqa: B008
) -> str:
    """Extract the user id from the bearer token, or raise 401."""
    if credentials is None:
        raise _http_error(
            status.HTTP_401_UNAUTHORIZED,
            "authentication_required",
            "bearer token is required",
        )
    tokens = default_token_store()
    try:
        return tokens.resolve(credentials.credentials)
    except InvalidTokenError as exc:
        raise _http_error(
            status.HTTP_401_UNAUTHORIZED,
            "invalid_token",
            "the supplied token is invalid or expired",
        ) from exc


def get_llm_client() -> LLMClient:
    """Build the LLM client for the current request.

    Defaults to the HTTP-backed client speaking the
    OpenAI-compatible ``/v1/chat/completions`` endpoint. Settings
    come from the environment via :func:`get_llm_settings`. The
    dependency is a seam so tests can drop in an in-memory fake.
    """
    settings: LLMSettings = get_llm_settings()
    return HttpLLMClient(settings=settings)


def get_screening_service(
    session: Session = Depends(get_db),  # noqa: B008
    llm: LLMClient = Depends(get_llm_client),  # noqa: B008
) -> ScreeningService:
    """Build a :class:`ScreeningService` for the current request.

    The SQL repositories share the request-scoped session so the
    question + answer persistence is one transaction. The user /
    vacancy / resume repositories are wired with the same session
    factory so a single :func:`get_db` call serves the entire
    request.
    """
    question_repo = SqlScreeningQuestionRepository(session=session)
    answer_repo = SqlScreeningAnswerRepository(session=session)
    user_repo = SqlAlchemyUsersRepository(session=session)
    vacancy_repo = SqlVacancyRepository(session=session)

    # The resumes repository is the SQLAlchemy-backed gateway. The
    # screening service only needs ``list_for_user``; the SQL repo
    # already implements that path.
    from job_apply.features.resumes.repository import ResumesRepository

    resume_repo = ResumesRepository(session)

    return ScreeningService(
        llm=llm,
        question_repo=question_repo,
        answer_repo=answer_repo,
        user_repo=user_repo,  # type: ignore[arg-type]
        resume_repo=resume_repo,  # type: ignore[arg-type]
        vacancy_repo=vacancy_repo,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# ORM → DTO mappers
# ---------------------------------------------------------------------------


def _question_to_dto(question: ScreeningQuestion) -> ScreeningQuestionRead:
    """Map an ORM row to the public DTO."""
    return ScreeningQuestionRead(
        id=question.id,
        vacancy_id=question.vacancy_id,
        question_text=question.question_text,
        question_order=question.question_order,
        created_at=question.created_at,
    )


def _answer_to_dto(answer: ScreeningQuestionAnswer) -> ScreeningQuestionAnswerRead:
    """Map an ORM row to the public DTO."""
    return ScreeningQuestionAnswerRead(
        id=answer.id,
        question_id=answer.question_id,
        user_id=answer.user_id,
        answer_text=answer.answer_text,
        prompt_version=answer.prompt_version,
        model_used=answer.model_used,
        created_at=answer.created_at,
        updated_at=answer.updated_at,
    )


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


@router.post(
    "/questions/{vacancy_id}",
    response_model=list[ScreeningQuestionRead],
    status_code=status.HTTP_201_CREATED,
    responses={
        401: {"description": "Missing or invalid bearer token"},
        422: {"description": "Body is missing the ``questions`` list"},
    },
)
def add_questions_to_vacancy(
    vacancy_id: str,
    payload: AddQuestionsRequest,
    user_id_str: str = Depends(_resolve_user_id),  # noqa: B008
    service: ScreeningService = Depends(get_screening_service),  # noqa: B008
) -> list[ScreeningQuestionRead]:
    """Attach one or more screening questions to ``vacancy_id``.

    The body is ``{"questions": ["...", "..."]}``. The questions
    are persisted with sequential ``question_order`` indices starting
    at 0. The vacancy itself is not modified by this call — the
    endpoint accepts any well-formed ``vacancy_id`` and the service
    does not re-check existence (the question rows store the
    ``vacancy_id`` verbatim).
    """
    try:
        vacancy_uuid = uuid.UUID(vacancy_id)
    except ValueError as exc:
        raise _http_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_id", "invalid vacancy id"
        ) from exc
    # The user_id is resolved for the auth gate; the endpoint does
    # not stamp the questions with the user (questions are global to
    # the vacancy).
    _ = uuid.UUID(user_id_str)
    created = service.add_questions_to_vacancy(vacancy_uuid, payload.questions)
    return [_question_to_dto(q) for q in created]


@router.post(
    "/questions/{question_id}/answer",
    response_model=ScreeningQuestionAnswerRead,
    status_code=status.HTTP_201_CREATED,
    responses={
        401: {"description": "Missing or invalid bearer token"},
        404: {"description": "No screening question with this id"},
    },
)
async def generate_answer(
    question_id: str,
    user_id_str: str = Depends(_resolve_user_id),  # noqa: B008
    service: ScreeningService = Depends(get_screening_service),  # noqa: B008
) -> ScreeningQuestionAnswerRead:
    """Generate (or refresh) the caller's answer for ``question_id``.

    The method is idempotent: a second call updates the same row
    rather than inserting a duplicate. A missing question is a 404.
    """
    try:
        question_uuid = uuid.UUID(question_id)
    except ValueError as exc:
        raise _http_error(status.HTTP_404_NOT_FOUND, "not_found", "invalid question id") from exc
    user_uuid = uuid.UUID(user_id_str)
    try:
        answer = await service.generate_answer(question_uuid, user_uuid)
    except ScreeningQuestionNotFoundError as exc:
        raise _http_error(
            status.HTTP_404_NOT_FOUND,
            "screening_question_not_found",
            str(exc),
        ) from exc
    return _answer_to_dto(answer)


@router.get(
    "/answers",
    response_model=list[ScreeningQuestionAnswerRead],
    responses={
        401: {"description": "Missing or invalid bearer token"},
    },
)
def list_my_answers(
    vacancy_id: str | None = Query(default=None),
    user_id_str: str = Depends(_resolve_user_id),  # noqa: B008
    service: ScreeningService = Depends(get_screening_service),  # noqa: B008
) -> list[ScreeningQuestionAnswerRead]:
    """Return the caller's answers.

    The optional ``vacancy_id`` query parameter narrows the listing
    to answers whose question belongs to that vacancy.
    """
    user_uuid = uuid.UUID(user_id_str)
    vacancy_uuid: uuid.UUID | None = None
    if vacancy_id is not None:
        try:
            vacancy_uuid = uuid.UUID(vacancy_id)
        except ValueError as exc:
            raise _http_error(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid_id", "invalid vacancy id"
            ) from exc
    answers = service.list_user_answers(user_uuid, vacancy_id=vacancy_uuid)
    return [_answer_to_dto(a) for a in answers]


__all__ = [
    "get_llm_client",
    "get_screening_service",
    "router",
]
