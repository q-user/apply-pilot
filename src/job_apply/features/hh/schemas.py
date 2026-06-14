"""DTOs for the HH credentials slice.

Every public DTO that touches token material uses string redaction in its
``__repr__`` / ``__str__`` so that tokens never leak into structured logs
or error messages. The raw token values are only available through
attribute access within the service layer.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CredentialsStoreRequest(BaseModel):
    """Input shape for ``POST /hh/credentials``."""

    model_config = ConfigDict(extra="forbid", frozen=False)

    access_token: str = Field(min_length=1, description="The hh.ru OAuth access token.")
    refresh_token: str | None = Field(
        default=None, description="The hh.ru OAuth refresh token (optional)."
    )
    token_type: str = Field(default="bearer", description="Token type (default: bearer).")
    expires_at: datetime | None = Field(
        default=None, description="When the access token expires (UTC)."
    )


class RedactedCredentials(BaseModel):
    """Output shape that hides token values.

    Used as the return type of ``POST /hh/credentials`` and ``store_credentials``.
    The actual token values are never serialised — only ``"REDACTED"`` placeholders.
    """

    model_config = ConfigDict(extra="forbid", frozen=False)

    user_id: uuid.UUID
    token_type: str
    expires_at: datetime | None
    access_token: str = "REDACTED"
    refresh_token: str = "REDACTED"

    def __repr__(self) -> str:
        return (
            f"RedactedCredentials(user_id={self.user_id!s}, token_type={self.token_type!r}, "
            f"expires_at={self.expires_at!r}, access_token={self.access_token!r}, "
            f"refresh_token={self.refresh_token!r})"
        )

    def __str__(self) -> str:
        return repr(self)


class InternalCredentials(BaseModel):
    """Internal DTO that carries the actual decrypted token values.

    Only used within the service layer; never returned to the HTTP layer.
    The ``__repr__`` / ``__str__`` still redact the token values.
    """

    model_config = ConfigDict(extra="forbid", frozen=False)

    user_id: uuid.UUID
    access_token: str
    refresh_token: str | None
    token_type: str
    expires_at: datetime | None

    def __repr__(self) -> str:
        return (
            f"InternalCredentials(user_id={self.user_id!s}, token_type={self.token_type!r}, "
            f"expires_at={self.expires_at!r}, access_token=REDACTED, refresh_token=REDACTED)"
        )

    def __str__(self) -> str:
        return repr(self)


class CredentialCheck(BaseModel):
    """Metadata-only response for ``GET /hh/credentials``.

    Contains zero token material — callers learn whether credentials
    exist and (if so) their type and expiry, but never the tokens
    themselves.
    """

    model_config = ConfigDict(extra="forbid", frozen=False)

    has_credentials: bool
    token_type: str | None = None
    expires_at: datetime | None = None


__all__ = [
    "CredentialCheck",
    "CredentialsStoreRequest",
    "InternalCredentials",
    "RedactedCredentials",
]
