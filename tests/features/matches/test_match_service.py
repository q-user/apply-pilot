"""TDD tests for the :class:`MatchService` use cases.

The service is exercised through the in-memory fakes so the slice
contract is verified end-to-end without an external database. The
service composes the matches and profiles repositories, so both fakes
live in the fixtures.
"""

from __future__ import annotations

import uuid

import pytest

from apply_pilot.features.matches.models import MatchStatus
from apply_pilot.features.matches.repository import InMemoryVacancyMatchRepository
from apply_pilot.features.matches.service import (
    MatchNotFoundError,
    MatchOwnershipError,
    MatchService,
)
from apply_pilot.features.search_profiles.models import SearchProfile
from apply_pilot.features.search_profiles.repository import InMemorySearchProfileRepository
from apply_pilot.features.sources.models import Vacancy

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _vacancy(source_id: str = "hh-1", title: str = "Python Dev") -> Vacancy:
    """Build a fully-populated :class:`Vacancy` mirroring a normalised import."""
    v = Vacancy(
        source="hh",
        source_id=source_id,
        title=title,
        raw_data={"id": source_id, "name": title},
    )
    v.id = uuid.uuid4()
    return v


def _profile(user_id: uuid.UUID, title: str = "Python", *, is_active: bool = True) -> SearchProfile:
    """Build a :class:`SearchProfile` owned by ``user_id``."""
    p = SearchProfile(user_id=user_id, title=title, is_active=is_active)
    p.id = uuid.uuid4()
    return p


@pytest.fixture
def user_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def other_user_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def profile_repo() -> InMemorySearchProfileRepository:
    return InMemorySearchProfileRepository()


@pytest.fixture
def match_repo(profile_repo: InMemorySearchProfileRepository) -> InMemoryVacancyMatchRepository:
    return InMemoryVacancyMatchRepository(list_user_profiles=profile_repo.list_by_user)


@pytest.fixture
def service(
    match_repo: InMemoryVacancyMatchRepository,
    profile_repo: InMemorySearchProfileRepository,
) -> MatchService:
    return MatchService(match_repo=match_repo, profile_repo=profile_repo)


# ---------------------------------------------------------------------------
# create_match
# ---------------------------------------------------------------------------


def test_create_match_returns_new_row(
    service: MatchService, profile_repo: InMemorySearchProfileRepository, user_id: uuid.UUID
) -> None:
    """A first call for a pair must produce a new match with status=new."""
    profile = _profile(user_id)
    profile_repo.create(profile)
    vacancy = _vacancy()

    result = service.create_match(profile.id, vacancy.id, user_id=user_id)

    assert result.search_profile_id == profile.id
    assert result.vacancy_id == vacancy.id
    assert result.status == MatchStatus.NEW.value
    assert result.id


def test_create_match_is_idempotent(
    service: MatchService, profile_repo: InMemorySearchProfileRepository, user_id: uuid.UUID
) -> None:
    """Re-running with the same pair must return the same match, not a duplicate."""
    profile = _profile(user_id)
    profile_repo.create(profile)
    vacancy = _vacancy()

    first = service.create_match(profile.id, vacancy.id, user_id=user_id)
    second = service.create_match(profile.id, vacancy.id, user_id=user_id)

    assert first.id == second.id
    # Repository only holds one row.
    assert len(list(service.repo.list_by_profile(profile.id))) == 1


def test_create_match_does_not_collide_across_profiles(
    service: MatchService, profile_repo: InMemorySearchProfileRepository, user_id: uuid.UUID
) -> None:
    """Two profiles matched against the same vacancy must produce two rows."""
    profile_a = _profile(user_id, title="A")
    profile_b = _profile(user_id, title="B")
    profile_repo.create(profile_a)
    profile_repo.create(profile_b)
    vacancy = _vacancy()

    match_a = service.create_match(profile_a.id, vacancy.id, user_id=user_id)
    match_b = service.create_match(profile_b.id, vacancy.id, user_id=user_id)

    assert match_a.id != match_b.id
    assert match_a.vacancy_id == match_b.vacancy_id == vacancy.id


# ---------------------------------------------------------------------------
# bulk_create_for_profile
# ---------------------------------------------------------------------------


