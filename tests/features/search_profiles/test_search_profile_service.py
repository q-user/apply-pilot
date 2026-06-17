"""TDD tests for the search profile service use cases.

These tests describe the behaviour the search_profiles slice must deliver
through the service layer. We use the in-memory repository so the slice
contract is exercised without an external database.
"""

from __future__ import annotations

import uuid

import pytest

from apply_pilot.features.search_profiles.repository import InMemorySearchProfileRepository
from apply_pilot.features.search_profiles.schemas import (
    SearchProfileCreate,
    SearchProfileUpdate,
)
from apply_pilot.features.search_profiles.service import (
    ProfileNotFoundError,
    ProfileOwnershipError,
    SearchProfileService,
)
from apply_pilot.shared.errors import ValidationError


@pytest.fixture
def repo() -> InMemorySearchProfileRepository:
    return InMemorySearchProfileRepository()


@pytest.fixture
def service(repo: InMemorySearchProfileRepository) -> SearchProfileService:
    return SearchProfileService(repo)


@pytest.fixture
def user_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def other_user_id() -> uuid.UUID:
    return uuid.uuid4()


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


def test_create_returns_profile_with_id(service: SearchProfileService, user_id: uuid.UUID) -> None:
    """Creating a profile must return a DTO with a UUID id and the owner's user_id."""
    result = service.create(
        SearchProfileCreate(title="Python dev", keywords="django fastapi"),
        user_id=user_id,
    )

    assert result.id
    assert result.user_id == user_id
    assert result.title == "Python dev"
    assert result.keywords == "django fastapi"
    assert result.is_active is True


def test_create_with_minimal_fields(service: SearchProfileService, user_id: uuid.UUID) -> None:
    """Only title is required; other fields default to None."""
    result = service.create(SearchProfileCreate(title="any"), user_id=user_id)

    assert result.title == "any"
    assert result.keywords is None
    assert result.salary_min is None
    assert result.salary_max is None
    assert result.location is None
    assert result.schedule is None


def test_create_with_salary_range(service: SearchProfileService, user_id: uuid.UUID) -> None:
    """Salary fields must be stored as provided."""
    result = service.create(
        SearchProfileCreate(title="dev", salary_min=50000, salary_max=100000),
        user_id=user_id,
    )

    assert result.salary_min == 50000
    assert result.salary_max == 100000


def test_create_invalid_salary_range_raises(user_id: uuid.UUID) -> None:
    """Pydantic validation must reject salary_min > salary_max before reaching the service."""
    with pytest.raises(ValueError, match="salary_min"):
        SearchProfileCreate(title="dev", salary_min=100000, salary_max=50000)


# ---------------------------------------------------------------------------
# Get
# ---------------------------------------------------------------------------


def test_get_returns_profile(service: SearchProfileService, user_id: uuid.UUID) -> None:
    """Getting a profile by id must return the correct DTO."""
    created = service.create(SearchProfileCreate(title="my profile"), user_id=user_id)

    result = service.get(created.id, user_id=user_id)

    assert result.id == created.id
    assert result.title == "my profile"


def test_get_unknown_profile_raises_not_found(
    service: SearchProfileService, user_id: uuid.UUID
) -> None:
    """Requesting a non-existent profile must raise ProfileNotFoundError."""
    with pytest.raises(ProfileNotFoundError):
        service.get(uuid.uuid4(), user_id=user_id)


def test_get_profile_of_other_user_raises_forbidden(
    service: SearchProfileService, user_id: uuid.UUID, other_user_id: uuid.UUID
) -> None:
    """A user must not be able to read another user's profile."""
    created = service.create(SearchProfileCreate(title="private"), user_id=user_id)

    with pytest.raises(ProfileOwnershipError):
        service.get(created.id, user_id=other_user_id)


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


def test_list_returns_only_own_profiles(
    service: SearchProfileService, user_id: uuid.UUID, other_user_id: uuid.UUID
) -> None:
    """Listing must only return profiles belonging to the requesting user."""
    mine = service.create(SearchProfileCreate(title="mine"), user_id=user_id)
    theirs = service.create(SearchProfileCreate(title="theirs"), user_id=other_user_id)

    result = service.list_by_user(user_id)

    ids = {p.id for p in result}
    assert mine.id in ids
    assert theirs.id not in ids


