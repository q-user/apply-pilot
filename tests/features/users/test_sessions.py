"""TDD tests for session persistence (M1, issue #12).

These tests describe the session CRUD contract and the AuthService
behaviour when sessions are persisted. We use in-memory repos via DI
to avoid external database dependencies.
"""

from __future__ import annotations

import pytest

from apply_pilot.features.users.repository import (
    InMemoryUserSessionRepository,
    InMemoryUsersRepository,
)
from apply_pilot.features.users.schemas import UserCreate
from apply_pilot.features.users.security import InvalidTokenError
from apply_pilot.features.users.service import (
    AuthenticationError,
    AuthService,
)


@pytest.fixture
def service() -> AuthService:
    """Return an auth service with in-memory user + session repos."""
    return AuthService(
        users_repo=InMemoryUsersRepository(),
        sessions_repo=InMemoryUserSessionRepository(),
    )


# ---------------------------------------------------------------------------
# Session creation
# ---------------------------------------------------------------------------


def test_login_creates_session(service: AuthService) -> None:
    """After login, a session record must exist for the issued token."""
    service.register(UserCreate(email="alice@example.com", password="hunter2!!"))

    result = service.login(email="alice@example.com", password="hunter2!!")

    # The session repo must contain exactly one session for this user.
    sessions = list(service.sessions_repo.list_by_user_id(result.user.id))
    assert len(sessions) == 1
    assert sessions[0].revoked_at is None


def test_login_creates_distinct_sessions(service: AuthService) -> None:
    """Two logins for the same user must create two separate session rows."""
    service.register(UserCreate(email="bob@example.com", password="hunter2!!"))

    first = service.login(email="bob@example.com", password="hunter2!!")
    second = service.login(email="bob@example.com", password="hunter2!!")

    sessions = list(service.sessions_repo.list_by_user_id(first.user.id))
    assert len(sessions) == 2
    assert first.access_token != second.access_token


# ---------------------------------------------------------------------------
# Token resolution validates sessions
# ---------------------------------------------------------------------------


def test_resolve_token_validates_active_session(service: AuthService) -> None:
    """A token backed by an active session must resolve to the user id."""
    service.register(UserCreate(email="carol@example.com", password="hunter2!!"))
    result = service.login(email="carol@example.com", password="hunter2!!")

    user_id = service.resolve_user_id_from_token(result.access_token)
    assert user_id == result.user.id


def test_revoked_session_rejects_token(service: AuthService) -> None:
    """A token whose session has been revoked must raise InvalidTokenError."""
    service.register(UserCreate(email="dan@example.com", password="hunter2!!"))
    result = service.login(email="dan@example.com", password="hunter2!!")

    # Simulate revocation (logout path)
    service.logout(result.access_token)

    with pytest.raises(InvalidTokenError):
        service.resolve_user_id_from_token(result.access_token)


def test_unknown_token_raises(service: AuthService) -> None:
    """A token with no corresponding session must raise InvalidTokenError."""
    with pytest.raises(InvalidTokenError):
        service.resolve_user_id_from_token("not-a-real-token")


def test_expired_session_rejects_token(service: AuthService) -> None:
    """A token whose session has expired must raise InvalidTokenError.

    We test this by using a zero-second TTL so the session is already
    expired the moment it is created.
    """
    service.register(UserCreate(email="erin@example.com", password="hunter2!!"))

    # Use a zero TTL for login, then re-create the service with
    # normal TTL for resolution (so the service doesn't use expiry
    # to reject the token — it should be the session repo doing it).
    short_service = AuthService(
        users_repo=service.users_repo,
        sessions_repo=service.sessions_repo,
        token_ttl_seconds=0,
    )
    result = short_service.login(email="erin@example.com", password="hunter2!!")

    with pytest.raises(InvalidTokenError):
        service.resolve_user_id_from_token(result.access_token)


# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------


def test_refresh_returns_new_token(service: AuthService) -> None:
    """POST /auth/refresh must return a new access token."""
    service.register(UserCreate(email="frank@example.com", password="hunter2!!"))
    result = service.login(email="frank@example.com", password="hunter2!!")

    refreshed = service.refresh_token(result.access_token)

    assert refreshed.access_token
    assert refreshed.access_token != result.access_token
    assert refreshed.token_type == "bearer"


def test_refreshed_token_is_valid(service: AuthService) -> None:
    """The token returned by refresh must be usable for authentication."""
    service.register(UserCreate(email="grace@example.com", password="hunter2!!"))
    result = service.login(email="grace@example.com", password="hunter2!!")

    refreshed = service.refresh_token(result.access_token)

    user_id = service.resolve_user_id_from_token(refreshed.access_token)
    assert user_id == result.user.id


def test_old_token_invalid_after_refresh(service: AuthService) -> None:
    """After refresh, the old token must no longer work."""
    service.register(UserCreate(email="heidi@example.com", password="hunter2!!"))
    result = service.login(email="heidi@example.com", password="hunter2!!")

    service.refresh_token(result.access_token)

    with pytest.raises(InvalidTokenError):
        service.resolve_user_id_from_token(result.access_token)


def test_refresh_with_invalid_token_raises(service: AuthService) -> None:
    """Refreshing with an unknown or revoked token must raise an error."""
    with pytest.raises(AuthenticationError):
        service.refresh_token("not-a-real-token")


def test_refresh_preserves_session_count(service: AuthService) -> None:
    """After refresh, the total session count for the user should be 1
    (old session revoked, one new session created)."""
    service.register(UserCreate(email="ivan@example.com", password="hunter2!!"))
    result = service.login(email="ivan@example.com", password="hunter2!!")

    service.refresh_token(result.access_token)

    sessions = list(service.sessions_repo.list_by_user_id(result.user.id))
    active = [s for s in sessions if s.revoked_at is None]
    assert len(active) == 1


# ---------------------------------------------------------------------------
# Logout revokes session
# ---------------------------------------------------------------------------


def test_logout_revokes_session_in_repo(service: AuthService) -> None:
    """After logout, the session must have revoked_at set."""
    service.register(UserCreate(email="judy@example.com", password="hunter2!!"))
    result = service.login(email="judy@example.com", password="hunter2!!")

    service.logout(result.access_token)

    sessions = list(service.sessions_repo.list_by_user_id(result.user.id))
    assert len(sessions) == 1
    assert sessions[0].revoked_at is not None


def test_logout_is_idempotent(service: AuthService) -> None:
    """Calling logout twice on the same token must not raise."""
    service.register(UserCreate(email="karl@example.com", password="hunter2!!"))
    result = service.login(email="karl@example.com", password="hunter2!!")

    service.logout(result.access_token)
    # Must not raise
    service.logout(result.access_token)


def test_logout_unknown_token_does_not_raise(service: AuthService) -> None:
    """Logging out with an unknown token is a no-op."""
    # Must not raise
    service.logout("not-a-real-token")
