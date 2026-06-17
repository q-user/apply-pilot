"""DTOs for the auth slice.

We do not reuse :class:`apply_pilot.shared.schemas.IdentifiedSchema`
because the shared base assumes an ``int`` id; the auth slice uses a
``UUID``. We do reuse :class:`TimestampedSchema` for the
``created_at`` / ``updated_at`` pair.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from apply_pilot.shared.schemas import TimestampedSchema

# EmailStr requires the ``email-validator`` extra. To stay
# dependency-free for M1 we validate the email shape with a plain
# regex through Pydantic`'s ``pattern=`` constraint instead.
EmailAddress = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        to_lower=True,
        min_length=3,
        max_length=255,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    ),
]

Password = Annotated[
    str,
    StringConstraints(min_length=8, max_length=128, strip_whitespace=False),
]


class UserCreate(BaseModel):
    """Input for ``POST /auth/register``."""

    model_config = ConfigDict(extra="forbid", frozen=False)

    email: EmailAddress
    password: Password


class UserLogin(BaseModel):
    """Input for ``POST /auth/login``."""

    model_config = ConfigDict(extra="forbid", frozen=False)

    email: EmailAddress
    password: Password


class UserRead(TimestampedSchema):
    """Output shape for user resources.

    The ``id`` is a UUID; Pydantic renders it as a canonical string in
    JSON responses.
    """

    model_config = ConfigDict(extra="forbid", frozen=False)

    id: uuid.UUID = Field(description="Stable identifier of the user.")
    email: EmailAddress
    is_active: bool = Field(default=True)


class AuthToken(BaseModel):
    """Output shape for ``POST /auth/login`` (bearer token only)."""

    model_config = ConfigDict(extra="forbid", frozen=False)

    access_token: str = Field(min_length=1)
    token_type: str = Field(default="bearer")


class AuthenticatedUser(BaseModel):
    """Output for ``POST /auth/login``: token + the user payload."""

    model_config = ConfigDict(extra="forbid", frozen=False)

    access_token: str
    token_type: str = "bearer"
    user: UserRead


__all__ = [
    "AuthToken",
    "AuthenticatedUser",
    "EmailAddress",
    "Password",
    "UserCreate",
    "UserLogin",
    "UserRead",
]
