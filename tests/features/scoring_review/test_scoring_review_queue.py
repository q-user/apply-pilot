"""TDD unit tests for the scoring_review slice (M8, issue #68).

The slice exposes a manual review queue for low-confidence matches:

* :class:`LowConfidenceMatch` — frozen dataclass the queue returns.
* :class:`ScoringReviewQueue` — Protocol the service depends on.
* :class:`InMemoryScoringReviewQueue` — list-backed fake for tests.
* :class:`ScoringReviewService` — facade the API layer uses.

These tests describe the contract before the production implementation
is in place; the matching ``sql_*`` tests live in
``test_scoring_review_sql.py`` and exercise the SQLAlchemy-backed
implementation against a sqlite in-memory engine.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from apply_pilot.features.audit.repository import InMemoryAuditLogRepository
from apply_pilot.features.audit.service import AuditService
from apply_pilot.features.matches.models import MatchStatus, VacancyMatch
from apply_pilot.features.matches.repository import InMemoryVacancyMatchRepository
from apply_pilot.features.scoring_review.models import LowConfidenceMatch
from apply_pilot.features.scoring_review.repository import InMemoryScoringReviewQueue
from apply_pilot.features.scoring_review.service import ScoringReviewService
from apply_pilot.features.search_profiles.models import SearchProfile
from apply_pilot.features.search_profiles.repository import InMemorySearchProfileRepository

# ---------------------------------------------------------------------------
# LowConfidenceMatch dataclass
# ---------------------------------------------------------------------------


def test_low_confidence_match_is_frozen() -> None:
    """The dataclass must be immutable so the queue cannot be mutated downstream."""
    row = LowConfidenceMatch(
        match_id=uuid.uuid4(),
        vacancy_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        search_profile_id=uuid.uuid4(),
        score=42,
        confidence=0.3,
        prompt_version="vacancy_scoring@v1",
        explanation="low",
        created_at=datetime.now(UTC),
    )
    with pytest.raises((AttributeError, TypeError)):
        row.confidence = 0.9  # type: ignore[misc]


# ---------------------------------------------------------------------------
# InMemoryScoringReviewQueue
# ---------------------------------------------------------------------------


def _seed_match(
    match_repo: InMemoryVacancyMatchRepository,
    profile_repo: InMemorySearchProfileRepository,
    *,
    confidence: float | None,
    score: int | None = 10,
    prompt_version: str | None = "vacancy_scoring@v1",
    explanation: str | None = "reason",
    user_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Create a profile + match row and return (user_id, profile_id, match_id)."""
    owner = user_id or uuid.uuid4()
    profile = SearchProfile(user_id=owner, title="p", is_active=True)
    profile.id = uuid.uuid4()
    profile_repo.create(profile)
    match = VacancyMatch(
        search_profile_id=profile.id,
        vacancy_id=uuid.uuid4(),
        status=MatchStatus.SCORED.value,
    )
    match.score = score
    match.confidence = confidence
    match.prompt_version = prompt_version
    match.explanation = explanation
    match_repo.create(match)
    return owner, profile.id, match.id


