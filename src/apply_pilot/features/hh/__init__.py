"""HH vertical slice.

Exports the public surface of the ``features/hh`` package so other
slices can reference ``HHCredential``, ``HHCredentialService``, etc.
without coupling to the internal module structure.
"""

from __future__ import annotations

from apply_pilot.features.hh.adapter import HhSourceAdapter as HhSourceAdapter
from apply_pilot.features.hh.apply import HhApplyAdapter as HhApplyAdapter
from apply_pilot.features.hh.apply import HhApplyError as HhApplyError
from apply_pilot.features.hh.apply import HhApplyRateLimitError as HhApplyRateLimitError
from apply_pilot.features.hh.apply import HhApplyTokenProvider as HhApplyTokenProvider
from apply_pilot.features.hh.encryption import CredentialEncryptor as CredentialEncryptor
from apply_pilot.features.hh.models import HHCredential as HHCredential
from apply_pilot.features.hh.oauth import (
    HhAuthService as HhAuthService,
)
from apply_pilot.features.hh.oauth import (
    HhHttpOAuthClient as HhHttpOAuthClient,
)
from apply_pilot.features.hh.oauth import (
    HhOAuthClient as HhOAuthClient,
)
from apply_pilot.features.hh.oauth import (
    HhOAuthStateStore as HhOAuthStateStore,
)
from apply_pilot.features.hh.oauth import (
    HhTokenResponse as HhTokenResponse,
)
from apply_pilot.features.hh.oauth import (
    InMemoryHhOAuthClient as InMemoryHhOAuthClient,
)
from apply_pilot.features.hh.oauth import (
    InvalidOAuthStateError as InvalidOAuthStateError,
)
from apply_pilot.features.hh.oauth import (
    MissingRefreshTokenError as MissingRefreshTokenError,
)
from apply_pilot.features.hh.oauth import (
    OAuthExchangeError as OAuthExchangeError,
)
from apply_pilot.features.hh.resumes import HhHttpResumesClient as HhHttpResumesClient
from apply_pilot.features.hh.resumes import HhResumeLink as HhResumeLink
from apply_pilot.features.hh.resumes import (
    HhResumeLinkRepository as HhResumeLinkRepository,
)
from apply_pilot.features.hh.resumes import HhResumeNotFoundError as HhResumeNotFoundError
from apply_pilot.features.hh.resumes import HhResumesClient as HhResumesClient
from apply_pilot.features.hh.resumes import HhResumesError as HhResumesError
from apply_pilot.features.hh.resumes import (
    HhResumesSyncService as HhResumesSyncService,
)
from apply_pilot.features.hh.resumes import (
    HhResumesTokenProvider as HhResumesTokenProvider,
)
from apply_pilot.features.hh.resumes import (
    InMemoryHhResumeLinkRepository as InMemoryHhResumeLinkRepository,
)
from apply_pilot.features.hh.resumes import (
    InMemoryHhResumesClient as InMemoryHhResumesClient,
)
from apply_pilot.features.hh.resumes import (
    SqlHhResumeLinkRepository as SqlHhResumeLinkRepository,
)
from apply_pilot.features.hh.schemas import (
    CredentialCheck as CredentialCheck,
)
from apply_pilot.features.hh.schemas import (
    CredentialsStoreRequest as CredentialsStoreRequest,
)
from apply_pilot.features.hh.schemas import (
    HhResumeLinkDTO as HhResumeLinkDTO,
)
from apply_pilot.features.hh.schemas import (
    HhResumesListResponse as HhResumesListResponse,
)
from apply_pilot.features.hh.schemas import (
    HhResumesSyncResponse as HhResumesSyncResponse,
)
from apply_pilot.features.hh.schemas import (
    InternalCredentials as InternalCredentials,
)
from apply_pilot.features.hh.schemas import (
    RedactedCredentials as RedactedCredentials,
)
from apply_pilot.features.hh.search import HhHttpVacancySearchClient as HhHttpVacancySearchClient
from apply_pilot.features.hh.search import HHQuery as HHQuery
from apply_pilot.features.hh.search import HHRateLimitError as HHRateLimitError
from apply_pilot.features.hh.search import HHVacancyNotFoundError as HHVacancyNotFoundError
from apply_pilot.features.hh.search import HHVacancySearchClient as HHVacancySearchClient
from apply_pilot.features.hh.search import HHVacancySearchError as HHVacancySearchError
from apply_pilot.features.hh.search import (
    InMemoryHhVacancySearchClient as InMemoryHhVacancySearchClient,
)
from apply_pilot.features.hh.service import HHCredentialService as HHCredentialService

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
    "HhSourceAdapter",
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
