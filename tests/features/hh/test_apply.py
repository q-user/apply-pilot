"""TDD tests for the hh.ru apply adapter (M5, issue #45).

The :class:`HhApplyAdapter` is the hh-specific
:class:`~job_apply.features.apply_worker.runtime.ApplyAdapter`. It
submits an :class:`ApplyJob` to hh.ru's ``/negotiations`` endpoint
using the user's OAuth token, the vacancy's ``source_id``, the user's
most recent resume, and the latest cover-letter draft for the match.

The slice uses :class:`httpx.MockTransport` for HTTP — no real network
calls. Cross-slice collaborators (vacancy / resume / cover-letter /
credentials repositories) are collaborator-injected in-memory fakes or
Protocols so the test stays at the use-case level.

Test surface
------------

* :meth:`submit` returns the external application id on a 2xx response.
* :meth:`submit` includes the bearer token, the hh vacancy id, the
  resume id, and the cover-letter text in the POST body / headers.
* :meth:`submit` returns a non-retryable failure for 4xx responses
  (including ``401`` — token expired).
* :meth:`submit` returns a retryable failure for 5xx responses and
  ``429`` rate-limit responses.
* :meth:`submit` returns a non-retryable failure when pre-flight
  collaborators (credentials, vacancy, cover letter, resume) are
  missing — never a retryable failure, so the worker can dead-letter
  the job instead of looping.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from job_apply.features.apply_worker.models import ApplyJob, ApplyJobStatus
from job_apply.features.cover_letter.models import CoverLetterDraft, CoverLetterDraftStatus
from job_apply.features.cover_letter.repository import InMemoryCoverLetterDraftRepository
from job_apply.features.hh.apply import (
    HH_APPLY_NO_COVER_LETTER_ERROR,
    HH_APPLY_NO_CREDENTIALS_ERROR,
    HH_APPLY_NO_RESUME_ERROR,
    HH_APPLY_NO_VACANCY_ERROR,
    HhApplyAdapter,
    HTTPClientFactory,
)
from job_apply.features.hh.schemas import InternalCredentials
from job_apply.features.resumes.models import Resume
from job_apply.features.sources.models import Vacancy
from job_apply.shared.errors import NotFoundError

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeCredentialService:
    """In-memory stand-in for :class:`HHCredentialService`.

    Only :meth:`get_credentials` is exercised by the adapter; the fake
    is kept deliberately narrow so the test asserts on the adapter's
    behaviour, not on the credential service's storage.
    """

    credentials: dict[uuid.UUID, InternalCredentials] = field(default_factory=dict)

    def get_credentials(self, user_id: uuid.UUID) -> InternalCredentials:
        creds = self.credentials.get(user_id)
        if creds is None:
            raise NotFoundError.for_entity("HH credentials", str(user_id))
        return creds

    def add(self, credentials: InternalCredentials) -> InternalCredentials:
        self.credentials[credentials.user_id] = credentials
        return credentials


@dataclass
class _FakeVacancyRepo:
    """In-memory vacancy repo with the only method the adapter needs."""

    vacancies: dict[uuid.UUID, Vacancy] = field(default_factory=dict)

    def get_by_id(self, vacancy_id: uuid.UUID) -> Vacancy | None:
        return self.vacancies.get(vacancy_id)

    def add(self, vacancy: Vacancy) -> Vacancy:
        self.vacancies[vacancy.id] = vacancy
        return vacancy


@dataclass
class _FakeResumeRepo:
    """In-memory resume repo with the only method the adapter needs.

    The contract mirrors :class:`ResumesRepository` — resumes are
    returned newest-first so the adapter can pick the most recent one.
    """

    resumes: dict[uuid.UUID, Resume] = field(default_factory=dict)

    def list_for_user(self, user_id: uuid.UUID) -> list[Resume]:
        rows = [r for r in self.resumes.values() if r.user_id == user_id]
        rows.sort(key=lambda r: (r.created_at, r.id), reverse=True)
        return rows

    def add(self, resume: Resume) -> Resume:
        self.resumes[resume.id] = resume
        return resume


# ---------------------------------------------------------------------------
# World builder
# ---------------------------------------------------------------------------


@dataclass
class _CapturedRequest:
    """A single captured HTTP request and a configurable response."""

    method: str
    url: str
    headers: dict[str, str]
    body_json: dict[str, Any]
    response_status: int = 200
    response_json: dict[str, Any] = field(default_factory=lambda: {"id": "hh-app-12345"})

    @classmethod
    def from_request(cls, request: httpx.Request) -> _CapturedRequest:
        body_text = request.content.decode("utf-8") if request.content else "{}"
        try:
            body_json = json.loads(body_text) if body_text else {}
        except json.JSONDecodeError:
            body_json = {}
        return cls(
            method=request.method,
            url=str(request.url),
            headers={k.lower(): v for k, v in request.headers.items()},
            body_json=body_json,
        )

    def build_response(self) -> httpx.Response:
        return httpx.Response(
            self.response_status,
            json=self.response_json,
            request=httpx.Request(self.method, self.url),
        )


@dataclass
class _World:
    user_id: uuid.UUID
    vacancy: Vacancy
    resume: Resume
    draft: CoverLetterDraft
    credentials: InternalCredentials
    job: ApplyJob
    credential_service: _FakeCredentialService
    vacancy_repo: _FakeVacancyRepo
    resume_repo: _FakeResumeRepo
    draft_repo: InMemoryCoverLetterDraftRepository
    captured: list[_CapturedRequest]
    http_client_factory: HTTPClientFactory


def _make_world(
    *,
    response_status: int = 200,
    response_json: dict[str, Any] | None = None,
    with_credentials: bool = True,
    with_vacancy: bool = True,
    with_resume: bool = True,
    with_cover_letter: bool = True,
    vacancy_source_id: str = "hh-98765",
) -> _World:
    user_id = uuid.uuid4()
    match_id = uuid.uuid4()

    vacancy = Vacancy(
        id=uuid.uuid4(),
        source="hh",
        source_id=vacancy_source_id,
        title="Senior Python Developer",
        raw_data={"title": "Senior Python Developer"},
    )
    resume = Resume(
        id=uuid.uuid4(),
        user_id=user_id,
        filename="resume.pdf",
        content_type="application/pdf",
        size=1024,
        raw_text="raw text",
        plain_text="plain text",
        created_at=datetime.now(UTC),
    )
    draft = CoverLetterDraft(
        id=uuid.uuid4(),
        match_id=match_id,
        user_id=user_id,
        content="Dear hiring manager, I am writing to apply for the role.",
        prompt_version="cover_letter@v1",
        model_used="gpt-test",
        version=1,
        status=CoverLetterDraftStatus.DRAFT.value,
        created_at=datetime.now(UTC),
    )
    credentials = InternalCredentials(
        user_id=user_id,
        access_token="hh-access-token-abc",
        refresh_token="hh-refresh-xyz",
        token_type="bearer",
        expires_at=None,
    )

    credential_service = _FakeCredentialService()
    if with_credentials:
        credential_service.add(credentials)

    vacancy_repo = _FakeVacancyRepo()
    if with_vacancy:
        vacancy_repo.add(vacancy)

    resume_repo = _FakeResumeRepo()
    if with_resume:
        resume_repo.add(resume)

    draft_repo = InMemoryCoverLetterDraftRepository()
    if with_cover_letter:
        draft_repo.create(draft)

    job = ApplyJob(
        id=uuid.uuid4(),
        match_id=match_id,
        user_id=user_id,
        vacancy_id=vacancy.id,
        status=ApplyJobStatus.RUNNING.value,
        attempts=1,
        idempotency_key="idempotency-test",
    )

    captured: list[_CapturedRequest] = []

    def _factory(base_url: str | None) -> httpx.AsyncClient:  # noqa: ARG001
        def handler(request: httpx.Request) -> httpx.Response:
            cap = _CapturedRequest.from_request(request)
            cap.response_status = response_status
            if response_json is not None:
                cap.response_json = response_json
            captured.append(cap)
            return cap.build_response()

        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url=base_url or "https://api.hh.ru",
        )

    return _World(
        user_id=user_id,
        vacancy=vacancy,
        resume=resume,
        draft=draft,
        credentials=credentials,
        job=job,
        credential_service=credential_service,
        vacancy_repo=vacancy_repo,
        resume_repo=resume_repo,
        draft_repo=draft_repo,
        captured=captured,
        http_client_factory=_factory,
    )


def _make_adapter(world: _World, *, base_url: str = "https://api.hh.ru") -> HhApplyAdapter:
    """Build the adapter with the world's pre-wired fakes + http client factory.

    The factory is captured at world construction time so per-test
    response status / body overrides flow into the adapter without an
    extra indirection layer.
    """
    return HhApplyAdapter(
        http_client_factory=world.http_client_factory,
        credential_service=world.credential_service,  # type: ignore[arg-type]
        vacancy_repo=world.vacancy_repo,
        resume_repo=world.resume_repo,  # type: ignore[arg-type]
        cover_letter_repo=world.draft_repo,
        base_url=base_url,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_success_returns_external_application_id() -> None:
    """A 2xx response yields ``success=True`` and the external application id."""
    world = _make_world(response_status=201, response_json={"id": "hh-app-7777"})
    adapter = _make_adapter(world)

    result = await adapter.submit(world.job)

    assert result.success is True
    assert result.external_application_id == "hh-app-7777"
    assert result.retryable is False
    assert result.error is None


@pytest.mark.asyncio
async def test_submit_posts_to_negotiations_endpoint() -> None:
    """The adapter POSTs to ``/negotiations`` on the hh base URL."""
    world = _make_world()
    adapter = _make_adapter(world)

    await adapter.submit(world.job)

    assert len(world.captured) == 1
    request = world.captured[0]
    assert request.method == "POST"
    assert request.url.endswith("/negotiations")


@pytest.mark.asyncio
async def test_submit_includes_bearer_token() -> None:
    """The ``Authorization`` header carries the user's access token."""
    world = _make_world()
    adapter = _make_adapter(world)

    await adapter.submit(world.job)

    assert world.captured[0].headers["authorization"] == "Bearer hh-access-token-abc"


