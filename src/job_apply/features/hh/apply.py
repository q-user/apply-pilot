"""hh.ru-specific :class:`ApplyAdapter` (M5, issue #45).

The :class:`HhApplyAdapter` is the hh.ru implementation of the
:class:`~job_apply.features.apply_worker.runtime.ApplyAdapter`
Protocol. It submits an :class:`ApplyJob` to hh.ru's ``/negotiations``
endpoint using:

* the user's OAuth access token (from
  :class:`~job_apply.features.hh.service.HHCredentialService`),
* the vacancy's ``source_id`` (from
  :class:`~job_apply.features.sources.models.Vacancy`),
* the user's most recently uploaded resume (from the resumes slice),
* the latest :class:`~job_apply.features.cover_letter.models.CoverLetterDraft`
  for the job's match.

Slice boundaries
----------------

The adapter is the only place that knows the hh.ru negotiations wire
format. It does not own the queue, the retry policy, or the database —
those live in the ``apply_worker`` slice. It also does not own the
network: the :data:`HTTPClientFactory` is injected so tests can swap
in :class:`httpx.MockTransport` and the production wiring can plug a
pooled :class:`httpx.AsyncClient` into the factory.

Pre-flight failures (missing credentials, missing vacancy, missing
cover letter, missing resume) are returned as **non-retryable**
:class:`ApplyResult` failures with stable error codes. The worker uses
the ``retryable`` flag to choose between re-queueing and dead-lettering;
turning a structural "this row is missing required data" condition into
a retryable failure would loop the worker for as long as the row lives.

HTTP status mapping
-------------------

* ``2xx``                     → success (``external_application_id`` is
                                the response's ``id`` field, when
                                present).
* ``400..499`` (other)        → non-retryable (the request will not
                                become valid on retry).
* ``401``                     → non-retryable (the user's token is
                                invalid; the user must re-authenticate
                                before any submission can succeed).
* ``429``                     → retryable (rate-limited; backing off is
                                the only correct response).
* ``500..599``                → retryable (transient upstream).
* Timeout / network error     → retryable (transient).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from typing import Any, Protocol

import httpx

from job_apply.features.apply_worker.models import ApplyJob
from job_apply.features.apply_worker.runtime import ApplyResult
from job_apply.features.cover_letter.repository import CoverLetterDraftRepository
from job_apply.features.hh.service import HHCredentialService
from job_apply.features.resumes.models import Resume
from job_apply.features.sources.models import Vacancy

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Stable error codes
# ---------------------------------------------------------------------------
#
# These strings are the public contract for pre-flight failures: the
# :class:`~job_apply.features.apply_worker.runtime.ApplyWorker` will
# persist them on the :class:`ApplyJob.last_error` column, where the
# dashboard and alerting match on the exact value. Renaming any of these
# is a breaking change for downstream consumers.

HH_APPLY_NO_CREDENTIALS_ERROR: str = "hh_apply_no_credentials"
HH_APPLY_NO_VACANCY_ERROR: str = "hh_apply_no_vacancy"
HH_APPLY_NO_COVER_LETTER_ERROR: str = "hh_apply_no_cover_letter"
HH_APPLY_NO_RESUME_ERROR: str = "hh_apply_no_resume"
HH_APPLY_UNAUTHORIZED_ERROR: str = "hh_apply_unauthorized"
HH_APPLY_RATE_LIMITED_ERROR: str = "hh_apply_rate_limited"
HH_APPLY_SERVER_ERROR_PREFIX: str = "hh_apply_server_error"
HH_APPLY_CLIENT_ERROR_PREFIX: str = "hh_apply_client_error"
HH_APPLY_NETWORK_ERROR_PREFIX: str = "hh_apply_network_error"
HH_APPLY_INVALID_RESPONSE_ERROR: str = "hh_apply_invalid_response"


# ---------------------------------------------------------------------------
# DI Protocols — narrow views the adapter needs
# ---------------------------------------------------------------------------


#: A factory that produces a fresh :class:`httpx.AsyncClient` for a
#: given base URL. The adapter calls the factory per submission and
#: owns the resulting client's lifetime (``aclose`` after every
#: ``submit`` call). The factory is injected so tests can swap in a
#: client bound to :class:`httpx.MockTransport` without a real network.
HTTPClientFactory = Callable[[str | None], httpx.AsyncClient]


class _VacancyLookup(Protocol):
    """The slice's view of the vacancy repository.

    Only :meth:`get_by_id` is needed — the adapter reads the vacancy's
    ``source_id`` (the hh.ru id) to include in the POST body.
    """

    def get_by_id(self, vacancy_id: uuid.UUID) -> Vacancy | None: ...


class _ResumeLookup(Protocol):
    """The slice's view of the resumes repository.

    The adapter picks the user's most recent resume to attach to the
    negotiation. The :class:`ResumesRepository` returns rows newest-first
    so the slice does not have to know the order.
    """

    def list_for_user(self, user_id: uuid.UUID) -> list[Resume]: ...


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class HhApplyAdapter:
    """hh.ru-specific :class:`ApplyAdapter`.

    The adapter is collaborator-injected. Production wiring in
    :mod:`job_apply.features.apply_worker.api` (or the future process
    entry-point) plugs in the SQLAlchemy-backed implementations; tests
    use lightweight fakes (``Protocol``-compatible stubs) for the
    cross-slice collaborators.
    """

    #: The dictionary key the :class:`ApplyWorker` uses to pick this
    #: adapter from its registry; matches ``Vacancy.source`` for hh rows.
    name: str = "hh"

    #: hh.ru's public API root.
    DEFAULT_BASE_URL: str = "https://api.hh.ru"

    #: Default User-Agent string. hh.ru requires a non-default
    #: User-Agent; we send one with a version stamp.
    DEFAULT_USER_AGENT: str = "ApplyPilot/0.1"

    def __init__(
        self,
        *,
        http_client_factory: HTTPClientFactory,
        credential_service: HHCredentialService,
        vacancy_repo: _VacancyLookup,
        resume_repo: _ResumeLookup,
        cover_letter_repo: CoverLetterDraftRepository,
        base_url: str = DEFAULT_BASE_URL,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        if not callable(http_client_factory):
            raise TypeError("http_client_factory must be callable")
        self._http_client_factory = http_client_factory
        self._credential_service = credential_service
        self._vacancy_repo = vacancy_repo
        self._resume_repo = resume_repo
        self._cover_letter_repo = cover_letter_repo
        self._base_url = base_url.rstrip("/")
        self._user_agent = user_agent

    # ------------------------------------------------------------------
    # ApplyAdapter
    # ------------------------------------------------------------------

    async def submit(self, job: ApplyJob) -> ApplyResult:
        """Submit *job* to hh.ru's ``/negotiations`` endpoint.

        The flow is:

        1. Resolve the user's OAuth credentials.
        2. Resolve the vacancy by id (the runtime also does this, but
           the adapter is a defensive backstop).
        3. Resolve the latest cover-letter draft for the match.
        4. Resolve the user's most recent resume.
        5. ``POST /negotiations`` with the vacancy id, the resume id,
           and the cover letter text as the message.

        Any pre-flight step that fails short-circuits to a
        **non-retryable** :class:`ApplyResult` with a stable error code.
        HTTP errors are mapped to retryable / non-retryable per the
        contract documented in the module docstring.
        """
        # -- pre-flight: credentials -------------------------------------
        # ``HHCredentialService.get_credentials`` raises :class:`NotFoundError`
        # when the user has no stored credentials. Any other exception is
        # treated as a structural failure too: a transient DB error must
        # not turn a "no credentials" condition into a retryable loop.
        try:
            credentials = self._credential_service.get_credentials(job.user_id)
        except Exception as exc:  # noqa: BLE001 — normalised to ApplyResult
            logger.warning(
                "hh_apply.no_credentials",
                extra={
                    "event": "hh_apply.no_credentials",
                    "job_id": str(job.id),
                    "user_id": str(job.user_id),
                    "error_type": type(exc).__name__,
                },
            )
            return ApplyResult(
                success=False,
                external_application_id=None,
                error=HH_APPLY_NO_CREDENTIALS_ERROR,
                retryable=False,
            )

        # -- pre-flight: vacancy -----------------------------------------
        vacancy = self._vacancy_repo.get_by_id(job.vacancy_id)
        if vacancy is None:
            logger.warning(
                "hh_apply.no_vacancy",
                extra={
                    "event": "hh_apply.no_vacancy",
                    "job_id": str(job.id),
                    "vacancy_id": str(job.vacancy_id),
                },
            )
            return ApplyResult(
                success=False,
                external_application_id=None,
                error=HH_APPLY_NO_VACANCY_ERROR,
                retryable=False,
            )

        # -- pre-flight: cover letter ------------------------------------
        draft = self._cover_letter_repo.get_by_match(job.match_id)
        if draft is None:
            logger.warning(
                "hh_apply.no_cover_letter",
                extra={
                    "event": "hh_apply.no_cover_letter",
                    "job_id": str(job.id),
                    "match_id": str(job.match_id),
                },
            )
            return ApplyResult(
                success=False,
                external_application_id=None,
                error=HH_APPLY_NO_COVER_LETTER_ERROR,
                retryable=False,
            )

        # -- pre-flight: resume ------------------------------------------
        resumes = list(self._resume_repo.list_for_user(job.user_id))
        if not resumes:
            logger.warning(
                "hh_apply.no_resume",
                extra={
                    "event": "hh_apply.no_resume",
                    "job_id": str(job.id),
                    "user_id": str(job.user_id),
                },
            )
            return ApplyResult(
                success=False,
                external_application_id=None,
                error=HH_APPLY_NO_RESUME_ERROR,
                retryable=False,
            )
        # ``ResumesRepository`` orders newest-first; the slice relies on
        # that contract to pick the most recently uploaded resume.
        resume = resumes[0]

        return await self._post_negotiation(
            access_token=credentials.access_token,
            vacancy_id=str(vacancy.source_id),
            resume_id=str(resume.id),
            message=draft.content,
            job_id=job.id,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _post_negotiation(
        self,
        *,
        access_token: str,
        vacancy_id: str,
        resume_id: str,
        message: str,
        job_id: uuid.UUID,
    ) -> ApplyResult:
        """Send the ``POST /negotiations`` request and translate the response."""
        url = f"{self._base_url}/negotiations"
        payload: dict[str, Any] = {
            "vacancy_id": vacancy_id,
            "resume_id": resume_id,
            "message": message,
        }
        headers = {
            "Authorization": f"Bearer {access_token}",
            "User-Agent": self._user_agent,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        # The factory may return either a shared client (production) or
        # a fresh one (tests). Either way, we own the client lifetime
        # for the duration of this submit and close it before returning.
        client = self._http_client_factory(self._base_url)
        try:
            try:
                response = await client.post(url, json=payload, headers=headers)
            except httpx.TimeoutException as exc:
                logger.warning(
                    "hh_apply.timeout",
                    extra={
                        "event": "hh_apply.timeout",
                        "job_id": str(job_id),
                        "vacancy_id": vacancy_id,
                    },
                )
                return ApplyResult(
                    success=False,
                    external_application_id=None,
                    error=f"{HH_APPLY_NETWORK_ERROR_PREFIX}: timeout ({exc})",
                    retryable=True,
                )
            except httpx.HTTPError as exc:
                # Connection drops, DNS failures, and the rest of the
                # httpx error family are all transient by default.
                logger.warning(
                    "hh_apply.network_error",
                    extra={
                        "event": "hh_apply.network_error",
                        "job_id": str(job_id),
                        "vacancy_id": vacancy_id,
                        "error_type": type(exc).__name__,
                    },
                )
                return ApplyResult(
                    success=False,
                    external_application_id=None,
                    error=f"{HH_APPLY_NETWORK_ERROR_PREFIX}: {type(exc).__name__} ({exc})",
                    retryable=True,
                )
        finally:
            await client.aclose()

        return self._parse_response(response, job_id=job_id, vacancy_id=vacancy_id)

    @staticmethod
    def _parse_response(
        response: httpx.Response,
        *,
        job_id: uuid.UUID,
        vacancy_id: str,
    ) -> ApplyResult:
        """Map an hh.ru response to an :class:`ApplyResult`."""
        status = response.status_code
        if 200 <= status < 300:
            try:
                data = response.json()
            except ValueError as exc:
                # 2xx with a non-JSON body is a server-side contract
                # violation — non-retryable.
                logger.warning(
                    "hh_apply.invalid_response",
                    extra={
                        "event": "hh_apply.invalid_response",
                        "job_id": str(job_id),
                        "vacancy_id": vacancy_id,
                        "status": status,
                    },
                )
                return ApplyResult(
                    success=False,
                    external_application_id=None,
                    error=f"{HH_APPLY_INVALID_RESPONSE_ERROR}: {exc}",
                    retryable=False,
                )
            external_id = data.get("id") if isinstance(data, dict) else None
            return ApplyResult(
                success=True,
                external_application_id=str(external_id) if external_id is not None else None,
                error=None,
                retryable=False,
            )
        if status == httpx.codes.TOO_MANY_REQUESTS:
            logger.warning(
                "hh_apply.rate_limited",
                extra={
                    "event": "hh_apply.rate_limited",
                    "job_id": str(job_id),
                    "vacancy_id": vacancy_id,
                    "status": status,
                },
            )
            return ApplyResult(
                success=False,
                external_application_id=None,
                error=f"{HH_APPLY_RATE_LIMITED_ERROR}: HTTP {status}",
                retryable=True,
            )
        if status in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN):
            # ``401`` (and the conservative ``403``) means the user's
            # token is no longer valid; the worker must dead-letter so
            # the user can re-authenticate via the credentials flow.
            logger.warning(
                "hh_apply.unauthorized",
                extra={
                    "event": "hh_apply.unauthorized",
                    "job_id": str(job_id),
                    "vacancy_id": vacancy_id,
                    "status": status,
                },
            )
            return ApplyResult(
                success=False,
                external_application_id=None,
                error=f"{HH_APPLY_UNAUTHORIZED_ERROR}: HTTP {status}",
                retryable=False,
            )
        if 400 <= status < 500:
            # Any other 4xx is a request-side problem that will not be
            # fixed by retrying (bad vacancy id, malformed message, etc.).
            logger.warning(
                "hh_apply.client_error",
                extra={
                    "event": "hh_apply.client_error",
                    "job_id": str(job_id),
                    "vacancy_id": vacancy_id,
                    "status": status,
                },
            )
            return ApplyResult(
                success=False,
                external_application_id=None,
                error=f"{HH_APPLY_CLIENT_ERROR_PREFIX}: HTTP {status}",
                retryable=False,
            )
        if 500 <= status < 600:
            logger.warning(
                "hh_apply.server_error",
                extra={
                    "event": "hh_apply.server_error",
                    "job_id": str(job_id),
                    "vacancy_id": vacancy_id,
                    "status": status,
                },
            )
            return ApplyResult(
                success=False,
                external_application_id=None,
                error=f"{HH_APPLY_SERVER_ERROR_PREFIX}: HTTP {status}",
                retryable=True,
            )

        # Unhandled status code — treat as non-retryable to be safe.
        logger.warning(
            "hh_apply.unexpected_status",
            extra={
                "event": "hh_apply.unexpected_status",
                "job_id": str(job_id),
                "vacancy_id": vacancy_id,
                "status": status,
            },
        )
        return ApplyResult(
            success=False,
            external_application_id=None,
            error=f"hh_apply_unexpected_status: HTTP {status}",
            retryable=False,
        )


__all__ = [
    "HH_APPLY_CLIENT_ERROR_PREFIX",
    "HH_APPLY_INVALID_RESPONSE_ERROR",
    "HH_APPLY_NETWORK_ERROR_PREFIX",
    "HH_APPLY_NO_COVER_LETTER_ERROR",
    "HH_APPLY_NO_CREDENTIALS_ERROR",
    "HH_APPLY_NO_RESUME_ERROR",
    "HH_APPLY_NO_VACANCY_ERROR",
    "HH_APPLY_RATE_LIMITED_ERROR",
    "HH_APPLY_SERVER_ERROR_PREFIX",
    "HH_APPLY_UNAUTHORIZED_ERROR",
    "HTTPClientFactory",
    "HhApplyAdapter",
]
