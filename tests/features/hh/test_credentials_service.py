"""TDD tests for the HH credential service use cases.

These tests exercise :class:`HHCredentialService` end-to-end through an
in-memory repository and encryptor, without touching a real database.
Token redaction is verified — no access/refresh token value must appear
in service responses or DTO string representations.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet

from apply_pilot.features.hh.encryption import CredentialEncryptor
from apply_pilot.features.hh.repository import InMemoryHHCredentialRepository
from apply_pilot.features.hh.schemas import CredentialCheck, RedactedCredentials
from apply_pilot.features.hh.service import HHCredentialService
from apply_pilot.shared.errors import NotFoundError

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def repo() -> InMemoryHHCredentialRepository:
    """Fresh in-memory repository for each test."""
    return InMemoryHHCredentialRepository()


@pytest.fixture
def encryptor() -> CredentialEncryptor:
    """Encryptor with a fresh key for each test."""
    return CredentialEncryptor(key=Fernet.generate_key())


@pytest.fixture
def service(
    repo: InMemoryHHCredentialRepository,
    encryptor: CredentialEncryptor,
) -> HHCredentialService:
    """Service wired to in-memory repo + fresh encryptor."""
    return HHCredentialService(repo=repo, encryptor=encryptor)


@pytest.fixture
def user_id() -> uuid.UUID:
    """Stable user id for tests."""
    return uuid.uuid4()


# ---------------------------------------------------------------------------
# Store + get + delete workflow
# ---------------------------------------------------------------------------


def test_store_credentials_succeeds(service: HHCredentialService, user_id: uuid.UUID) -> None:
    """Storing credentials for a new user must return a RedactedCredentials DTO
    with metadata but no raw tokens."""
    result = service.store_credentials(
        user_id=user_id,
        access_token="acc-abc",
        refresh_token="ref-xyz",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    assert isinstance(result, RedactedCredentials)
    assert result.user_id == user_id
    assert result.token_type == "bearer"
    assert result.expires_at is not None
    # No raw tokens should leak
    assert "acc-abc" not in str(result)
    assert "ref-xyz" not in str(result)
    assert result.access_token == "REDACTED"
    assert result.refresh_token == "REDACTED"


def test_get_credentials_recovers_tokens(service: HHCredentialService, user_id: uuid.UUID) -> None:
    """get_credentials() must return the decrypted tokens (raw, for internal use),
    but the DTO string repr must still redact them."""
    service.store_credentials(
        user_id=user_id,
        access_token="acc-123",
        refresh_token="ref-456",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    result = service.get_credentials(user_id)

    assert result.access_token == "acc-123"
    assert result.refresh_token == "ref-456"
    assert result.token_type == "bearer"
    assert result.user_id == user_id
    # String representation must redact
    assert "acc-123" not in str(result)
    assert "ref-456" not in str(result)


def test_get_credentials_raises_not_found(service: HHCredentialService, user_id: uuid.UUID) -> None:
    """get_credentials() must raise NotFoundError for a user with no credentials."""
    with pytest.raises(NotFoundError, match="HH credentials"):
        service.get_credentials(user_id)


def test_delete_credentials_removes(service: HHCredentialService, user_id: uuid.UUID) -> None:
    """delete_credentials() must remove stored credentials."""
    service.store_credentials(
        user_id=user_id,
        access_token="acc-del",
        refresh_token=None,
        expires_at=None,
    )
    service.delete_credentials(user_id)

    with pytest.raises(NotFoundError):
        service.get_credentials(user_id)


def test_delete_credentials_missing_is_noop(
    service: HHCredentialService, user_id: uuid.UUID
) -> None:
    """Deleting non-existent credentials must not raise (idempotent)."""
    # Should not raise
    service.delete_credentials(user_id)


def test_store_overwrites_existing(service: HHCredentialService, user_id: uuid.UUID) -> None:
    """Storing credentials twice for the same user must update (not duplicate)."""
    service.store_credentials(
        user_id=user_id,
        access_token="first-token",
        refresh_token=None,
        expires_at=None,
    )
    service.store_credentials(
        user_id=user_id,
        access_token="second-token",
        refresh_token="new-refresh",
        expires_at=datetime.now(UTC) + timedelta(hours=2),
    )

    result = service.get_credentials(user_id)
    assert result.access_token == "second-token"
    assert result.refresh_token == "new-refresh"


def test_store_credentials_without_refresh_token(
    service: HHCredentialService, user_id: uuid.UUID
) -> None:
    """Storing credentials with refresh_token=None must be allowed."""
    result = service.store_credentials(
        user_id=user_id,
        access_token="just-access",
        refresh_token=None,
        expires_at=None,
    )
    assert result.access_token == "REDACTED"
    assert result.refresh_token == "REDACTED"


def test_check_credentials_exists(service: HHCredentialService, user_id: uuid.UUID) -> None:
    """check_credentials() must return metadata without tokens."""
    expires = datetime.now(UTC) + timedelta(hours=1)
    service.store_credentials(
        user_id=user_id,
        access_token="secret-acc",
        refresh_token="secret-ref",
        expires_at=expires,
    )

    result = service.check_credentials(user_id)

    assert isinstance(result, CredentialCheck)
    assert result.has_credentials is True
    assert result.token_type == "bearer"
    assert result.expires_at == expires
    # Raw token values must not be accessible via CredentialCheck
    assert not hasattr(result, "access_token")
    assert not hasattr(result, "refresh_token")


def test_check_credentials_not_found(service: HHCredentialService, user_id: uuid.UUID) -> None:
    """check_credentials() for a user with no credentials returns has_credentials=False."""
    result = service.check_credentials(user_id)
    assert result.has_credentials is False
    assert result.token_type is None
    assert result.expires_at is None