@pytest.mark.asyncio
async def test_submit_includes_vacancy_id_from_vacancy_repo() -> None:
    """The body's ``vacancy_id`` is the vacancy's ``source_id`` (the hh id)."""
    world = _make_world(vacancy_source_id="hh-112233")
    adapter = _make_adapter(world)

    await adapter.submit(world.job)

    assert world.captured[0].body_json["vacancy_id"] == "hh-112233"


@pytest.mark.asyncio
async def test_submit_includes_resume_id_from_resume_repo() -> None:
    """The body's ``resume_id`` is the user's most recent resume id."""
    world = _make_world()
    adapter = _make_adapter(world)

    await adapter.submit(world.job)

    assert world.captured[0].body_json["resume_id"] == str(world.resume.id)


@pytest.mark.asyncio
async def test_submit_includes_cover_letter_message() -> None:
    """The body's ``message`` is the latest cover-letter draft's content."""
    world = _make_world()
    adapter = _make_adapter(world)

    await adapter.submit(world.job)

    assert world.captured[0].body_json["message"] == world.draft.content


# ---------------------------------------------------------------------------
# HTTP error mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_4xx_returns_non_retryable_failure() -> None:
    """A 4xx (other than 401/429) is a non-retryable failure."""
    world = _make_world(response_status=422, response_json={"error": "invalid"})
    adapter = _make_adapter(world)

    result = await adapter.submit(world.job)

    assert result.success is False
    assert result.retryable is False
    assert result.external_application_id is None
    assert result.error is not None
    assert "422" in result.error


