"""Tests for :class:`StatsService` — daily-digest stats aggregation.

The service is exercised with the in-memory fakes so the dict-backed
contracts are verified end-to-end. A deterministic clock is injected so
``applied_today`` boundaries stay predictable across timezones.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import cast

import pytest

from apply_pilot.features.matches.models import MatchStatus, VacancyMatch
from apply_pilot.features.matches.repository import InMemoryVacancyMatchRepository
from apply_pilot.features.search_profiles.models import SearchProfile
from apply_pilot.features.search_profiles.repository import InMemorySearchProfileRepository
from apply_pilot.features.telegram.digest import StatsService, UserStats
from apply_pilot.features.telegram.repository import InMemoryTelegramAccountRepository
from apply_pilot.features.users.models import User
from apply_pilot.features.users.repository import InMemoryUsersRepository

# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------


class _FakeClock:
    """Deterministic clock the :class:`StatsService` reads from the constructor."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def __call__(self) -> datetime:
        return self._now


def _make_user(*, email: str = "user@example.com") -> User:
    return User(
        id=uuid.uuid4(),
        email=email,
        hashed_password="x",
        is_active=True,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _make_match(
    *,
    profile_id: uuid.UUID,
    status: str,
    updated_at: datetime | None = None,
) -> VacancyMatch:
    """Build a match with a deterministic ``created_at`` / ``updated_at``."""
    match = VacancyMatch(
        id=uuid.uuid4(),
        search_profile_id=profile_id,
        vacancy_id=uuid.uuid4(),
        status=status,
    )
    match.created_at = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    match.updated_at = updated_at or datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    return match


def _seed_match(repo: InMemoryVacancyMatchRepository, match: VacancyMatch) -> None:
    """Insert *match* into the in-memory repo without overwriting ``updated_at``.

    ``InMemoryVacancyMatchRepository.create`` resets ``updated_at`` to
    ``datetime.now(UTC)`` on every insert, which would clobber the
    deterministic timestamps that ``applied_today`` tests rely on.
    Seeding bypasses that path while keeping the indexes in sync.
    """
    repo._by_id[match.id] = match  # noqa: SLF001
    repo._by_pair[(match.search_profile_id, match.vacancy_id)] = match.id  # noqa: SLF001


def _seed_user(users_repo: InMemoryUsersRepository, user: User) -> None:
    """Insert a user into the in-memory repo without going through the public API.

    The repo's ``create`` requires a hashed password and email; tests build
    full :class:`User` objects with deterministic ids, so direct seeding
    keeps the fixtures terse and the public API under test.
    """
    users_repo._by_id[user.id] = user  # noqa: SLF001
    users_repo._by_email[user.email.lower()] = user.id  # noqa: SLF001


def _make_service(
    *,
    users: list[User] | None = None,
    telegram: InMemoryTelegramAccountRepository | None = None,
    profiles: InMemorySearchProfileRepository | None = None,
    matches: InMemoryVacancyMatchRepository | None = None,
    now: datetime | None = None,
) -> StatsService:
    """Wire the stats service with the in-memory fakes."""
    users_repo = InMemoryUsersRepository()
    for user in users or []:
        _seed_user(users_repo, user)
    telegram_repo = telegram or InMemoryTelegramAccountRepository()
    profile_repo = profiles or InMemorySearchProfileRepository()
    if matches is None:
        matches = InMemoryVacancyMatchRepository(
            list_user_profiles=lambda uid: profile_repo.list_by_user(uid),
        )
    clock = _FakeClock(now or datetime(2026, 6, 15, 9, 0, tzinfo=UTC))
    return StatsService(
        match_repo=cast("InMemoryVacancyMatchRepository", matches),
        telegram_account_repo=telegram_repo,
        user_repo=cast("InMemoryUsersRepository", users_repo),
        now=cast("Callable[[], datetime]", clock),
        profile_repo=cast("InMemorySearchProfileRepository", profile_repo),
    )


# ---------------------------------------------------------------------------
# get_user_stats
# ---------------------------------------------------------------------------


async def test_get_user_stats_zero_when_no_matches() -> None:
    """A user with no matches gets all-zero counts and the requested date."""
    service = _make_service()
    stats = await service.get_user_stats(uuid.uuid4(), on_date=date(2026, 6, 15))

    assert stats == UserStats(
        matches_total=0,
        matches_new=0,
        matches_review=0,
        matches_accepted=0,
        matches_rejected=0,
        matches_applied=0,
        pending_applications=0,
        applied_today=0,
        digest_date=date(2026, 6, 15),
    )


async def test_get_user_stats_groups_matches_by_status() -> None:
    """Counts reflect the ``status`` of every match under the user's profiles."""
    user_id = uuid.uuid4()
    profile_repo = InMemorySearchProfileRepository()
    profile = SearchProfile(id=uuid.uuid4(), user_id=user_id, title="p1", is_active=True)
    profile_repo.create(profile)
    matches_repo = InMemoryVacancyMatchRepository(
        list_user_profiles=lambda uid: profile_repo.list_by_user(uid),
    )
    # Distribution: 2 new, 3 scored, 1 review, 2 accepted, 1 rejected, 1 applied, 1 dismissed.
    statuses = [
        MatchStatus.NEW,
        MatchStatus.NEW,
        MatchStatus.SCORED,
        MatchStatus.SCORED,
        MatchStatus.SCORED,
        MatchStatus.REVIEW,
        MatchStatus.ACCEPTED,
        MatchStatus.ACCEPTED,
        MatchStatus.REJECTED,
        MatchStatus.APPLIED,
        MatchStatus.DISMISSED,
    ]
    for i, status in enumerate(statuses):
        match = _make_match(
            profile_id=profile.id,
            status=status.value,
            # Stagger ``updated_at`` so the single ``APPLIED`` row in
            # this fixture is anchored to a known day (the digest
            # date); the in-memory repo's ``create`` clobbers it, so
            # we seed directly for the APPLIED row only.
            updated_at=(
                datetime(2026, 6, 14, 12, 0, tzinfo=UTC)
                if status is MatchStatus.APPLIED
                else datetime(2026, 6, 1, 12, 0, tzinfo=UTC) + timedelta(seconds=i)
            ),
        )
        if status is MatchStatus.APPLIED:
            _seed_match(matches_repo, match)
        else:
            matches_repo.create(match)

    service = _make_service(
        profiles=profile_repo,
        matches=matches_repo,
        now=datetime(2026, 6, 15, 9, 0, tzinfo=UTC),
    )
    stats = await service.get_user_stats(user_id, on_date=date(2026, 6, 15))

    assert stats.matches_total == 11
    assert stats.matches_new == 5  # 2 new + 3 scored
    assert stats.matches_review == 1
    assert stats.matches_accepted == 2
    assert stats.matches_rejected == 1
    assert stats.matches_applied == 1
    assert stats.pending_applications == 2  # proxy = matches with status=accepted
    assert stats.applied_today == 0
    assert stats.digest_date == date(2026, 6, 15)


async def test_get_user_stats_applied_today_counts_only_today_in_utc() -> None:
    """``applied_today`` = applied matches whose ``updated_at`` is in the digest date (UTC)."""
    user_id = uuid.uuid4()
    profile_repo = InMemorySearchProfileRepository()
    profile = SearchProfile(id=uuid.uuid4(), user_id=user_id, title="p1", is_active=True)
    profile_repo.create(profile)
    matches_repo = InMemoryVacancyMatchRepository(
        list_user_profiles=lambda uid: profile_repo.list_by_user(uid),
    )
    today = date(2026, 6, 15)
    _seed_match(
        matches_repo,
        _make_match(
            profile_id=profile.id,
            status=MatchStatus.APPLIED.value,
            updated_at=datetime(2026, 6, 15, 7, 30, tzinfo=UTC),
        ),
    )
    _seed_match(
        matches_repo,
        _make_match(
            profile_id=profile.id,
            status=MatchStatus.APPLIED.value,
            updated_at=datetime(2026, 6, 15, 23, 59, tzinfo=UTC),
        ),
    )
    _seed_match(
        matches_repo,
        _make_match(
            profile_id=profile.id,
            status=MatchStatus.APPLIED.value,
            updated_at=datetime(2026, 6, 14, 23, 59, tzinfo=UTC),
        ),
    )
    _seed_match(
        matches_repo,
        _make_match(
            profile_id=profile.id,
            status=MatchStatus.APPLIED.value,
            updated_at=datetime(2026, 6, 16, 0, 0, tzinfo=UTC),
        ),
    )
    # Non-applied match on the same day must not bump the applied_today count.
    _seed_match(
        matches_repo,
        _make_match(
            profile_id=profile.id,
            status=MatchStatus.ACCEPTED.value,
            updated_at=datetime(2026, 6, 15, 8, 0, tzinfo=UTC),
        ),
    )

    service = _make_service(
        profiles=profile_repo,
        matches=matches_repo,
        now=datetime(2026, 6, 15, 9, 0, tzinfo=UTC),
    )
    stats = await service.get_user_stats(user_id, on_date=today)

    assert stats.matches_applied == 4
    assert stats.applied_today == 2


async def test_get_user_stats_uses_clock_when_on_date_omitted() -> None:
    """When ``on_date`` is omitted, the service uses the injected clock's date."""
    service = _make_service(now=datetime(2026, 1, 7, 12, 0, tzinfo=UTC))
    stats = await service.get_user_stats(uuid.uuid4())
    assert stats.digest_date == date(2026, 1, 7)


async def test_get_user_stats_only_counts_users_own_profiles() -> None:
    """Matches from another user's profile must not leak into the digest."""
    user_id = uuid.uuid4()
    other_id = uuid.uuid4()
    profile_repo = InMemorySearchProfileRepository()
    own = SearchProfile(id=uuid.uuid4(), user_id=user_id, title="own", is_active=True)
    other = SearchProfile(id=uuid.uuid4(), user_id=other_id, title="other", is_active=True)
    profile_repo.create(own)
    profile_repo.create(other)
    matches_repo = InMemoryVacancyMatchRepository(
        list_user_profiles=lambda uid: profile_repo.list_by_user(uid),
    )
    matches_repo.create(_make_match(profile_id=own.id, status=MatchStatus.NEW.value))
    matches_repo.create(_make_match(profile_id=own.id, status=MatchStatus.NEW.value))
    matches_repo.create(_make_match(profile_id=other.id, status=MatchStatus.NEW.value))
    matches_repo.create(_make_match(profile_id=other.id, status=MatchStatus.NEW.value))
    matches_repo.create(_make_match(profile_id=other.id, status=MatchStatus.NEW.value))

    service = _make_service(
        profiles=profile_repo,
        matches=matches_repo,
        now=datetime(2026, 6, 15, 9, 0, tzinfo=UTC),
    )
    stats = await service.get_user_stats(user_id, on_date=date(2026, 6, 15))

    assert stats.matches_total == 2


# ---------------------------------------------------------------------------
# get_all_users_with_telegram
# ---------------------------------------------------------------------------


async def test_get_all_users_with_telegram_returns_only_linked_users() -> None:
    """Users without a linked Telegram account are excluded from the broadcast."""
    linked_a = _make_user(email="a@example.com")
    linked_b = _make_user(email="b@example.com")
    orphan = _make_user(email="c@example.com")
    telegram = InMemoryTelegramAccountRepository()
    telegram.create(user_id=linked_a.id, telegram_user_id=11)
    telegram.create(user_id=linked_b.id, telegram_user_id=22)

    service = _make_service(users=[linked_a, linked_b, orphan], telegram=telegram)

    returned_ids = {u.id for u in await service.get_all_users_with_telegram()}
    assert returned_ids == {linked_a.id, linked_b.id}


async def test_get_all_users_with_telegram_empty_when_no_links() -> None:
    """A repo with no accounts yields an empty list (not an error)."""
    service = _make_service()
    assert await service.get_all_users_with_telegram() == []


# ---------------------------------------------------------------------------
# Status semantics
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status_value", "is_new"),
    [
        (MatchStatus.NEW.value, True),
        (MatchStatus.SCORED.value, True),
        (MatchStatus.REVIEW.value, False),
        (MatchStatus.ACCEPTED.value, False),
        (MatchStatus.REJECTED.value, False),
        (MatchStatus.APPLIED.value, False),
        (MatchStatus.DISMISSED.value, False),
    ],
)
async def test_matches_new_aggregates_new_and_scored(status_value: str, *, is_new: bool) -> None:
    """``matches_new`` = matches with status in {new, scored}."""
    user_id = uuid.uuid4()
    profile_repo = InMemorySearchProfileRepository()
    profile = SearchProfile(id=uuid.uuid4(), user_id=user_id, title="p1", is_active=True)
    profile_repo.create(profile)
    matches_repo = InMemoryVacancyMatchRepository(
        list_user_profiles=lambda uid: profile_repo.list_by_user(uid),
    )
    matches_repo.create(_make_match(profile_id=profile.id, status=status_value))

    service = _make_service(
        profiles=profile_repo,
        matches=matches_repo,
        now=datetime(2026, 6, 15, 9, 0, tzinfo=UTC),
    )
    stats = await service.get_user_stats(user_id, on_date=date(2026, 6, 15))
    assert (stats.matches_new == 1) is is_new


async def test_pending_applications_uses_accepted_as_proxy() -> None:
    """Pending applications proxy = matches with status=accepted (no apply worker yet)."""
    user_id = uuid.uuid4()
    profile_repo = InMemorySearchProfileRepository()
    profile = SearchProfile(id=uuid.uuid4(), user_id=user_id, title="p1", is_active=True)
    profile_repo.create(profile)
    matches_repo = InMemoryVacancyMatchRepository(
        list_user_profiles=lambda uid: profile_repo.list_by_user(uid),
    )
    for _ in range(4):
        matches_repo.create(_make_match(profile_id=profile.id, status=MatchStatus.ACCEPTED.value))
    # An applied match must not be counted as pending.
    matches_repo.create(_make_match(profile_id=profile.id, status=MatchStatus.APPLIED.value))

    service = _make_service(
        profiles=profile_repo,
        matches=matches_repo,
        now=datetime(2026, 6, 15, 9, 0, tzinfo=UTC),
    )
    stats = await service.get_user_stats(user_id, on_date=date(2026, 6, 15))
    assert stats.pending_applications == 4
    assert stats.matches_applied == 1
