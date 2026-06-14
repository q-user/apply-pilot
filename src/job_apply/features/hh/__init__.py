"""HH credentials vertical slice.

Exports the public surface of the ``features/hh`` package so other
slices can reference ``HHCredential``, ``HHCredentialService``, etc.
without coupling to the internal module structure.
"""

from __future__ import annotations

from job_apply.features.hh.encryption import CredentialEncryptor as CredentialEncryptor
from job_apply.features.hh.models import HHCredential as HHCredential
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
from job_apply.features.hh.service import HHCredentialService as HHCredentialService

__all__ = [
    "CredentialCheck",
    "CredentialEncryptor",
    "CredentialsStoreRequest",
    "HHCredential",
    "HHCredentialService",
    "InternalCredentials",
    "RedactedCredentials",
]