def test_list_empty_for_new_user(service: SearchProfileService, user_id: uuid.UUID) -> None:
    """A user with no profiles must get an empty list."""
    assert service.list_by_user(user_id) == []


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


def test_update_title(service: SearchProfileService, user_id: uuid.UUID) -> None:
    """Updating the title must change only the title."""
    created = service.create(SearchProfileCreate(title="old"), user_id=user_id)

    updated = service.update(created.id, SearchProfileUpdate(title="new"), user_id=user_id)

    assert updated.id == created.id
    assert updated.title == "new"
    assert updated.keywords == created.keywords


def test_update_salary_range(service: SearchProfileService, user_id: uuid.UUID) -> None:
    """Updating salary fields must reflect the new values."""
    created = service.create(
        SearchProfileCreate(title="dev", salary_min=50000, salary_max=100000),
        user_id=user_id,
    )

    updated = service.update(
        created.id, SearchProfileUpdate(salary_min=60000, salary_max=90000), user_id=user_id
    )

    assert updated.salary_min == 60000
    assert updated.salary_max == 90000


def test_update_other_user_profile_raises_forbidden(
    service: SearchProfileService, user_id: uuid.UUID, other_user_id: uuid.UUID
) -> None:
    """A user must not be able to update another user's profile."""
    created = service.create(SearchProfileCreate(title="private"), user_id=user_id)

    with pytest.raises(ProfileOwnershipError):
        service.update(created.id, SearchProfileUpdate(title="hacked"), user_id=other_user_id)


def test_update_unknown_profile_raises_not_found(
    service: SearchProfileService, user_id: uuid.UUID
) -> None:
    """Updating a non-existent profile must raise ProfileNotFoundError."""
    with pytest.raises(ProfileNotFoundError):
        service.update(uuid.uuid4(), SearchProfileUpdate(title="x"), user_id=user_id)


def test_update_sets_updated_at(
    service: SearchProfileService, repo: InMemorySearchProfileRepository, user_id: uuid.UUID
) -> None:
    """An update must set the updated_at timestamp."""
    created = service.create(SearchProfileCreate(title="stale"), user_id=user_id)
    original_updated_at = created.updated_at

    updated = service.update(created.id, SearchProfileUpdate(title="fresh"), user_id=user_id)

    assert updated.updated_at is not None
    # In-memory repo sets updated_at during update
    assert updated.updated_at != original_updated_at or original_updated_at is None


def test_update_invalid_salary_range_raises_validation(
    service: SearchProfileService, user_id: uuid.UUID
) -> None:
    """Updating with salary_min > existing salary_max must raise ValidationError."""
    created = service.create(SearchProfileCreate(title="dev", salary_max=50000), user_id=user_id)

    with pytest.raises(ValidationError, match="salary_min"):
        service.update(
            created.id,
            SearchProfileUpdate(salary_min=100000),  # single field, passes Pydantic
            user_id=user_id,
        )


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


def test_delete_removes_profile(service: SearchProfileService, user_id: uuid.UUID) -> None:
    """After deletion, fetching the profile must raise ProfileNotFoundError."""
    created = service.create(SearchProfileCreate(title="to-delete"), user_id=user_id)

    service.delete(created.id, user_id=user_id)

    with pytest.raises(ProfileNotFoundError):
        service.get(created.id, user_id=user_id)


def test_delete_other_user_profile_raises_forbidden(
    service: SearchProfileService, user_id: uuid.UUID, other_user_id: uuid.UUID
) -> None:
    """A user must not be able to delete another user's profile."""
    created = service.create(SearchProfileCreate(title="private"), user_id=user_id)

    with pytest.raises(ProfileOwnershipError):
        service.delete(created.id, user_id=other_user_id)


def test_delete_unknown_profile_raises_not_found(
    service: SearchProfileService, user_id: uuid.UUID
) -> None:
    """Deleting a non-existent profile must raise ProfileNotFoundError."""
    with pytest.raises(ProfileNotFoundError):
        service.delete(uuid.uuid4(), user_id=user_id)