class TestInMemoryQueue:
    def test_filters_by_threshold(
        self,
        match_repo: InMemoryVacancyMatchRepository,
        profile_repo: InMemorySearchProfileRepository,
    ) -> None:
        """Matches with confidence strictly below the threshold must come back."""
        _seed_match(match_repo, profile_repo, confidence=0.1)
        _seed_match(match_repo, profile_repo, confidence=0.4)
        _seed_match(match_repo, profile_repo, confidence=0.9)

        queue = InMemoryScoringReviewQueue(match_repo=match_repo, profile_repo=profile_repo)
        rows = queue.list_low_confidence(threshold=0.5, limit=50, since=None)

        confidences = sorted(r.confidence for r in rows)
        assert confidences == [0.1, 0.4]

    def test_orders_by_confidence_ascending(
        self,
        match_repo: InMemoryVacancyMatchRepository,
        profile_repo: InMemorySearchProfileRepository,
    ) -> None:
        """The least confident row must come first."""
        _seed_match(match_repo, profile_repo, confidence=0.45)
        _seed_match(match_repo, profile_repo, confidence=0.1)
        _seed_match(match_repo, profile_repo, confidence=0.3)

        queue = InMemoryScoringReviewQueue(match_repo=match_repo, profile_repo=profile_repo)
        rows = queue.list_low_confidence(threshold=0.5, limit=50, since=None)

        assert [r.confidence for r in rows] == [0.1, 0.3, 0.45]

    def test_respects_limit(
        self,
        match_repo: InMemoryVacancyMatchRepository,
        profile_repo: InMemorySearchProfileRepository,
    ) -> None:
        """The list must be capped at *limit* entries."""
        for _ in range(7):
            _seed_match(match_repo, profile_repo, confidence=0.1)

        queue = InMemoryScoringReviewQueue(match_repo=match_repo, profile_repo=profile_repo)
        rows = queue.list_low_confidence(threshold=0.5, limit=3, since=None)

        assert len(rows) == 3

    def test_filters_by_since(
        self,
        match_repo: InMemoryVacancyMatchRepository,
        profile_repo: InMemorySearchProfileRepository,
    ) -> None:
        """``since`` must restrict the listing to rows created at or after the cutoff."""
        # Insert a row, freeze ``since`` after it, then insert a second row.
        _seed_match(match_repo, profile_repo, confidence=0.2)
        cutoff = datetime.now(UTC)
        _seed_match(match_repo, profile_repo, confidence=0.3)

        queue = InMemoryScoringReviewQueue(match_repo=match_repo, profile_repo=profile_repo)
        rows = queue.list_low_confidence(threshold=0.5, limit=50, since=cutoff)

        # Only the row created after the cutoff must appear.
        assert len(rows) == 1
        assert rows[0].confidence == 0.3

    def test_excludes_null_confidence(
        self,
        match_repo: InMemoryVacancyMatchRepository,
        profile_repo: InMemorySearchProfileRepository,
    ) -> None:
        """Matches without a confidence score (still unscored) must not appear."""
        _seed_match(match_repo, profile_repo, confidence=None)
        _seed_match(match_repo, profile_repo, confidence=0.2)

        queue = InMemoryScoringReviewQueue(match_repo=match_repo, profile_repo=profile_repo)
        rows = queue.list_low_confidence(threshold=0.5, limit=50, since=None)

        assert len(rows) == 1
        assert rows[0].confidence == 0.2

    def test_returns_user_and_profile_ids(
        self,
        match_repo: InMemoryVacancyMatchRepository,
        profile_repo: InMemorySearchProfileRepository,
    ) -> None:
        """The DTO must carry the user_id and search_profile_id for the row."""
        owner, profile_id, _ = _seed_match(
            match_repo, profile_repo, confidence=0.2, user_id=uuid.uuid4()
        )

        queue = InMemoryScoringReviewQueue(match_repo=match_repo, profile_repo=profile_repo)
        rows = queue.list_low_confidence(threshold=0.5, limit=50, since=None)

        assert len(rows) == 1
        assert rows[0].user_id == owner
        assert rows[0].search_profile_id == profile_id

    def test_mark_reviewed_writes_audit_event(
        self,
        match_repo: InMemoryVacancyMatchRepository,
        profile_repo: InMemorySearchProfileRepository,
        audit_repo: InMemoryAuditLogRepository,
    ) -> None:
        """``mark_reviewed`` must append a MATCH_REVIEWED audit event with the note."""
        _seed_match(match_repo, profile_repo, confidence=0.1)
        queue = InMemoryScoringReviewQueue(match_repo=match_repo, profile_repo=profile_repo)
        rows = queue.list_low_confidence(threshold=0.5, limit=50, since=None)
        match_id = rows[0].match_id
        service = ScoringReviewService(
            queue=queue, audit_service=AuditService(audit_repo=audit_repo)
        )

        service.mark_reviewed(match_id, reviewer_note="looks fine")

        logs = audit_repo.list_by_event_type("match_reviewed")
        assert len(logs) == 1
        import json

        assert logs[0].details is not None
        payload = json.loads(logs[0].details)
        assert payload["match_id"] == str(match_id)
        assert payload["note"] == "looks fine"

    def test_mark_reviewed_raises_for_unknown_match(
        self,
        match_repo: InMemoryVacancyMatchRepository,
        profile_repo: InMemorySearchProfileRepository,
        audit_repo: InMemoryAuditLogRepository,
    ) -> None:
        """``mark_reviewed`` must raise when the match does not exist."""
        queue = InMemoryScoringReviewQueue(match_repo=match_repo, profile_repo=profile_repo)
        service = ScoringReviewService(
            queue=queue, audit_service=AuditService(audit_repo=audit_repo)
        )
        from apply_pilot.shared.errors import NotFoundError

        with pytest.raises(NotFoundError):
            service.mark_reviewed(uuid.uuid4(), reviewer_note="x")

    def test_threshold_uses_strict_inequality(
        self,
        match_repo: InMemoryVacancyMatchRepository,
        profile_repo: InMemorySearchProfileRepository,
    ) -> None:
        """``confidence < threshold`` excludes matches at the threshold itself."""
        _seed_match(match_repo, profile_repo, confidence=0.5)
        _seed_match(match_repo, profile_repo, confidence=0.4999)

        queue = InMemoryScoringReviewQueue(match_repo=match_repo, profile_repo=profile_repo)
        rows = queue.list_low_confidence(threshold=0.5, limit=50, since=None)

        assert [r.confidence for r in rows] == [0.4999]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def match_repo() -> InMemoryVacancyMatchRepository:
    return InMemoryVacancyMatchRepository()


@pytest.fixture
def profile_repo() -> InMemorySearchProfileRepository:
    return InMemorySearchProfileRepository()


@pytest.fixture
def audit_repo() -> InMemoryAuditLogRepository:
    return InMemoryAuditLogRepository()
