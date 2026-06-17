"""SQLAlchemy-backed tests for the scoring_review slice (M8, issue #68).

These tests stand up a fresh sqlite in-memory engine with the full
project metadata, persist a small set of ``VacancyMatch`` /
``SearchProfile`` / ``Vacancy`` rows, and verify the
:class:`SqlScoringReviewQueue` and :class:`ScoringReviewService`
implementations behave the same as their in-memory counterparts.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from job_apply.db import Base
from job_apply.features.audit.repository import SqlAuditLogRepository
from job_apply.features.audit.service import AuditService
from job_apply.features.matches import models as _matches_models  # noqa: F401
from job_apply.features.matches.models import MatchStatus, VacancyMatch
from job_apply.features.scoring_review.repository import SqlScoringReviewQueue
from job_apply.features.scoring_review.service import ScoringReviewService
from job_apply.features.search_profiles import models as _sp_models  # noqa: F401
from job_apply.features.search_profiles.models import SearchProfile
from job_apply.features.sources import models as _sources_models  # noqa: F401
from job_apply.features.sources.models import Vacancy
from job_apply.features.users import models as _users_models  # noqa: F401
from job_apply.features.users.models import User
from job_apply.shared.errors import NotFoundError


@pytest.fixture
def session_factory() -> Iterator:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    try:
        yield factory
    finally:
        engine.dispose()


def _seed_user(session: Session) -> uuid.UUID:
    user = User(id=uuid.uuid4(), email=f"u{uuid.uuid4().hex[:8]}@example.com", hashed_password="x")
    session.add(user)
    session.flush()
    return user.id


def _seed_profile(session: Session, user_id: uuid.UUID) -> uuid.UUID:
    profile = SearchProfile(id=uuid.uuid4(), user_id=user_id, title="p", is_active=True)
    session.add(profile)
    session.flush()
    return profile.id


def _seed_vacancy(session: Session) -> uuid.UUID:
    vacancy = Vacancy(
        id=uuid.uuid4(),
        source="hh",
        source_id=f"hh-{uuid.uuid4().hex[:6]}",
        title="Python",
        raw_data={},
    )
    session.add(vacancy)
    session.flush()
    return vacancy.id


def _seed_match(
    session: Session,
    *,
    profile_id: uuid.UUID,
    confidence: float | None,
    vacancy_id: uuid.UUID | None = None,
    score: int | None = 10,
    explanation: str | None = "reason",
    prompt_version: str | None = "vacancy_scoring@v1",
) -> uuid.UUID:
    match = VacancyMatch(
        id=uuid.uuid4(),
        search_profile_id=profile_id,
        vacancy_id=vacancy_id or _seed_vacancy(session),
        status=MatchStatus.SCORED.value,
    )
    match.score = score
    match.confidence = confidence
    match.prompt_version = prompt_version
    match.explanation = explanation
    session.add(match)
    session.flush()
    return match.id


def test_sql_list_filters_orders_and_limits(session_factory) -> None:
    """The SQL queue must mirror the in-memory contract."""
    session: Session = session_factory()
    try:
        user_id = _seed_user(session)
        profile_id = _seed_profile(session, user_id)
        # Three matching + two non-matching + one null.
        _seed_match(session, profile_id=profile_id, confidence=0.1)
        _seed_match(session, profile_id=profile_id, confidence=0.45)
        _seed_match(session, profile_id=profile_id, confidence=0.3)
        _seed_match(session, profile_id=profile_id, confidence=0.9)
        _seed_match(session, profile_id=profile_id, confidence=0.8)
        _seed_match(session, profile_id=profile_id, confidence=None)
        session.commit()
    finally:
        session.close()

    queue = SqlScoringReviewQueue(session_factory=session_factory)
    rows = queue.list_low_confidence(threshold=0.5, limit=2, since=None)

    assert [r.confidence for r in rows] == [0.1, 0.3]
    assert rows[0].user_id == user_id
    assert rows[0].search_profile_id == profile_id
    assert rows[0].prompt_version == "vacancy_scoring@v1"
    assert rows[0].explanation == "reason"
    assert rows[0].score == 10


def test_sql_list_excludes_unscored(session_factory) -> None:
    """Null-confidence rows must be excluded."""
    session: Session = session_factory()
    try:
        user_id = _seed_user(session)
        profile_id = _seed_profile(session, user_id)
        _seed_match(session, profile_id=profile_id, confidence=None)
        _seed_match(session, profile_id=profile_id, confidence=0.2)
        session.commit()
    finally:
        session.close()

    queue = SqlScoringReviewQueue(session_factory=session_factory)
    rows = queue.list_low_confidence(threshold=0.5, limit=50, since=None)

    assert len(rows) == 1
    assert rows[0].confidence == 0.2


def test_sql_mark_reviewed_writes_audit_event(session_factory) -> None:
    """``mark_reviewed`` via the service must append a MATCH_REVIEWED audit row."""
    session: Session = session_factory()
    try:
        user_id = _seed_user(session)
        profile_id = _seed_profile(session, user_id)
        match_id = _seed_match(session, profile_id=profile_id, confidence=0.1)
        session.commit()
    finally:
        session.close()

    queue = SqlScoringReviewQueue(session_factory=session_factory)
    audit_repo = SqlAuditLogRepository(session_factory=session_factory)
    service = ScoringReviewService(queue=queue, audit_service=AuditService(audit_repo=audit_repo))

    service.mark_reviewed(match_id, reviewer_note="spammy")

    logs = audit_repo.list_by_event_type("match_reviewed")
    assert len(logs) == 1
    assert logs[0].details is not None
    payload = json.loads(logs[0].details)
    assert payload["match_id"] == str(match_id)
    assert payload["note"] == "spammy"
    # The audit event is recorded as a system-level annotation; no user_id is
    # attached because the reviewer is an admin operator, not a regular user.
    assert logs[0].user_id is None


def test_sql_mark_reviewed_raises_for_unknown_match(session_factory) -> None:
    """``mark_reviewed`` must raise :class:`NotFoundError` for unknown ids."""
    queue = SqlScoringReviewQueue(session_factory=session_factory)
    audit_repo = SqlAuditLogRepository(session_factory=session_factory)
    service = ScoringReviewService(queue=queue, audit_service=AuditService(audit_repo=audit_repo))

    with pytest.raises(NotFoundError):
        service.mark_reviewed(uuid.uuid4(), reviewer_note="x")


def test_sql_threshold_is_strict(session_factory) -> None:
    """``confidence < threshold`` excludes the boundary value."""
    session: Session = session_factory()
    try:
        user_id = _seed_user(session)
        profile_id = _seed_profile(session, user_id)
        _seed_match(session, profile_id=profile_id, confidence=0.5)
        _seed_match(session, profile_id=profile_id, confidence=0.4999)
        session.commit()
    finally:
        session.close()

    queue = SqlScoringReviewQueue(session_factory=session_factory)
    rows = queue.list_low_confidence(threshold=0.5, limit=50, since=None)

    assert [r.confidence for r in rows] == [0.4999]
