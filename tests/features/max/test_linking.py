"""Tests for the MAX account-linking service.

Covers the full one-time-code lifecycle:

* ``generate_token`` returns a fresh, non-empty token for a user.
* ``link_account`` consumes a valid token and records the MAX user id.
* ``link_account`` is one-shot: a second call with the same token fails.
* ``link_account`` raises :class:`InvalidMaxLinkingTokenError` for an
  unknown token.
* ``link_account`` raises :class:`MaxAccountAlreadyLinkedError` when
  the ``max_user_id`` is already linked to a *different* local user.
* An expired token is rejected (``ttl_seconds=0`` with a tiny sleep).

The tests are intentionally no-Mock — the linking service is a
dict-backed state machine with zero external I/O, so a plain
:class:`MaxLinkingService` runs fast and gives back a faithful
behavioural surface.
"""

from __future__ import annotations

import time

import pytest

from apply_pilot.features.max.linking import (
    InvalidMaxLinkingTokenError,
    MaxAccountAlreadyLinkedError,
    MaxLinkingService,
)


@pytest.fixture
def linking_service() -> MaxLinkingService:
    """Return a fresh linking service with an empty in-memory token store."""
    return MaxLinkingService()


# ---------------------------------------------------------------------------
# generate_token
# ---------------------------------------------------------------------------


def test_generate_token_returns_non_empty_string(linking_service: MaxLinkingService) -> None:
    """``generate_token`` returns a non-empty string token."""
    token = linking_service.generate_token(user_id="user-1")
    assert isinstance(token, str)
    assert len(token) > 0


def test_generate_token_is_unique_per_call(linking_service: MaxLinkingService) -> None:
    """Two calls to ``generate_token`` produce distinct values (independent users)."""
    a = linking_service.generate_token(user_id="user-a")
    b = linking_service.generate_token(user_id="user-b")
    assert a != b


def test_generate_token_replaces_previous_token_for_same_user(
    linking_service: MaxLinkingService,
) -> None:
    """A fresh token for the same user invalidates the previous one."""
    first = linking_service.generate_token(user_id="user-1")
    second = linking_service.generate_token(user_id="user-1")

    assert first != second
    # The first token must no longer resolve to a user.
    assert linking_service.get_user_id_for_token(first) is None
    # The second token still does.
    assert linking_service.get_user_id_for_token(second) == "user-1"


# ---------------------------------------------------------------------------
# link_account
# ---------------------------------------------------------------------------


def test_link_account_returns_user_id(linking_service: MaxLinkingService) -> None:
    """A valid token returns the local user id it was issued for."""
    token = linking_service.generate_token(user_id="user-1")

    result = linking_service.link_account(token=token, max_user_id=123456789)

    assert result == "user-1"


def test_link_account_is_one_shot(linking_service: MaxLinkingService) -> None:
    """A second ``link_account`` with the same token raises :class:`InvalidMaxLinkingTokenError`."""
    token = linking_service.generate_token(user_id="user-1")
    linking_service.link_account(token=token, max_user_id=111)

    with pytest.raises(InvalidMaxLinkingTokenError):
        linking_service.link_account(token=token, max_user_id=222)


def test_link_account_with_unknown_token_raises(
    linking_service: MaxLinkingService,
) -> None:
    """An unknown token is rejected as :class:`InvalidMaxLinkingTokenError`."""
    with pytest.raises(InvalidMaxLinkingTokenError):
        linking_service.link_account(token="no-such-token", max_user_id=999)


def test_link_account_with_expired_token_raises(
    linking_service: MaxLinkingService,
) -> None:
    """A token past its TTL is rejected (issue #177: 10-minute default)."""
    token = linking_service.generate_token(user_id="user-1", ttl_seconds=0)
    # Force expiry by sleeping past the zero-second TTL.
    time.sleep(0.01)

    with pytest.raises(InvalidMaxLinkingTokenError):
        linking_service.link_account(token=token, max_user_id=999)