def test_bulk_create_for_profile_creates_all(
    service: MatchService, profile_repo: InMemorySearchProfileRepository, user_id: uuid.UUID
) -> None:
    """Bulk insert must produce one match per vacancy in the batch."""
    profile = _profile(user_id)
    profile_repo.create(profile)
    vacancies = [_vacancy(source_id=f"hh-{i}") for i in range(3)]

    created = service.bulk_create_for_profile(profile, vacancies)

    assert len(created) == 3
    assert {m.vacancy_id for m in created} == {v.id for v in vacancies}
    assert all(m.status == MatchStatus.NEW.value for m in created)


def test_bulk_create_for_profile_skips_existing(
    service: MatchService, profile_repo: InMemorySearchProfileRepository, user_id: uuid.UUID
) -> None:
    """Pairs that already have a match must be skipped, not re-inserted."""
    profile = _profile(user_id)
    profile_repo.create(profile)
    vacancies = [_vacancy(source_id=f"hh-{i}") for i in range(3)]

    # Pre-seed one match.
    service.create_match(profile.id, vacancies[0].id)

    created = service.bulk_create_for_profile(profile, vacancies)

    # Only the two new pairs are returned.
    assert len(created) == 2
    assert {m.vacancy_id for m in created} == {vacancies[1].id, vacancies[2].id}
    assert len(list(service.repo.list_by_profile(profile.id))) == 3


def test_bulk_create_for_profile_empty_list(
    service: MatchService, profile_repo: InMemorySearchProfileRepository, user_id: uuid.UUID
) -> None:
    """An empty vacancy list must yield an empty result, not error."""
    profile = _profile(user_id)
    profile_repo.create(profile)

    assert service.bulk_create_for_profile(profile, []) == []


# ---------------------------------------------------------------------------
# bulk_create_for_all_active_profiles
# ---------------------------------------------------------------------------


def test_bulk_create_for_all_active_profiles_iterates_active(
    service: MatchService, profile_repo: InMemorySearchProfileRepository, user_id: uuid.UUID
) -> None:
    """Every active profile must get one match per vacancy."""
    p1 = _profile(user_id, title="A")
    p2 = _profile(user_id, title="B")
    profile_repo.create(p1)
    profile_repo.create(p2)
    vacancies = [_vacancy(source_id=f"hh-{i}") for i in range(2)]

    total = service.bulk_create_for_all_active_profiles(vacancies)

    assert total == 4  # 2 profiles × 2 vacancies
    assert len(list(service.repo.list_by_profile(p1.id))) == 2
    assert len(list(service.repo.list_by_profile(p2.id))) == 2


def test_bulk_create_for_all_active_profiles_skips_inactive(
    service: MatchService, profile_repo: InMemorySearchProfileRepository, user_id: uuid.UUID
) -> None:
    """Inactive profiles must be skipped entirely."""
    active = _profile(user_id, title="Active", is_active=True)
    inactive = _profile(user_id, title="Inactive", is_active=False)
    profile_repo.create(active)
    profile_repo.create(inactive)
    vacancies = [_vacancy()]

    total = service.bulk_create_for_all_active_profiles(vacancies)

    assert total == 1
    assert len(list(service.repo.list_by_profile(inactive.id))) == 0


def test_bulk_create_for_all_active_profiles_no_profiles(
    service: MatchService,
) -> None:
    """No active profiles means zero matches, not an error."""
    assert service.bulk_create_for_all_active_profiles([_vacancy()]) == 0


# ---------------------------------------------------------------------------
# list_matches
# ---------------------------------------------------------------------------


def test_list_matches_returns_only_own(
    service: MatchService,
    profile_repo: InMemorySearchProfileRepository,
    user_id: uuid.UUID,
    other_user_id: uuid.UUID,
) -> None:
    """Matches for other users' profiles must not leak into the listing."""
    mine = _profile(user_id, title="mine")
    theirs = _profile(other_user_id, title="theirs")
    profile_repo.create(mine)
    profile_repo.create(theirs)
    vacancy = _vacancy()

    service.create_match(mine.id, vacancy.id, user_id=user_id)
    service.create_match(theirs.id, vacancy.id, user_id=user_id)

    mine_listed = service.list_matches(user_id)
    theirs_listed = service.list_matches(other_user_id)

    assert len(mine_listed) == 1
    assert mine_listed[0].search_profile_id == mine.id
    assert len(theirs_listed) == 1
    assert theirs_listed[0].search_profile_id == theirs.id


