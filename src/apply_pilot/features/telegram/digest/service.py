"""Stats aggregation for the daily digest.

:class:`StatsService` walks the per-user match set, buckets matches by
status and returns a :class:`UserStats` snapshot. The clock is
injected so the ``applied_today`` boundary is deterministic in tests
and overridable in production.

A ``list_user_profiles`` callable is required to bridge the
:class:`VacancyMatchRepository`'s ``list_by_user`` contract to a list
of profile ids (the SQL implementation does this in a JOIN). The
in-memory fake expects a Python callable; the SQL one is self-contained.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from typing import Protocol

from apply_pilot.features.matches.models import MatchStatus, VacancyMatch
from apply_pilot.features.telegram.digest.models import UserStats
from apply_pilot.features.telegram.models import TelegramAccount
from apply_pilot.features.users.models import User

# ---------------------------------------------------------------------------
# Protocols / types
# ---------------------------------------------------------------------------


class _MatchRepo(Protocol):
    """Subset of :class:`VacancyMatchRepository` the digest service uses."""

    def list_by_user(
        self,
        user_id: uuid.UUID,
        *,
        status: str | None = None,
    ) -> Sequence[VacancyMatch]: ...


class _TelegramAccountRepo(Protocol):
    def list_all(self) -> Sequence[TelegramAccount]: ...


class _UserRepo(Protocol):
    def list_all(self) -> Sequence[User]: ...
    def get_by_id(self, user_id: uuid.UUID) -> User | None: ...


class _ProfileRepo(Protocol):
    def list_by_user(self, user_id: uuid.UUID) -> Sequence[object]: ...


# Statuses that count as "new from the user's perspective".
_NEW_STATUSES: frozenset[str] = frozenset({MatchStatus.NEW.value, MatchStatus.SCORED.value})


class StatsService:
    """Compute a :class:`UserStats` snapshot for a single user.

    The ``now`` callable returns the current time and is the single
    source of truth for the ``digest_date`` default and the
    ``applied_today`` boundary. Tests inject a deterministic clock;
    production wiring calls :func:`datetime.now` with UTC.
    """

    def __init__(
        self,
        match_repo: _MatchRepo,
        telegram_account_repo: _TelegramAccountRepo,
        user_repo: _UserRepo,
        *,
        profile_repo: _ProfileRepo | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._match_repo = match_repo
        self._telegram_account_repo = telegram_account_repo
        self._user_repo = user_repo
        self._profile_repo = profile_repo
        self._now: Callable[[], datetime] = now or self._default_now

    @staticmethod
    def _default_now() -> datetime:
        return datetime.now(UTC)

    @property
    def now(self) -> Callable[[], datetime]:
        return self._now

    # ------------------------------------------------------------------
    # Per-user aggregation
    # ------------------------------------------------------------------

    async def get_user_stats(
        self,
        user_id: uuid.UUID,
        *,
        on_date: date | None = None,
    ) -> UserStats:
        """Return a :class:`UserStats` snapshot for *user_id*.

        ``on_date`` defaults to the current UTC date; callers that
        want a deterministic boundary (tests, backfills) can pass it
        explicitly. The aggregation is a single ``list_by_user`` call
        followed by an in-memory bucket-by-status walk.
        """
        target_date = on_date or self._now().date()
        matches = list(self._match_repo.list_by_user(user_id))

        counts = {
            "total": 0,
            "new": 0,
            "review": 0,
            "accepted": 0,
            "rejected": 0,
            "applied": 0,
            "applied_today": 0,
        }
        # ``deferred`` matches are a soft "not now, maybe later" state
        # (issue #39): they are stored on the row so the user can
        # resume them, but the daily digest must not surface them.
        # We drop them from every bucket here rather than filtering
        # the list up-front so a future "include deferred" toggle can
        # add them back to ``total`` without re-walking the matches.
        for match in matches:
            status = match.status
            if status == MatchStatus.DEFERRED.value:
                continue
            counts["total"] += 1
            if status in _NEW_STATUSES:
                counts["new"] += 1
            elif status == MatchStatus.REVIEW.value:
                counts["review"] += 1
            elif status == MatchStatus.ACCEPTED.value:
                counts["accepted"] += 1
            elif status == MatchStatus.REJECTED.value:
                counts["rejected"] += 1
            elif status == MatchStatus.APPLIED.value:
                counts["applied"] += 1
                if _updated_on_date(match, target_date):
                    counts["applied_today"] += 1
            # ``dismissed`` and any future non-deferred status are counted
            # in ``total`` but do not move any of the explicit buckets.

        return UserStats(
            matches_total=counts["total"],
            matches_new=counts["new"],
            matches_review=counts["review"],
            matches_accepted=counts["accepted"],
            matches_rejected=counts["rejected"],
            matches_applied=counts["applied"],
            # The apply worker does not exist yet; the closest proxy is
            # the matches the user has accepted (the apply pipeline will
            # consume them when it lands).
            pending_applications=counts["accepted"],
            applied_today=counts["applied_today"],
            digest_date=target_date,
        )

    # ------------------------------------------------------------------
    # User enumeration
    # ------------------------------------------------------------------

    async def get_all_users_with_telegram(self) -> list[User]:
        """Return every :class:`User` that has a linked Telegram account.

        The implementation lists the (small) set of telegram accounts
        and resolves each one through the user repository. Users with
        no row in :class:`UsersRepository` are skipped — they would
        otherwise fail the foreign-key check on the SQL side and are
        not broadcast targets.
        """
        accounts = self._telegram_account_repo.list_all()
        users: list[User] = []
        for account in accounts:
            user = self._user_repo.get_by_id(account.user_id)
            if user is not None:
                users.append(user)
        return users


def _updated_on_date(match: VacancyMatch, target: date) -> bool:
    """Return True iff *match*'s ``updated_at`` falls on *target* (UTC)."""
    updated_at = match.updated_at
    if updated_at is None:
        return False
    if updated_at.tzinfo is None:
        # Treat naive timestamps as UTC to keep the boundary safe on
        # databases that do not enforce timezone-aware columns.
        updated_at = updated_at.replace(tzinfo=UTC)
    return updated_at.date() == target


__all__ = ["StatsService"]
