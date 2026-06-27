"""Persistence gateway for the scoring review slice (M8, issue #68).

The slice exposes a read-mostly queue plus a single ``mark_reviewed``
writer that records a reviewer note as an ``AuditEventType.MATCH_REVIEWED``
event. Three implementations live here, mirroring the conventions used
by the ``matches`` and ``audit`` slices:

* :class:`ScoringReviewQueue` — Protocol the service depends on.
* :class:`InMemoryScoringReviewQueue` — list-backed fake for tests.
* :class:`SqlScoringReviewQueue` — production implementation backed by
  a SQLAlchemy ``Session``. The ``list_low_confidence`` method joins
  through ``search_profiles`` to resolve the ``user_id`` in a single
  query; ``mark_reviewed`` only verifies the match exists and lets the
  service write the audit row (the queue never persists the note).

The slice deliberately re-uses the existing ``vacancy_matches`` table —
no new columns, no new migrations. Reviewer notes live in the
``audit_logs.details`` JSON column under a new ``match_reviewed`` event
type (see :class:`apply_pilot.features.audit.models.AuditEventType`).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from apply_pilot.features.matches.models import VacancyMatch
from apply_pilot.features.matches.repository import VacancyMatchRepository
from apply_pilot.features.scoring_review.models import LowConfidenceMatch
from apply_pilot.features.search_profiles.models import SearchProfile
from apply_pilot.features.search_profiles.repository import SearchProfileRepository
from apply_pilot.shared.errors import NotFoundError


class ScoringReviewQueue(Protocol):
    """Minimal interface the :class:`ScoringReviewService` relies on.

    ``list_low_confidence`` returns the slice's value object so the
    service and API layer never touch the ORM directly. ``mark_reviewed``
    only validates that the match exists; the actual note is recorded
    by the service via the audit slice so the queue's contract stays
    focused on read-side state.
    """

    def list_low_confidence(
        self,
        threshold: float,
        limit: int,
        since: datetime | None,
    ) -> Sequence[LowConfidenceMatch]: ...

    def mark_reviewed(self, match_id: uuid.UUID) -> None: ...


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------


class InMemoryScoringReviewQueue:
    """List-backed queue for tests.

    The queue borrows the existing :class:`VacancyMatchRepository` for
    the raw match data and the :class:`SearchProfileRepository` to
    resolve the ``user_id``. Tests wire the two fakes together so the
    in-memory queue mirrors the SQL behaviour 1:1.
    """

    def __init__(
        self,
        *,
        match_repo: VacancyMatchRepository,
        profile_repo: SearchProfileRepository,
    ) -> None:
        self._match_repo = match_repo
        self._profile_repo = profile_repo

    def list_low_confidence(
        self,
        threshold: float,
        limit: int,
        since: datetime | None,
    ) -> Sequence[LowConfidenceMatch]:
        """Return every scored match with ``confidence < threshold``.

        The result is ordered by ``confidence ASC`` (least confident
        first), with ``created_at`` as a deterministic tie-breaker.
        Matches whose ``confidence`` is ``NULL`` (still unscored) are
        excluded — they belong in the scoring queue, not the review
        one.
        """
        rows = list(_all_match_rows(self._match_repo))
        rows = [
            m for m in rows if m.confidence is not None and float(m.confidence) < float(threshold)
        ]
        if since is not None:
            rows = [m for m in rows if m.created_at is not None and m.created_at >= since]
        rows.sort(key=lambda m: (m.confidence, m.created_at))
        return [_to_dto(self._profile_repo, m) for m in rows[:limit]]

    def mark_reviewed(self, match_id: uuid.UUID) -> None:
        """Raise :class:`NotFoundError` if the match does not exist.

        The method intentionally performs no mutation: the reviewer note
        is recorded by the :class:`ScoringReviewService` via the audit
        slice. The queue only exists to validate that the target match
        is real before the service logs the event.
        """
        match = self._match_repo.get_by_id(match_id)
        if match is None:
            raise NotFoundError.for_entity("vacancy match", match_id)


def _all_match_rows(match_repo: VacancyMatchRepository) -> Sequence[VacancyMatch]:
    """Return every :class:`VacancyMatch` the in-memory repo stores.

    The :class:`InMemoryVacancyMatchRepository` exposes a private
    ``_by_id`` dict; the SQL implementation already returns the full
    set in a single round-trip so it never reaches this path.
    """
    by_id = getattr(match_repo, "_by_id", None)
    if isinstance(by_id, dict):
        items: list[VacancyMatch] = list(by_id.values())
    return items
    # Defensive fallback for fakes that don't expose the storage dict:
    # call the public read method and ask for a very large limit.
    return list(match_repo.list_by_profile(uuid.uuid4(), limit=10_000))


def _to_dto(
    profile_repo: SearchProfileRepository,
    match: VacancyMatch,
) -> LowConfidenceMatch:
    profile = profile_repo.get_by_id(match.search_profile_id)
    # A match that points at a missing profile is broken state, but the
    # queue must still surface it (the admin needs to see it). Use the
    # match's profile id as a synthetic user id so the DTO is well-formed;
    # the value is never persisted.
    user_id = match.search_profile_id if profile is None else profile.user_id
    return LowConfidenceMatch(
        match_id=match.id,
        vacancy_id=match.vacancy_id,
        user_id=user_id,
        search_profile_id=match.search_profile_id,
        score=match.score,
        confidence=match.confidence,
        prompt_version=match.prompt_version,
        explanation=match.explanation,
        created_at=match.created_at,
    )


# ---------------------------------------------------------------------------
# SQLAlchemy implementation
# ---------------------------------------------------------------------------


class SqlScoringReviewQueue:
    """SQLAlchemy-backed queue.

    The repository opens a short-lived session per operation and closes
    it before returning. ``list_low_confidence`` joins ``vacancy_matches``
    to ``search_profiles`` so the admin can see the user id without a
    second round-trip; ``mark_reviewed`` only validates the match exists
    and lets the service emit the audit event.
    """

    def __init__(
        self,
        session: Session | None = None,
        *,
        session_factory: Callable[[], Session] | None = None,
    ) -> None:
        if session is not None and session_factory is not None:
            raise ValueError("pass either session or session_factory, not both")
        self._session = session
        self._session_factory = session_factory

    def _scope(self) -> Session:
        if self._session is not None:
            return self._session
        if self._session_factory is None:
            raise RuntimeError("SqlScoringReviewQueue is not bound to a session")
        return self._session_factory()

    def list_low_confidence(
        self,
        threshold: float,
        limit: int,
        since: datetime | None,
    ) -> Sequence[LowConfidenceMatch]:
        session = self._scope()
        try:
            statement = (
                select(VacancyMatch, SearchProfile)
                .join(SearchProfile, SearchProfile.id == VacancyMatch.search_profile_id)
                .where(
                    VacancyMatch.confidence.is_not(None),
                    VacancyMatch.confidence < threshold,
                )
                .order_by(VacancyMatch.confidence.asc(), VacancyMatch.created_at.asc())
                .limit(limit)
            )
            if since is not None:
                statement = statement.where(VacancyMatch.created_at >= since)
            rows = session.execute(statement).all()
            return [
                LowConfidenceMatch(
                    match_id=match.id,
                    vacancy_id=match.vacancy_id,
                    user_id=profile.user_id,
                    search_profile_id=match.search_profile_id,
                    score=match.score,
                    confidence=match.confidence,
                    prompt_version=match.prompt_version,
                    explanation=match.explanation,
                    created_at=match.created_at,
                )
                for match, profile in rows
            ]
        finally:
            if self._session is None:
                session.close()

    def mark_reviewed(self, match_id: uuid.UUID) -> None:
        session = self._scope()
        try:
            match = session.get(VacancyMatch, match_id)
            if match is None:
                raise NotFoundError.for_entity("vacancy match", match_id)
        finally:
            if self._session is None:
                session.close()


__all__ = [
    "InMemoryScoringReviewQueue",
    "ScoringReviewQueue",
    "SqlScoringReviewQueue",
]