@pytest.mark.asyncio
async def test_submit_5xx_returns_retryable_failure() -> None:
    """A 5xx is a retryable failure (transient upstream error)."""
    world = _make_world(response_status=502, response_json={"error": "bad gateway"})
    adapter = _make_adapter(world)

    result = await adapter.submit(world.job)

    assert result.success is False
    assert result.retryable is True
    assert result.external_application_id is None
    assert result.error is not None
    assert "502" in result.error


@pytest.mark.asyncio
async def test_submit_429_returns_retryable_failure() -> None:
    """``429`` is a retryable rate-limit failure."""
    world = _make_world(response_status=429, response_json={"error": "rate limit"})
    adapter = _make_adapter(world)

    result = await adapter.submit(world.job)

    assert result.success is False
    assert result.retryable is True
    assert result.external_application_id is None
    assert result.error is not None
    assert "429" in result.error


@pytest.mark.asyncio
async def test_submit_401_returns_non_retryable_failure() -> None:
    """``401`` (token expired) is a non-retryable failure — the user must re-auth."""
    world = _make_world(response_status=401, response_json={"error": "unauthorized"})
    adapter = _make_adapter(world)

    result = await adapter.submit(world.job)

    assert result.success is False
    assert result.retryable is False
    assert result.external_application_id is None
    assert result.error is not None
    assert "401" in result.error