def test_link_account_rejects_duplicate_max_user_id(
    linking_service: MaxLinkingService,
) -> None:
    """A ``max_user_id`` cannot be linked to two different local users.

    The duplicate guard lives in :class:`MaxLinkingService` itself
    (the ``_max_to_user`` index), so the test exercises the linking
    service state directly without seeding an account repository.
    """
    # First link: user-1 <-> 100.
    token1 = linking_service.generate_token(user_id="user-1")
    linking_service.link_account(token=token1, max_user_id=100)

    # Second link attempt: user-2 tries to claim the same max_user_id.
    token2 = linking_service.generate_token(user_id="user-2")
    with pytest.raises(MaxAccountAlreadyLinkedError):
        linking_service.link_account(token=token2, max_user_id=100)


def test_link_account_allows_same_user_relink_with_fresh_token(
    linking_service: MaxLinkingService,
) -> None:
    """The same (user, max_user_id) pair can re-link with a fresh token.

    The duplicate guard only fires for a *different* user, not the same
    user. Issuing a fresh token keeps the loop testable.
    """
    token1 = linking_service.generate_token(user_id="user-1")
    linking_service.link_account(token=token1, max_user_id=100)

    token2 = linking_service.generate_token(user_id="user-1")
    result = linking_service.link_account(token=token2, max_user_id=100)

    assert result == "user-1"


# ---------------------------------------------------------------------------
# Reverse lookups
# ---------------------------------------------------------------------------


def test_find_max_user_id_returns_none_for_unlinked(
    linking_service: MaxLinkingService,
) -> None:
    """``find_max_user_id`` returns ``None`` for a user that has no link."""
    assert linking_service.find_max_user_id(user_id="unknown-user") is None


def test_find_max_user_id_returns_id_for_linked(
    linking_service: MaxLinkingService,
) -> None:
    """After linking, ``find_max_user_id`` returns the MAX user id."""
    token = linking_service.generate_token(user_id="user-1")
    linking_service.link_account(token=token, max_user_id=12345)

    assert linking_service.find_max_user_id(user_id="user-1") == 12345


def test_find_user_id_returns_none_for_unlinked(
    linking_service: MaxLinkingService,
) -> None:
    """``find_user_id`` returns ``None`` for a MAX user id that has no link."""
    assert linking_service.find_user_id(max_user_id=999_999) is None


def test_find_user_id_returns_id_for_linked(
    linking_service: MaxLinkingService,
) -> None:
    """After linking, ``find_user_id`` returns the local user id."""
    token = linking_service.generate_token(user_id="user-1")
    linking_service.link_account(token=token, max_user_id=12345)

    assert linking_service.find_user_id(max_user_id=12345) == "user-1"


# ---------------------------------------------------------------------------
# get_user_id_for_token (used by the API endpoint that resolves tokens)
# ---------------------------------------------------------------------------


def test_get_user_id_for_token_returns_holder_before_consumption(
    linking_service: MaxLinkingService,
) -> None:
    """``get_user_id_for_token`` resolves the user id behind a live token."""
    token = linking_service.generate_token(user_id="user-1")

    assert linking_service.get_user_id_for_token(token) == "user-1"


def test_get_user_id_for_token_returns_user_id_after_consumption(
    linking_service: MaxLinkingService,
) -> None:
    """A consumed token still resolves to the user_id it was issued for.

    The implementation deliberately does NOT delete the record on
    consumption — the API endpoint needs to know which user owns a
    token. The one-shot semantics live on :attr:`_MaxTokenRecord.consumed`
    and are enforced by :meth:`link_account` (a second call with the
    same token raises :class:`InvalidMaxLinkingTokenError`).
    """
    token = linking_service.generate_token(user_id="user-1")
    linking_service.link_account(token=token, max_user_id=42)

    # The user_id is still retrievable even after the token is consumed.
    assert linking_service.get_user_id_for_token(token) == "user-1"

    # But a second consumption attempt must fail with the explicit message.
    with pytest.raises(InvalidMaxLinkingTokenError, match="already used"):
        linking_service.link_account(token=token, max_user_id=43)
