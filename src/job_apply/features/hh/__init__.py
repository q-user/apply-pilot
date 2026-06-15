"""HH vertical slice.

Exports the public surface of the ``features/hh`` package so other
slices can reference ``HHCredential``, ``HHCredentialService``, etc.
without coupling to the internal module structure.
"""

from __future__ import annotations

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
from job_apply.features.hh.schemas import (
    CredentialCheck as CredentialCheck,
)
from job_apply.features.hh.schemas import (
    CredentialsStoreRequest as CredentialsStoreRequest,
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
    "HhAuthService",
    "HhHttpOAuthClient",
    "HhHttpVacancySearchClient",
    "HhOAuthClient",
    "HhOAuthStateStore",
    "HhTokenResponse",
    "InMemoryHhOAuthClient",
    "InMemoryHhVacancySearchClient",
    "InternalCredentials",
    "InvalidOAuthStateError",
    "MissingRefreshTokenError",
    "OAuthExchangeError",
    "RedactedCredentials",
]
