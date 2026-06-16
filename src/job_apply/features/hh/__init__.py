"""HH vertical slice.

Exports the public surface of the ``features/hh`` package so other
slices can reference ``HHCredential``, ``HHCredentialService``, etc.
without coupling to the internal module structure.
"""

from __future__ import annotations

from job_apply.features.hh.apply import HhApplyAdapter as HhApplyAdapter
from job_apply.features.hh.apply import HhApplyError as HhApplyError
from job_apply.features.hh.apply import HhApplyRateLimitError as HhApplyRateLimitError
from job_apply.features.hh.apply import HhApplyTokenProvider as HhApplyTokenProvider
from job_apply.features.hh.encryption import CredentialEncryptor as CredentialEncryptor
from job_apply.features.hh.models import HHCredential as HHCredential
from job_apply.features.hh.oauth import (
    HhAuthService as HhAuthService,
)
from job_apply.features.hh.oauth import (
    HhHttpOAuthClient as HhHttpOAuthClient,
)
from job_apply.features.hh.oauth import (
    HhOAuthClient as HhOAuthClient,
)
from job_apply.features.hh.oauth import (
    HhOAuthStateStore as HhOAuthStateStore,
)
from job_apply.features.hh.oauth import (
    HhTokenResponse as HhTokenResponse,
)
from job_apply.features.hh.oauth import (
    InMemoryHhOAuthClient as InMemoryHhOAuthClient,
)
from job_apply.features.hh.oauth import (
    InvalidOAuthStateError as InvalidOAuthStateError,
)
from job_apply.features.hh.oauth import (
    MissingRefreshTokenError as MissingRefreshTokenError,
)
from job_apply.features.hh.oauth import (
    OAuthExchangeError as OAuthExchangeError,
)
from job_apply.features.hh.resumes import HhHttpResumesClient as HhHttpResumesClient
from job_apply.features.hh.resumes import HhResumeLink as HhResumeLink
from job_apply.features.hh.resumes import (
    HhResumeLinkRepository as HhResumeLinkRepository,
)
from job_apply.features.hh.resumes import HhResumeNotFoundError as HhResumeNotFoundError
from job_apply.features.hh.resumes import HhResumesClient as HhResumesClient
from job_apply.features.hh.resumes import HhResumesError as HhResumesError
from job_apply.features.hh.resumes import (
    HhResumesSyncService as HhResumesSyncService,
)
from job_apply.features.hh.resumes import (
    HhResumesTokenProvider as HhResumesTokenProvider,
)
from job_apply.features.hh.resumes import (
    InMemoryHhResumeLinkRepository as InMemoryHhResumeLinkRepository,
)
from job_apply.features.hh.resumes import (
    InMemoryHhResumesClient as InMemoryHhResumesClient,
)
from job_apply.features.hh.resumes import (
    SqlHhResumeLinkRepository as SqlHhResumeLinkRepository,
)
from job_apply.features.hh.schemas import (
    CredentialCheck as CredentialCheck,
)
from job_apply.features.hh.schemas import (
    CredentialsStoreRequest as CredentialsStoreRequest,
)
from job_apply.features.hh.schemas import (
    HhResumeLinkDTO as HhResumeLinkDTO,
)
from job_apply.features.hh.schemas import (
    HhResumesListResponse as HhResumesListResponse,
)
from job_apply.features.hh.schemas import (
    HhResumesSyncResponse as HhResumesSyncResponse,
)
from job_apply.features.hh.schemas import (
    InternalCredentials as InternalCredentials,
)
from job_apply.features.hh.schemas import (
    RedactedCredentials as RedactedCredentials,
)
from job_apply.features.hh.search import HhHttpVacancySearchClient as HhHttpVacancySearchClient
from job_apply.features.hh.search import HHQuery as HHQuery
from job_apply.features.hh.search import HHRateLimitError as HHRateLimitError
from job_apply.features.hh.search import HHVacancyNotFoundError as HHVacancyNotFoundError
from job_apply.features.hh.search import HHVacancySearchClient as HHVacancySearchClient
from job_apply.features.hh.search import HHVacancySearchError as HHVacancySearchError
from job_apply.features.hh.search import (
    InMemoryHhVacancySearchClient as InMemoryHhVacancySearchClient,
)
from job_apply.features.hh.service import HHCredentialService as HHCredentialService

__all__ = [
    "CredentialCheck",
    "CredentialEncryptor",
    "CredentialsStoreRequest",
    "HHQuery",
    "HHRateLimitError",
    "HHCredential",
    "HHCredentialService",
    "HHVacancyNotFoundError",
    "HHVacancySearchClient",
    "HHVacancySearchError",
    "HhApplyAdapter",
    "HhApplyError",
    "HhApplyRateLimitError",
    "HhApplyTokenProvider",
    "HhAuthService",
    "HhHttpOAuthClient",
    "HhHttpResumesClient",
    "HhHttpVacancySearchClient",
    "HhOAuthClient",
    "HhOAuthStateStore",
    "HhResumeLink",
    "HhResumeLinkDTO",
    "HhResumeLinkRepository",
    "HhResumeNotFoundError",
    "HhResumesClient",
    "HhResumesError",
    "HhResumesListResponse",
    "HhResumesSyncResponse",
    "HhResumesSyncService",
    "HhResumesTokenProvider",
    "HhTokenResponse",
    "InMemoryHhOAuthClient",
    "InMemoryHhResumeLinkRepository",
    "InMemoryHhResumesClient",
    "InMemoryHhVacancySearchClient",
    "InternalCredentials",
    "InvalidOAuthStateError",
    "MissingRefreshTokenError",
    "OAuthExchangeError",
    "RedactedCredentials",
    "SqlHhResumeLinkRepository",
]
