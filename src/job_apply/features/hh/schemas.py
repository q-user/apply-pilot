"""DTOs for the HH slice.

Two layers of DTOs live here:

* The credentials DTOs (request/response for ``/hh/credentials``) and
  the internal-vs-redacted credential value objects used by the
  service layer.
* :class:`HhResumeLinkDTO` and the ``/hh/resumes`` response envelopes
  used by the resume metadata sync endpoint.

The public DTOs that touch token material use string redaction in their
``__repr__`` / ``__str__`` so tokens never leak into structured logs or
error messages. The raw token values are only available through
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


# ---------------------------------------------------------------------------
# Resume metadata DTOs (issue #21)
# ---------------------------------------------------------------------------


class HhResumeLinkDTO(BaseModel):
    """Public representation of a :class:`HhResumeLink` row.

    Built from the ORM model via :class:`ConfigDict.from_attributes`. The
    ``local_resume_id`` is ``None`` until full-text fetch (M2+'s next
    slice) links the hh resume to a local :class:`Resume` row.
    """

    model_config = ConfigDict(extra="forbid", frozen=False, from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    local_resume_id: uuid.UUID | None = None
    hh_resume_id: str = Field(max_length=50, description="hh.ru's external resume id.")
    title: str | None = Field(default=None, max_length=255)
    updated_at_hh: datetime | None = None
    last_synced_at: datetime | None = None
    created_at: datetime
    updated_at: datetime | None = None


class HhResumesListResponse(BaseModel):
    """Response envelope for ``GET /hh/resumes``."""

    model_config = ConfigDict(extra="forbid", frozen=False)

    items: list[HhResumeLinkDTO]


class HhResumesSyncResponse(BaseModel):
    """Response envelope for ``POST /hh/resumes/sync``.

    ``synced_count`` is the number of rows written (inserts + updates)
    so clients can show "synced N resumes" without re-counting ``items``.
    """

    model_config = ConfigDict(extra="forbid", frozen=False)

    items: list[HhResumeLinkDTO]
    synced_count: int = Field(ge=0)


__all__ = [
    "CredentialCheck",
    "CredentialsStoreRequest",
    "HhResumeLinkDTO",
    "HhResumesListResponse",
    "HhResumesSyncResponse",
    "InternalCredentials",
    "RedactedCredentials",
]