def test_list_matches_filters_by_status(
    service: MatchService, profile_repo: InMemorySearchProfileRepository, user_id: uuid.UUID
) -> None:
    """A status filter must restrict the listing to that status."""
    profile = _profile(user_id)
    profile_repo.create(profile)
    m1 = service.create_match(profile.id, _vacancy("hh-1").id)
    m2 = service.create_match(profile.id, _vacancy("hh-2").id)
    service.update_status(m2.id, MatchStatus.ACCEPTED.value)

    new_only = service.list_matches(user_id, status=MatchStatus.NEW.value)
    accepted_only = service.list_matches(user_id, status=MatchStatus.ACCEPTED.value)

    assert [m.id for m in new_only] == [m1.id]
    assert [m.id for m in accepted_only] == [m2.id]


def test_list_matches_invalid_status_raises(service: MatchService, user_id: uuid.UUID) -> None:
    """An unknown status filter must raise ValidationError."""
    from apply_pilot.shared.errors import ValidationError

    with pytest.raises(ValidationError, match="unknown match status"):
        service.list_matches(user_id, status="bogus")


# ---------------------------------------------------------------------------
# update_status
# ---------------------------------------------------------------------------


def test_update_status_changes_status(
    service: MatchService, profile_repo: InMemorySearchProfileRepository, user_id: uuid.UUID
) -> None:
    """Updating the status must persist the new value."""
    profile = _profile(user_id)
    profile_repo.create(profile)
    match = service.create_match(profile.id, _vacancy().id)

    updated = service.update_status(match.id, MatchStatus.SCORED.value, score=85, user_id=user_id)

    assert updated.status == MatchStatus.SCORED.value
    assert updated.score == 85


def test_update_status_invalid_status_raises(
    service: MatchService, profile_repo: InMemorySearchProfileRepository, user_id: uuid.UUID
) -> None:
    """An unknown status must raise ValidationError before the repo is touched."""
    from apply_pilot.shared.errors import ValidationError

    profile = _profile(user_id)
    profile_repo.create(profile)
    match = service.create_match(profile.id, _vacancy().id)

    with pytest.raises(ValidationError, match="unknown match status"):
        service.update_status(match.id, "bogus", user_id=user_id)


def test_update_status_unknown_match_raises(service: MatchService, user_id: uuid.UUID) -> None:
    """Updating a non-existent match must raise MatchNotFoundError."""
    with pytest.raises(MatchNotFoundError):
        service.update_status(uuid.uuid4(), MatchStatus.ACCEPTED.value, user_id=user_id)


def test_update_status_enforces_ownership(
    service: MatchService,
    profile_repo: InMemorySearchProfileRepository,
    user_id: uuid.UUID,
    other_user_id: uuid.UUID,
) -> None:
    """A user must not be able to update a match they do not own."""
    profile = _profile(user_id)
    profile_repo.create(profile)
    match = service.create_match(profile.id, _vacancy().id)

    with pytest.raises(MatchOwnershipError):
        service.update_status(match.id, MatchStatus.ACCEPTED.value, user_id=other_user_id)


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


def test_get_returns_match(
    service: MatchService, profile_repo: InMemorySearchProfileRepository, user_id: uuid.UUID
) -> None:
    """Fetching by id must return the right match for the owner."""
    profile = _profile(user_id)
    profile_repo.create(profile)
    created = service.create_match(profile.id, _vacancy().id)

    result = service.get(created.id, user_id=user_id)

    assert result.id == created.id


def test_get_unknown_match_raises(service: MatchService, user_id: uuid.UUID) -> None:
    """Fetching a non-existent match must raise MatchNotFoundError."""
    with pytest.raises(MatchNotFoundError):
        service.get(uuid.uuid4(), user_id=user_id)


def test_get_other_users_match_raises_forbidden(
    service: MatchService,
    profile_repo: InMemorySearchProfileRepository,
    user_id: uuid.UUID,
    other_user_id: uuid.UUID,
) -> None:
    """A user must not be able to read another user's match."""
    profile = _profile(user_id)
    profile_repo.create(profile)
    match = service.create_match(profile.id, _vacancy().id)

    with pytest.raises(MatchOwnershipError):
        service.get(match.id, user_id=other_user_id)
