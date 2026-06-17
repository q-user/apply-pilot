"""FastAPI router for the search_profiles slice.

Endpoints
---------

* ``POST /search-profiles`` — create a new search profile.
* ``GET /search-profiles`` — list profiles belonging to the caller.
* ``GET /search-profiles/preferred`` — return the user's "preferred" profile (M6 placeholder).
* ``GET /search-profiles/{id}`` — get a single profile.
* ``PUT /search-profiles/{id}`` — update a profile.
* ``DELETE /search-profiles/{id}`` — delete a profile.
* ``POST /search-profiles/{id}/activate`` — flip ``is_active`` to ``True``.
* ``POST /search-profiles/{id}/deactivate`` — flip ``is_active`` to ``False``.

All endpoints require a valid bearer token.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from apply_pilot.db import get_db
from apply_pilot.features.search_profiles.repository import SqlSearchProfileRepository
from apply_pilot.features.search_profiles.schemas import (
    SearchProfileCreate,
    SearchProfileRead,
    SearchProfileUpdate,
)
from apply_pilot.features.search_profiles.service import (
    ProfileNotFoundError,
    ProfileOwnershipError,
    SearchProfileService,
)
from apply_pilot.features.users.security import InvalidTokenError, default_token_store
from apply_pilot.shared.errors import ValidationError

_LOGGER = logging.getLogger("apply_pilot.features.search_profiles.api")

router = APIRouter(prefix="/search-profiles", tags=["search-profiles"])

_bearer_scheme = HTTPBearer(auto_error=False)


def _http_error(status_code: int, code: str, message: str) -> HTTPException:
    """Return a JSON-shaped 4xx error that the API contract promises."""
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _resolve_user_id(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),  # noqa: B008
) -> str:
    """Extract the user id from the bearer token, or raise 401."""
    if credentials is None:
        raise _http_error(
            status.HTTP_401_UNAUTHORIZED,
            "authentication_required",
            "bearer token is required",
        )
    tokens = default_token_store()
    try:
        return tokens.resolve(credentials.credentials)
    except InvalidTokenError as exc:
        raise _http_error(
            status.HTTP_401_UNAUTHORIZED,
            "invalid_token",
            "the supplied token is invalid or expired",
        ) from exc


def get_search_profile_service(
    session: Session = Depends(get_db),  # noqa: B008
) -> SearchProfileService:
    """Build a ``SearchProfileService`` for the current request."""
    repo = SqlSearchProfileRepository(session_factory=lambda: session)
    return SearchProfileService(repo)


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=SearchProfileRead,
    status_code=status.HTTP_201_CREATED,
    responses={
        401: {"description": "Missing or invalid bearer token"},
        422: {"description": "Validation error"},
    },
)
def create_profile(
    payload: SearchProfileCreate,
    user_id_str: str = Depends(_resolve_user_id),  # noqa: B008
    service: SearchProfileService = Depends(get_search_profile_service),  # noqa: B008
) -> SearchProfileRead:
    """Create a new search profile for the authenticated user."""
    import uuid

    return service.create(payload, user_id=uuid.UUID(user_id_str))


@router.get(
    "",
    response_model=list[SearchProfileRead],
    responses={
        401: {"description": "Missing or invalid bearer token"},
    },
)
def list_profiles(
    user_id_str: str = Depends(_resolve_user_id),  # noqa: B008
    service: SearchProfileService = Depends(get_search_profile_service),  # noqa: B008
) -> list[SearchProfileRead]:
    """List search profiles belonging to the authenticated user."""
    import uuid

    return service.list_by_user(uuid.UUID(user_id_str))


@router.get(
    "/preferred",
    response_model=SearchProfileRead,
    responses={
        401: {"description": "Missing or invalid bearer token"},
        404: {"description": "The user has no preferred profile"},
    },
)
def get_preferred_profile(
    user_id_str: str = Depends(_resolve_user_id),  # noqa: B008
    service: SearchProfileService = Depends(get_search_profile_service),  # noqa: B008
) -> SearchProfileRead:
    """Return the authenticated user's "preferred" search profile.

    This endpoint is a placeholder for a future M6 feature: a dedicated
    setter is not yet exposed, so every user currently gets ``404``. The
    data-model column ``is_preferred`` lands in this milestone so the
    follow-up issue can be picked up without a schema change.
    """
    import uuid

    preferred = service.get_preferred(uuid.UUID(user_id_str))
    if preferred is None:
        raise _http_error(
            status.HTTP_404_NOT_FOUND,
            "no_preferred_profile",
            "the user has no preferred search profile",
        )
    return preferred


@router.get(
    "/{profile_id}",
    response_model=SearchProfileRead,
    responses={
        401: {"description": "Missing or invalid bearer token"},
        403: {"description": "Profile does not belong to the caller"},
        404: {"description": "Profile not found"},
    },
)
def get_profile(
    profile_id: str,
    user_id_str: str = Depends(_resolve_user_id),  # noqa: B008
    service: SearchProfileService = Depends(get_search_profile_service),  # noqa: B008
) -> SearchProfileRead:
    """Return a single search profile by id."""
    import uuid

    try:
        profile_uuid = uuid.UUID(profile_id)
    except ValueError as exc:
        raise _http_error(status.HTTP_404_NOT_FOUND, "not_found", "invalid profile id") from exc
    try:
        return service.get(profile_uuid, user_id=uuid.UUID(user_id_str))
    except ProfileNotFoundError as exc:
        raise _http_error(status.HTTP_404_NOT_FOUND, exc.code, exc.message) from exc
    except ProfileOwnershipError as exc:
        raise _http_error(status.HTTP_403_FORBIDDEN, exc.code, exc.message) from exc


@router.put(
    "/{profile_id}",
    response_model=SearchProfileRead,
    responses={
        401: {"description": "Missing or invalid bearer token"},
        403: {"description": "Profile does not belong to the caller"},
        404: {"description": "Profile not found"},
        422: {"description": "Validation error"},
    },
)
def update_profile(
    profile_id: str,
    payload: SearchProfileUpdate,
    user_id_str: str = Depends(_resolve_user_id),  # noqa: B008
    service: SearchProfileService = Depends(get_search_profile_service),  # noqa: B008
) -> SearchProfileRead:
    """Update an existing search profile."""
    import uuid

    try:
        profile_uuid = uuid.UUID(profile_id)
    except ValueError as exc:
        raise _http_error(status.HTTP_404_NOT_FOUND, "not_found", "invalid profile id") from exc
    try:
        return service.update(profile_uuid, payload, user_id=uuid.UUID(user_id_str))
    except ProfileNotFoundError as exc:
        raise _http_error(status.HTTP_404_NOT_FOUND, exc.code, exc.message) from exc
    except ProfileOwnershipError as exc:
        raise _http_error(status.HTTP_403_FORBIDDEN, exc.code, exc.message) from exc
    except ValidationError as exc:
        raise _http_error(status.HTTP_422_UNPROCESSABLE_ENTITY, exc.code, exc.message) from exc


@router.delete(
    "/{profile_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        401: {"description": "Missing or invalid bearer token"},
        403: {"description": "Profile does not belong to the caller"},
        404: {"description": "Profile not found"},
    },
)
def delete_profile(
    profile_id: str,
    user_id_str: str = Depends(_resolve_user_id),  # noqa: B008
    service: SearchProfileService = Depends(get_search_profile_service),  # noqa: B008
) -> None:
    """Delete a search profile."""
    import uuid

    try:
        profile_uuid = uuid.UUID(profile_id)
    except ValueError as exc:
        raise _http_error(status.HTTP_404_NOT_FOUND, "not_found", "invalid profile id") from exc
    try:
        service.delete(profile_uuid, user_id=uuid.UUID(user_id_str))
    except ProfileNotFoundError as exc:
        raise _http_error(status.HTTP_404_NOT_FOUND, exc.code, exc.message) from exc
    except ProfileOwnershipError as exc:
        raise _http_error(status.HTTP_403_FORBIDDEN, exc.code, exc.message) from exc


@router.post(
    "/{profile_id}/activate",
    response_model=SearchProfileRead,
    responses={
        401: {"description": "Missing or invalid bearer token"},
        403: {"description": "Profile does not belong to the caller"},
        404: {"description": "Profile not found"},
    },
)
def activate_profile(
    profile_id: str,
    user_id_str: str = Depends(_resolve_user_id),  # noqa: B008
    service: SearchProfileService = Depends(get_search_profile_service),  # noqa: B008
) -> SearchProfileRead:
    """Flip ``is_active`` to ``True`` on the given profile."""
    import uuid

    try:
        profile_uuid = uuid.UUID(profile_id)
    except ValueError as exc:
        raise _http_error(status.HTTP_404_NOT_FOUND, "not_found", "invalid profile id") from exc
    try:
        return service.set_active(profile_uuid, active=True, user_id=uuid.UUID(user_id_str))
    except ProfileNotFoundError as exc:
        raise _http_error(status.HTTP_404_NOT_FOUND, exc.code, exc.message) from exc
    except ProfileOwnershipError as exc:
        raise _http_error(status.HTTP_403_FORBIDDEN, exc.code, exc.message) from exc


@router.post(
    "/{profile_id}/deactivate",
    response_model=SearchProfileRead,
    responses={
        401: {"description": "Missing or invalid bearer token"},
        403: {"description": "Profile does not belong to the caller"},
        404: {"description": "Profile not found"},
    },
)
def deactivate_profile(
    profile_id: str,
    user_id_str: str = Depends(_resolve_user_id),  # noqa: B008
    service: SearchProfileService = Depends(get_search_profile_service),  # noqa: B008
) -> SearchProfileRead:
    """Flip ``is_active`` to ``False`` on the given profile."""
    import uuid

    try:
        profile_uuid = uuid.UUID(profile_id)
    except ValueError as exc:
        raise _http_error(status.HTTP_404_NOT_FOUND, "not_found", "invalid profile id") from exc
    try:
        return service.set_active(profile_uuid, active=False, user_id=uuid.UUID(user_id_str))
    except ProfileNotFoundError as exc:
        raise _http_error(status.HTTP_404_NOT_FOUND, exc.code, exc.message) from exc
    except ProfileOwnershipError as exc:
        raise _http_error(status.HTTP_403_FORBIDDEN, exc.code, exc.message) from exc


__all__ = [
    "get_search_profile_service",
    "router",
]
