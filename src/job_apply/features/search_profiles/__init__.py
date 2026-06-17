"""Search profiles vertical slice.

Public surface
--------------

The slice exposes the ``SearchProfile`` ORM model and the
``SearchProfileService`` entry point. Other M1 slices import the model
from here when they need to reference search profiles.

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
"""

from __future__ import annotations

from job_apply.features.search_profiles.models import SearchProfile
from job_apply.features.search_profiles.repository import (
    InMemorySearchProfileRepository,
    SearchProfileRepository,
    SqlSearchProfileRepository,
)
from job_apply.features.search_profiles.schemas import (
    SearchProfileCreate,
    SearchProfileRead,
    SearchProfileUpdate,
)
from job_apply.features.search_profiles.service import (
    ProfileNotFoundError,
    ProfileOwnershipError,
    SearchProfileService,
)

__all__ = [
    "InMemorySearchProfileRepository",
    "ProfileNotFoundError",
    "ProfileOwnershipError",
    "SearchProfile",
    "SearchProfileCreate",
    "SearchProfileRead",
    "SearchProfileRepository",
    "SearchProfileService",
    "SearchProfileUpdate",
    "SqlSearchProfileRepository",
]
