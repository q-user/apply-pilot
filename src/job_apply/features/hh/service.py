"""HH credential use-case service.

The service is the only place where encryption, decryption, and ORM
model construction are combined. It never returns raw tokens to the
HTTP layer — :class:`RedactedCredentials` is used for public responses,
and :class:`InternalCredentials` is for internal callers.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from job_apply.features.hh.encryption import CredentialEncryptor
from job_apply.features.hh.models import HHCredential
from job_apply.features.hh.repository import HHCredentialRepository
from job_apply.features.hh.schemas import (
    CredentialCheck,
    InternalCredentials,
    RedactedCredentials,
)
from job_apply.shared.errors import NotFoundError


class HHCredentialService:
    """Encrypt, store, retrieve, and delete hh.ru OAuth credentials."""

    def __init__(
        self,
        *,
        repo: HHCredentialRepository,
        encryptor: CredentialEncryptor,
    ) -> None:
        self._repo = repo
        self._encryptor = encryptor

    def store_credentials(
        self,
        *,
        user_id: uuid.UUID,
        access_token: str,
        refresh_token: str | None = None,
        token_type: str = "bearer",
        expires_at: datetime | None = None,
    ) -> RedactedCredentials:
        """Encrypt and persist credentials for *user_id*.

        If credentials already exist for the user, they are overwritten.
        Returns a :class:`RedactedCredentials` DTO with no raw token material.
        """
        cred = HHCredential(
            user_id=user_id,
            encrypted_access_token=self._encryptor.encrypt(access_token),
            encrypted_refresh_token=(
                self._encryptor.encrypt(refresh_token) if refresh_token is not None else None
            ),
            token_type=token_type,
            expires_at=expires_at,
        )
        if cred.created_at is None:
            cred.created_at = datetime.now(UTC)

        stored = self._repo.store(cred)

        return RedactedCredentials(
            user_id=stored.user_id,
            token_type=stored.token_type,
            expires_at=stored.expires_at,
        )

    def get_credentials(self, user_id: uuid.UUID) -> InternalCredentials:
        """Retrieve and decrypt credentials for *user_id*.

        The returned :class:`InternalCredentials` carries the raw token
        values in its attributes, but ``__repr__`` / ``__str__`` redact them.

        Raises:
            NotFoundError: If no credentials are stored for *user_id*.
        """
        row = self._repo.get_by_user_id(user_id)
        if row is None:
            raise NotFoundError.for_entity("HH credentials", str(user_id))

        return InternalCredentials(
            user_id=row.user_id,
            access_token=self._encryptor.decrypt(row.encrypted_access_token),
            refresh_token=(
                self._encryptor.decrypt(row.encrypted_refresh_token)
                if row.encrypted_refresh_token is not None
                else None
            ),
            token_type=row.token_type,
            expires_at=row.expires_at,
        )

    def check_credentials(self, user_id: uuid.UUID) -> CredentialCheck:
        """Return metadata about stored credentials — never the tokens themselves.

        Callers that only need to know *whether* credentials exist (and,
        if so, their type and expiry) should use this method instead of
        :meth:`get_credentials`.
        """
        row = self._repo.get_by_user_id(user_id)
        if row is None:
            return CredentialCheck(has_credentials=False)
        return CredentialCheck(
            has_credentials=True,
            token_type=row.token_type,
            expires_at=row.expires_at,
        )

    def delete_credentials(self, user_id: uuid.UUID) -> None:
        """Remove stored credentials for *user_id*.

        Idempotent: does not raise if no credentials exist.
        """
        self._repo.delete(user_id)


__all__ = ["HHCredentialService"]