@pytest.mark.asyncio
async def test_submit_network_error_returns_retryable_failure() -> None:
    """A connection / timeout error is a retryable failure.

    The :class:`HhApplyAdapter` does not own the network — the
    :func:`http_client_factory` may emit any :class:`httpx.HTTPError`
    on a connection drop. The adapter must surface that as retryable.
    """

    def _factory(base_url: str | None) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("simulated connection drop")

        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url=base_url or "https://api.hh.ru",
        )

    world = _make_world()
    adapter = HhApplyAdapter(
        http_client_factory=_factory,
        credential_service=world.credential_service,  # type: ignore[arg-type]
        vacancy_repo=world.vacancy_repo,
        resume_repo=world.resume_repo,  # type: ignore[arg-type]
        cover_letter_repo=world.draft_repo,
    )

    result = await adapter.submit(world.job)

    assert result.success is False
    assert result.retryable is True
    assert result.external_application_id is None
    assert result.error is not None


# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_no_credentials_raises_non_retryable_failure() -> None:
    """Missing credentials surface as a non-retryable failure with a stable code."""
    world = _make_world(with_credentials=False)
    adapter = _make_adapter(world)

    result = await adapter.submit(world.job)

    assert result.success is False
    assert result.retryable is False
    assert result.external_application_id is None
    assert result.error == HH_APPLY_NO_CREDENTIALS_ERROR
    # No HTTP call should have been made — we failed before the wire.
    assert world.captured == []


@pytest.mark.asyncio
async def test_submit_no_vacancy_raises_non_retryable_failure() -> None:
    """A missing vacancy surfaces as a non-retryable failure with a stable code.

    The runtime already handles the missing-vacancy case before calling
    the adapter, but the adapter is a defensive backstop so a missing
    vacancy never turns into a retryable failure (which would loop the
    worker for as long as the row lives).
    """
    world = _make_world(with_vacancy=False)
    adapter = _make_adapter(world)

    result = await adapter.submit(world.job)

    assert result.success is False
    assert result.retryable is False
    assert result.external_application_id is None
    assert result.error == HH_APPLY_NO_VACANCY_ERROR
    assert world.captured == []


@pytest.mark.asyncio
async def test_submit_no_cover_letter_raises_non_retryable_failure() -> None:
    """A missing cover-letter draft surfaces as a non-retryable failure."""
    world = _make_world(with_cover_letter=False)
    adapter = _make_adapter(world)

    result = await adapter.submit(world.job)

    assert result.success is False
    assert result.retryable is False
    assert result.external_application_id is None
    assert result.error == HH_APPLY_NO_COVER_LETTER_ERROR
    assert world.captured == []


@pytest.mark.asyncio
async def test_submit_no_resume_raises_non_retryable_failure() -> None:
    """A user with no resume surfaces as a non-retryable failure."""
    world = _make_world(with_resume=False)
    adapter = _make_adapter(world)

    result = await adapter.submit(world.job)

    assert result.success is False
    assert result.retryable is False
    assert result.external_application_id is None
    assert result.error == HH_APPLY_NO_RESUME_ERROR
    assert world.captured == []


# ---------------------------------------------------------------------------
# ApplyResult contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_name_is_hh() -> None:
    """The adapter's ``name`` attribute matches the worker key."""
    world = _make_world()
    adapter = _make_adapter(world)

    assert adapter.name == "hh"


@pytest.mark.asyncio
async def test_submit_response_with_missing_id_yields_none_external_id() -> None:
    """A 2xx body without an ``id`` key returns ``external_application_id=None``."""
    world = _make_world(response_status=200, response_json={})
    adapter = _make_adapter(world)

    result = await adapter.submit(world.job)

    assert result.success is True
    assert result.external_application_id is None
    assert result.error is None
