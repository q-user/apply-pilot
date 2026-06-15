"""Business logic for the matches slice.

The :class:`MatchService` owns the rules for turning a stream of
``Vacancy`` rows into ``VacancyMatch`` rows tied to a
``SearchProfile``. It enforces:

* **Idempotency** — :meth:`create_match` returns the existing row when
  a match for the same ``(profile, vacancy)`` pair already exists.
* **Skip-on-conflict bulk insertion** — :meth:`bulk_create_for_profile`
  and :meth:`bulk_create_for_all_active_profiles` skip pairs that
  already have a match rather than raise.
* **Ownership** — :meth:`get` and :meth:`update_status` raise
  :class:`MatchOwnershipError` when the match's profile is owned by
  someone other than the requesting user.

The service is collaborator-injected: tests build it with the
in-memory fakes, the FastAPI dependency in :mod:`api` builds it with
the SQLAlchemy-backed versions sharing the request's session.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from job_apply.features.matches.models import MatchStatus, VacancyMatch
from job_apply.features.matches.repository import VacancyMatchRepository
from job_apply.features.matches.schemas import VacancyMatchRead
from job_apply.features.search_profiles.models import SearchProfile
from job_apply.features.search_profiles.repository import SearchProfileRepository
from job_apply.features.sources.models import Vacancy
from job_apply.shared.errors import NotFoundError, ValidationError


class MatchNotFoundError(NotFoundError):
    """The requested vacancy match does not exist."""

    code: str = "vacancy_match_not_found"


class MatchOwnershipError(Exception):
    """The caller does not own the requested vacancy match.

    Raised as a plain ``Exception`` (not :class:`DomainError`) so the
    HTTP layer always returns 403 regardless of error-code evolution.
    """

    code: str = "forbidden"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


# ---------------------------------------------------------------------------
# Status validation
# ---------------------------------------------------------------------------


_VALID_STATUSES: frozenset[str] = frozenset(s.value for s in MatchStatus)


def _validate_status(status: str) -> str:
    """Return ``status`` if it matches a known :class:`MatchStatus` value."""
    if status not in _VALID_STATUSES:
        raise ValidationError(f"unknown match status: {status!r}")
    return status


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


def _match_to_dto(match: VacancyMatch) -> VacancyMatchRead:
    return VacancyMatchRead(
        id=match.id,
        search_profile_id=match.search_profile_id,
        vacancy_id=match.vacancy_id,
        status=match.status,
        score=match.score,
        match_reason=match.match_reason,
        created_at=match.created_at,
        updated_at=match.updated_at,
    )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class MatchService:
    """CRUD + bulk operations for vacancy matches."""

    def __init__(
        self,
        match_repo: VacancyMatchRepository,
        profile_repo: SearchProfileRepository,
    ) -> None:
        self._match_repo = match_repo
        self._profile_repo = profile_repo

    @property
    def repo(self) -> VacancyMatchRepository:
        """Expose the repository for tests that need to assert state."""
        return self._match_repo

    @property
    def profile_repo(self) -> SearchProfileRepository:
        """Expose the profile repository for tests that need to assert state."""
        return self._profile_repo

    # -- single-row writers ----------------------------------------------

    def create_match(self, profile_id: uuid.UUID, vacancy_id: uuid.UUID) -> VacancyMatchRead:
        """Insert a match, returning the existing row on conflict.

        Idempotent: re-running with the same ``(profile_id, vacancy_id)``
        pair yields the same match, not a duplicate.
        """
        existing = self._match_repo.find_existing(profile_id, vacancy_id)
        if existing is not None:
            return _match_to_dto(existing)
        match = VacancyMatch(
            search_profile_id=profile_id,
            vacancy_id=vacancy_id,
            status=MatchStatus.NEW.value,
        )
        created = self._match_repo.create(match)
        return _match_to_dto(created)

    def get(self, match_id: uuid.UUID, *, user_id: uuid.UUID) -> VacancyMatchRead:
        """Return a single match, enforcing ownership."""
        match = self._match_repo.get_by_id(match_id)
        if match is None:
            raise MatchNotFoundError(f"vacancy match {match_id} not found")
        self._assert_ownership(match, user_id)
        return _match_to_dto(match)

    def list_matches(
        self,
        user_id: uuid.UUID,
        status: str | None = None,
    ) -> list[VacancyMatchRead]:
        """List all matches belonging to ``user_id``, optionally filtered."""
        if status is not None:
            _validate_status(status)
        matches = self._match_repo.list_by_user(user_id, status=status)
        return [_match_to_dto(m) for m in matches]

    def update_status(
        self,
        match_id: uuid.UUID,
        status: str,
        score: int | None = None,
        *,
        user_id: uuid.UUID | None = None,
    ) -> VacancyMatchRead:
        """Update a match's status (and optionally its score).

        When ``user_id`` is supplied the service enforces ownership
        before mutating state. The HTTP layer always supplies it; unit
        tests that exercise the repository wiring can pass ``None`` to
        skip the check.
        """
        _validate_status(status)
        match = self._match_repo.get_by_id(match_id)
        if match is None:
            raise MatchNotFoundError(f"vacancy match {match_id} not found")
        if user_id is not None:
            self._assert_ownership(match, user_id)
        try:
            updated = self._match_repo.update_status(match_id, status, score=score)
        except NotFoundError as exc:
            # Surface the repository's not-found as a domain error too,
            # in case the row was removed between the get and the update.
            raise MatchNotFoundError(f"vacancy match {match_id} not found") from exc
        return _match_to_dto(updated)

    # -- bulk writers ----------------------------------------------------

    def bulk_create_for_profile(
        self,
        profile: SearchProfile,
        vacancies: Sequence[Vacancy],
    ) -> list[VacancyMatchRead]:
        """Create matches for ``profile`` against each vacancy.

        Skips pairs that already have a match. Returns the newly
        created matches; an empty list means every pair was a duplicate.
        """
        if not vacancies:
            return []
        profile_id = profile.id
        wanted: list[VacancyMatch] = []
        for vacancy in vacancies:
            vacancy_id = vacancy.id
            if vacancy_id is None:
                continue
            existing = self._match_repo.find_existing(profile_id, vacancy_id)
            if existing is not None:
                continue
            wanted.append(
                VacancyMatch(
                    search_profile_id=profile_id,
                    vacancy_id=vacancy_id,
                    status=MatchStatus.NEW.value,
                )
            )
        if not wanted:
            return []
        self._bulk_insert(wanted)
        return [_match_to_dto(m) for m in wanted]

    def bulk_create_for_all_active_profiles(
        self,
        vacancies: Sequence[Vacancy],
    ) -> int:
        """Insert matches for every active profile, skipping existing pairs.

        Returns the total number of newly created matches across all
        active profiles. Inactive profiles are skipped entirely.
        """
        active = self._profile_repo.list_active()
        total = 0
        for profile in active:
            total += len(self.bulk_create_for_profile(profile, vacancies))
        return total

    # -- helpers ---------------------------------------------------------

    def _assert_ownership(self, match: VacancyMatch, user_id: uuid.UUID) -> None:
        profile = self._profile_repo.get_by_id(match.search_profile_id)
        if profile is None or profile.user_id != user_id:
            raise MatchOwnershipError(f"vacancy match {match.id} does not belong to user {user_id}")

    def _bulk_insert(self, matches: Sequence[VacancyMatch]) -> None:
        """Insert a batch of matches, skipping duplicates.

        The :class:`SqlVacancyMatchRepository` exposes
        :meth:`bulk_create_ignore_conflicts` and uses the
        ``ON CONFLICT DO NOTHING`` path; the in-memory fake falls
        through to the per-row :meth:`create` path.
        """
        repo = self._match_repo
        # ``getattr`` with a default is the standard Python idiom for
        # optional capability detection. It also keeps strict type
        # checkers (``ty``) from falling back to ``object`` after
        # ``hasattr`` on a Protocol-typed value.
        bulk_method = getattr(repo, "bulk_create_ignore_conflicts", None)
        if bulk_method is not None:
            bulk_method(matches)
            return
        for match in matches:
            repo.create(match)


__all__ = [
    "MatchNotFoundError",
    "MatchOwnershipError",
    "MatchService",
]
