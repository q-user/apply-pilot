"""TDD tests for the auth service use cases.

These tests are TDD-first: they describe the behaviour the auth slice
must deliver end-to-end through the service layer. We deliberately use
an in-memory fake repository instead of a real database so the slice
contract is exercised without an external dependency.
"""

from __future__ import annotations

import pytest

from job_apply.features.users.repository import InMemoryUsersRepository
from job_apply.features.users.schemas import UserCreate
from job_apply.features.users.security import InvalidTokenError
from job_apply.features.users.service import (
    AuthenticationError,
    AuthService,
    DuplicateEmailError,
)


@pytest.fixture
def service() -> AuthService:
    """Return an auth service backed by an in-memory repository."""
    return AuthService(users_repo=InMemoryUsersRepository())


def test_register_creates_user(service: AuthService) -> None:
    """A new email/password pair must produce an active user with a UUID id."""
    result = service.register(UserCreate(email="alice@example.com", password="hunter2!!"))

    assert result.is_active is True
    assert result.email == "alice@example.com"
    assert result.id  # truthy UUID string


def test_register_does_not_store_plaintext_password(service: AuthService) -> None:
    """The persisted user must carry a hashed password, not the plaintext."""
    service.register(UserCreate(email="bob@example.com", password="hunter2!!"))

    stored = service.users_repo.get_by_email("bob@example.com")
    assert stored is not None
    assert stored.hashed_password != "hunter2!!"


def test_register_duplicate_email_returns_conflict(service: AuthService) -> None:
    """A second registration for the same email must raise DuplicateEmailError."""
    service.register(UserCreate(email="alice@example.com", password="hunter2!!"))

    with pytest.raises(DuplicateEmailError):
        service.register(UserCreate(email="alice@example.com", password="different-pw"))


def test_register_normalizes_email_lowercase(service: AuthService) -> None:
    """Email comparison must be case-insensitive (stored normalised lowercase)."""
    service.register(UserCreate(email="Alice@Example.com", password="hunter2!!"))

    stored = service.users_repo.get_by_email("alice@example.com")
    assert stored is not None


def test_login_with_correct_password_returns_token(service: AuthService) -> None:
    """A successful login must return a valid session token for that user."""
    service.register(UserCreate(email="carol@example.com", password="hunter2!!"))

    token = service.login(email="carol@example.com", password="hunter2!!")

    user_id = service.resolve_user_id_from_token(token.access_token)
    assert user_id  # truthy UUID


def test_login_with_wrong_password_raises(service: AuthService) -> None:
    """Logging in with the wrong password must raise AuthenticationError."""
    service.register(UserCreate(email="dan@example.com", password="hunter2!!"))

    with pytest.raises(AuthenticationError):
        service.login(email="dan@example.com", password="WRONG")


def test_login_with_unknown_email_raises(service: AuthService) -> None:
    """Logging in with an unknown email must raise AuthenticationError."""
    with pytest.raises(AuthenticationError):
        service.login(email="ghost@example.com", password="whatever")


def test_get_me_returns_user_for_valid_token(service: AuthService) -> None:
    """resolve_user_id_from_token + get_user must return the registered user."""
    registered = service.register(UserCreate(email="erin@example.com", password="hunter2!!"))
    token = service.login(email="erin@example.com", password="hunter2!!")

    user_id = service.resolve_user_id_from_token(token.access_token)
    me = service.get_user(user_id=user_id)

    assert me.id == registered.id
    assert me.email == "erin@example.com"


def test_get_me_with_invalid_token_raises(service: AuthService) -> None:
    """An unknown token must raise InvalidTokenError."""
    with pytest.raises(InvalidTokenError):
        service.resolve_user_id_from_token("not-a-real-token")


def test_logout_invalidates_token(service: AuthService) -> None:
    """After logout, the same token must no longer resolve to a user."""
    service.register(UserCreate(email="frank@example.com", password="hunter2!!"))
    token = service.login(email="frank@example.com", password="hunter2!!")

    service.logout(token.access_token)

    with pytest.raises(InvalidTokenError):
        service.resolve_user_id_from_token(token.access_token)
