"""Search profiles business logic.

The service owns validation rules (title required, salary range sanity),
authorisation checks (profile ownership), and the mapping between ORM rows
and public DTOs.

It accepts a ``SearchProfileRepository`` through constructor injection so
tests can supply the in-memory fake while production wiring uses the
SQLAlchemy-backed version.
"""

from __future__ import annotations

import uuid

from job_apply.features.search_profiles.models import SearchProfile
from job_apply.features.search_profiles.repository import SearchProfileRepository
from job_apply.features.search_profiles.schemas import (
    SearchProfileCreate,
    SearchProfileRead,
    SearchProfileUpdate,
)
from job_apply.shared.errors import NotFoundError, ValidationError


class ProfileNotFoundError(NotFoundError):
    """The requested search profile does not exist."""

    code: str = "search_profile_not_found"


class ProfileOwnershipError(Exception):
    """The caller does not own the requested search profile.

    Raises as a plain ``Exception`` (not ``DomainError``) so the HTTP layer
    always returns 403 regardless of error-code evolution.
    """

    code: str = "forbidden"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _profile_to_dto(profile: SearchProfile) -> SearchProfileRead:
    """Map an ORM ``SearchProfile`` row to a public ``SearchProfileRead`` DTO."""
    return SearchProfileRead(
        id=profile.id,
        user_id=profile.user_id,
        title=profile.title,
        keywords=profile.keywords,
        salary_min=profile.salary_min,
        salary_max=profile.salary_max,
        location=profile.location,
        schedule=profile.schedule,
        is_active=profile.is_active,
        is_preferred=profile.is_preferred,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


class SearchProfileService:
    """CRUD operations for job-search profiles."""

    def __init__(self, repository: SearchProfileRepository) -> None:
        self._repo = repository

    @property
    def repo(self) -> SearchProfileRepository:
        """Expose the repository for tests that need to assert state."""
        return self._repo

    def create(self, payload: SearchProfileCreate, *, user_id: uuid.UUID) -> SearchProfileRead:
        """Create a new search profile for ``user_id``."""
        profile = SearchProfile(
            user_id=user_id,
            title=payload.title,
            keywords=payload.keywords,
            salary_min=payload.salary_min,
            salary_max=payload.salary_max,
            location=payload.location,
            schedule=payload.schedule,
        )
        created = self._repo.create(profile)
        return _profile_to_dto(created)

    def get(self, profile_id: uuid.UUID, *, user_id: uuid.UUID) -> SearchProfileRead:
        """Return a single profile, raising if not found or not owned."""
        profile = self._repo.get_by_id(profile_id)
        if profile is None:
            raise ProfileNotFoundError(f"search profile {profile_id} not found")
        if profile.user_id != user_id:
            raise ProfileOwnershipError(
                f"search profile {profile_id} does not belong to user {user_id}"
            )
        return _profile_to_dto(profile)

    def list_by_user(self, user_id: uuid.UUID) -> list[SearchProfileRead]:
        """Return all search profiles owned by ``user_id``."""
        profiles = self._repo.list_by_user(user_id)
        return [_profile_to_dto(p) for p in profiles]

    def update(
        self,
        profile_id: uuid.UUID,
        payload: SearchProfileUpdate,
        *,
        user_id: uuid.UUID,
    ) -> SearchProfileRead:
        """Update fields on an existing search profile."""
        profile = self._repo.get_by_id(profile_id)
        if profile is None:
            raise ProfileNotFoundError(f"search profile {profile_id} not found")
        if profile.user_id != user_id:
            raise ProfileOwnershipError(
                f"search profile {profile_id} does not belong to user {user_id}"
            )

        # Apply only the fields that were provided (not None meaning "not set").
        # For salary fields we must validate the resulting pair.
        update_data = payload.model_dump(exclude_unset=True)

        if "title" in update_data and update_data["title"] is not None:
            profile.title = update_data["title"]
        if "keywords" in update_data:
            profile.keywords = update_data["keywords"]
        if "location" in update_data:
            profile.location = update_data["location"]
        if "schedule" in update_data:
            profile.schedule = update_data["schedule"]
        if "is_active" in update_data:
            profile.is_active = update_data["is_active"]

        # Salary fields: apply individually, then validate the pair.
        if "salary_min" in update_data:
            profile.salary_min = update_data["salary_min"]
        if "salary_max" in update_data:
            profile.salary_max = update_data["salary_max"]
        # Re-read after applying to handle the case where only one was updated.
        effective_min = profile.salary_min
        effective_max = profile.salary_max
        if (
            effective_min is not None
            and effective_max is not None
            and effective_min > effective_max
        ):
            raise ValidationError("salary_min must be <= salary_max")

        updated = self._repo.update(profile)
        return _profile_to_dto(updated)

    def delete(self, profile_id: uuid.UUID, *, user_id: uuid.UUID) -> None:
        """Delete a search profile (hard delete)."""
        profile = self._repo.get_by_id(profile_id)
        if profile is None:
            raise ProfileNotFoundError(f"search profile {profile_id} not found")
        if profile.user_id != user_id:
            raise ProfileOwnershipError(
                f"search profile {profile_id} does not belong to user {user_id}"
            )
        self._repo.delete(profile)

    def set_active(
        self,
        profile_id: uuid.UUID,
        *,
        active: bool,
        user_id: uuid.UUID,
    ) -> SearchProfileRead:
        """Toggle the ``is_active`` flag on a profile owned by ``user_id``.

        Used by ``POST /search-profiles/{id}/activate`` and
        ``POST /search-profiles/{id}/deactivate`` so the HTTP layer does
        not need a separate handler per direction.
        """
        profile = self._repo.get_by_id(profile_id)
        if profile is None:
            raise ProfileNotFoundError(f"search profile {profile_id} not found")
        if profile.user_id != user_id:
            raise ProfileOwnershipError(
                f"search profile {profile_id} does not belong to user {user_id}"
            )
        profile.is_active = active
        updated = self._repo.update(profile)
        return _profile_to_dto(updated)

    def get_preferred(self, user_id: uuid.UUID) -> SearchProfileRead | None:
        """Return the user's "preferred" search profile, or ``None`` if none
        is flagged ``is_preferred=True``.

        The ``is_preferred`` flag is added in this milestone (M6, #53) as a
        data-model placeholder; the dedicated "set preferred profile"
        endpoint will land in a follow-up issue. Until then every user
        gets ``None`` and the HTTP layer translates that into ``404``.
        """
        for profile in self._repo.list_by_user(user_id):
            if profile.is_preferred:
                return _profile_to_dto(profile)
        return None


__all__ = [
    "ProfileNotFoundError",
    "ProfileOwnershipError",
    "SearchProfileService",
]
