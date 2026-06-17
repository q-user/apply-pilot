"""TDD tests for the digest's treatment of ``deferred`` matches (M4, issue #39).

A ``deferred`` match is a soft "I don't want to look at this right now
but maybe later" state. It must not surface in the daily digest: a
match the user has explicitly shelved should not contribute to the
"new" / "review" counts, and it must not be part of the
``matches_total`` headline number — the digest is meant to be a
review queue, and the user has already decided to skip it.

The test exercises :class:`StatsService` end-to-end with the in-memory
fakes (no ``Mock``) and pins the expected bucket counts for a
representative distribution of statuses.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import cast

from apply_pilot.features.matches.models import MatchStatus, VacancyMatch
from apply_pilot.features.matches.repository import InMemoryVacancyMatchRepository
from apply_pilot.features.search_profiles.models import SearchProfile
from apply_pilot.features.search_profiles.repository import InMemorySearchProfileRepository
from apply_pilot.features.telegram.digest import StatsService
from apply_pilot.features.telegram.repository import InMemoryTelegramAccountRepository
from apply_pilot.features.users.repository import InMemoryUsersRepository


class _FakeClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def __call__(self) -> datetime:
        return self._now


def _make_match(*, profile_id: uuid.UUID, status: str) -> VacancyMatch:
    match = VacancyMatch(
        id=uuid.uuid4(),
        search_profile_id=profile_id,
        vacancy_id=uuid.uuid4(),
        status=status,
    )
    match.created_at = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    match.updated_at = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    return match


def _seed_match(repo: InMemoryVacancyMatchRepository, match: VacancyMatch) -> None:
    repo._by_id[match.id] = match  # noqa: SLF001
    repo._by_pair[(match.search_profile_id, match.vacancy_id)] = match.id  # noqa: SLF001


async def test_stats_exclude_deferred_matches() -> None:
    """``deferred`` matches must not contribute to any digest bucket.

    Distribution: 2 new, 1 scored, 1 review, 1 accepted, 1 rejected,
    1 applied, 1 dismissed, **2 deferred**.

    The user-visible digest should report:

    * ``matches_total = 6`` (everything except the 2 deferred),
    * ``matches_new = 3`` (2 new + 1 scored),
    * ``matches_review = 1``,
    * ``matches_accepted = 1``,
    * ``matches_rejected = 1``,
    * ``matches_applied = 1``,
    * ``pending_applications = 1`` (proxy = accepted).

    The 2 deferred matches are silently dropped from the digest: they
    must not appear in any bucket the renderer prints, but they are
    still on the row so the user can resume them later.
    """
    user_id = uuid.uuid4()
    profile_repo = InMemorySearchProfileRepository()
    profile = SearchProfile(id=uuid.uuid4(), user_id=user_id, title="p1", is_active=True)
    profile_repo.create(profile)
    matches_repo = InMemoryVacancyMatchRepository(
        list_user_profiles=lambda uid: profile_repo.list_by_user(uid),
    )
    statuses = [
        MatchStatus.NEW,
        MatchStatus.NEW,
        MatchStatus.SCORED,
        MatchStatus.REVIEW,
        MatchStatus.ACCEPTED,
        MatchStatus.REJECTED,
        MatchStatus.APPLIED,
        MatchStatus.DISMISSED,
        MatchStatus.DEFERRED,
        MatchStatus.DEFERRED,
    ]
    for status in statuses:
        _seed_match(matches_repo, _make_match(profile_id=profile.id, status=status.value))

    users_repo = InMemoryUsersRepository()
    service = StatsService(
        match_repo=cast("InMemoryVacancyMatchRepository", matches_repo),
        telegram_account_repo=InMemoryTelegramAccountRepository(),
        user_repo=cast("InMemoryUsersRepository", users_repo),
        profile_repo=cast("InMemorySearchProfileRepository", profile_repo),
        now=cast("Callable[[], datetime]", _FakeClock(datetime(2026, 6, 15, 9, 0, tzinfo=UTC))),
    )

    stats = service.get_user_stats(user_id, on_date=date(2026, 6, 15))

    assert stats.matches_total == 8  # all rows except the 2 deferred
    assert stats.matches_new == 3  # 2 new + 1 scored
    assert stats.matches_review == 1
    assert stats.matches_accepted == 1
    assert stats.matches_rejected == 1
    assert stats.matches_applied == 1
    assert stats.pending_applications == 1  # proxy = accepted


async def test_stats_digest_does_not_mention_deferred_for_user_with_only_deferred() -> None:
    """A user who has deferred every match sees a zero digest (not the deferred count)."""
    user_id = uuid.uuid4()
    profile_repo = InMemorySearchProfileRepository()
    profile = SearchProfile(id=uuid.uuid4(), user_id=user_id, title="p1", is_active=True)
    profile_repo.create(profile)
    matches_repo = InMemoryVacancyMatchRepository(
        list_user_profiles=lambda uid: profile_repo.list_by_user(uid),
    )
    for _ in range(3):
        _seed_match(
            matches_repo, _make_match(profile_id=profile.id, status=MatchStatus.DEFERRED.value)
        )

    users_repo = InMemoryUsersRepository()
    service = StatsService(
        match_repo=cast("InMemoryVacancyMatchRepository", matches_repo),
        telegram_account_repo=InMemoryTelegramAccountRepository(),
        user_repo=cast("InMemoryUsersRepository", users_repo),
        profile_repo=cast("InMemorySearchProfileRepository", profile_repo),
        now=cast("Callable[[], datetime]", _FakeClock(datetime(2026, 6, 15, 9, 0, tzinfo=UTC))),
    )

    stats = service.get_user_stats(user_id, on_date=date(2026, 6, 15))

    assert stats.matches_total == 0
    assert stats.matches_new == 0
    assert stats.matches_review == 0
    assert stats.matches_accepted == 0
    assert stats.matches_rejected == 0
    assert stats.matches_applied == 0
