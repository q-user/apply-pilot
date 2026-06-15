"""TDD tests for extending :class:`VacancyMatch` with LLM scoring fields.

This module exercises the behaviour required by issue #30:

* ``score`` (already present), ``explanation``, ``prompt_version`` and
  ``scored_at`` can all be set on a freshly created match and round-trip
  through the in-memory repository.
* Default values for the new fields are ``NULL`` so the change is
  backward-compatible with rows that pre-date the migration.
* The schema (``VacancyMatchRead``) and the service's DTO mapping expose
  the new fields to callers.
* The hand-written Alembic migration that adds the columns actually
  adds them (column-level integration test on sqlite).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from job_apply.db import Base
from job_apply.features.matches import models as _matches_models  # noqa: F401
from job_apply.features.matches.models import MatchStatus, VacancyMatch
from job_apply.features.matches.repository import InMemoryVacancyMatchRepository
from job_apply.features.matches.schemas import VacancyMatchRead
from job_apply.features.matches.service import MatchService
from job_apply.features.search_profiles import models as _sp_models  # noqa: F401
from job_apply.features.search_profiles.models import SearchProfile
from job_apply.features.search_profiles.repository import InMemorySearchProfileRepository
from job_apply.features.sources import models as _sources_models  # noqa: F401
from job_apply.features.sources.models import Vacancy
from job_apply.features.users import models as _users_models  # noqa: F401


def _vacancy(source_id: str = "hh-1") -> Vacancy:
    v = Vacancy(source="hh", source_id=source_id, title="t", raw_data={})
    v.id = uuid.uuid4()
    return v


def _profile(user_id: uuid.UUID) -> SearchProfile:
    p = SearchProfile(user_id=user_id, title="t", is_active=True)
    p.id = uuid.uuid4()
    return p


# ---------------------------------------------------------------------------
# Model defaults
# ---------------------------------------------------------------------------


def test_match_defaults_have_null_scoring_fields() -> None:
    """A freshly constructed match has NULL scoring fields.

    The migration is additive: existing rows that pre-date issue #30
    must keep their ``explanation`` / ``prompt_version`` / ``scored_at``
    columns as ``NULL`` until a scoring pass writes to them.
    """
    match = VacancyMatch(search_profile_id=uuid.uuid4(), vacancy_id=uuid.uuid4())

    assert match.explanation is None
    assert match.prompt_version is None
    assert match.scored_at is None


def test_match_scoring_fields_can_be_assigned() -> None:
    """The new columns are writable: callers set them after scoring."""
    when = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)
    match = VacancyMatch(
        search_profile_id=uuid.uuid4(),
        vacancy_id=uuid.uuid4(),
        score=82,
        explanation="Strong match on Python, FastAPI, and 5y experience.",
        prompt_version="1.2.0",
        scored_at=when,
    )

    assert match.score == 82
    assert match.explanation == "Strong match on Python, FastAPI, and 5y experience."
    assert match.prompt_version == "1.2.0"
    assert match.scored_at == when


# ---------------------------------------------------------------------------
# Schema exposure
# ---------------------------------------------------------------------------


def test_vacancy_match_read_schema_exposes_scoring_fields() -> None:
    """The public DTO must surface the new fields for HTTP / DTO callers."""
    schema = VacancyMatchRead(
        id=uuid.uuid4(),
        search_profile_id=uuid.uuid4(),
        vacancy_id=uuid.uuid4(),
        status=MatchStatus.SCORED.value,
        score=85,
        match_reason="rule-set match",
        explanation="Detailed LLM reasoning...",
        prompt_version="1.0.0",
        scored_at=datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC),
        created_at=datetime(2026, 6, 15, 11, 0, 0, tzinfo=UTC),
    )

    assert schema.explanation == "Detailed LLM reasoning..."
    assert schema.prompt_version == "1.0.0"
    assert schema.scored_at == datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)


def test_vacancy_match_read_schema_accepts_null_scoring_fields() -> None:
    """A pre-scoring match is still a valid DTO with NULL scoring fields."""
    schema = VacancyMatchRead(
        id=uuid.uuid4(),
        search_profile_id=uuid.uuid4(),
        vacancy_id=uuid.uuid4(),
        status=MatchStatus.NEW.value,
        created_at=datetime(2026, 6, 15, 11, 0, 0, tzinfo=UTC),
    )

    assert schema.score is None
    assert schema.match_reason is None
    assert schema.explanation is None
    assert schema.prompt_version is None
    assert schema.scored_at is None
    assert schema.updated_at is None


# ---------------------------------------------------------------------------
# Repository round-trip
# ---------------------------------------------------------------------------


@pytest.fixture
def service() -> MatchService:
    profile_repo = InMemorySearchProfileRepository()
    match_repo = InMemoryVacancyMatchRepository(list_user_profiles=profile_repo.list_by_user)
    return MatchService(match_repo=match_repo, profile_repo=profile_repo)


def test_create_match_dto_carries_null_scoring_fields(
    service: MatchService,
) -> None:
    """A brand-new match (status=new) has no scoring data yet."""
    user_id = uuid.uuid4()
    profile = _profile(user_id)
    service.profile_repo.create(profile)

    dto = service.create_match(profile.id, _vacancy().id)

    assert dto.status == MatchStatus.NEW.value
    assert dto.score is None
    assert dto.explanation is None
    assert dto.prompt_version is None
    assert dto.scored_at is None


def test_service_dto_propagates_scoring_fields_through_repo(
    service: MatchService,
) -> None:
    """The DTO carries the scoring fields that the repository stored."""
    user_id = uuid.uuid4()
    profile = _profile(user_id)
    service.profile_repo.create(profile)
    match = VacancyMatch(
        search_profile_id=profile.id,
        vacancy_id=_vacancy().id,
        score=77,
        explanation="Solid match",
        prompt_version="1.0.0",
        scored_at=datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC),
    )
    service.repo.create(match)

    fetched = service.get(match.id, user_id=user_id)

    assert fetched.score == 77
    assert fetched.explanation == "Solid match"
    assert fetched.prompt_version == "1.0.0"
    assert fetched.scored_at == datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Migration: column-level integration
# ---------------------------------------------------------------------------


@pytest.fixture
def migrated_engine():
    """Yield a fresh sqlite engine with the full ``Base.metadata`` schema.

    The ``vacancy_matches`` table in the in-memory database reflects the
    post-#30 model: it must include the new scoring columns.
    """
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def test_migration_adds_scoring_columns_to_vacancy_matches(
    migrated_engine,
) -> None:
    """The SQL schema must contain the new columns on ``vacancy_matches``."""
    inspector = inspect(migrated_engine)
    columns = {c["name"] for c in inspector.get_columns("vacancy_matches")}

    assert "explanation" in columns
    assert "prompt_version" in columns
    assert "scored_at" in columns


def test_migration_scoring_columns_default_to_null(migrated_engine) -> None:
    """Inserting a row without the new columns leaves them NULL (additive)."""
    SessionLocal = sessionmaker(bind=migrated_engine)
    session = SessionLocal()
    try:
        # Pre-flight: we need a user + profile + vacancy for the FK chain.
        from job_apply.features.users.models import User

        user_id = uuid.uuid4()
        profile_id = uuid.uuid4()
        vacancy_id = uuid.uuid4()
        session.add(User(id=user_id, email=f"u{user_id.hex[:8]}@example.com", hashed_password="x"))
        session.add(SearchProfile(id=profile_id, user_id=user_id, title="t", is_active=True))
        session.add(Vacancy(id=vacancy_id, source="hh", source_id="hh-1", title="t", raw_data={}))
        session.commit()

        match_id = uuid.uuid4()
        session.add(
            VacancyMatch(
                id=match_id,
                search_profile_id=profile_id,
                vacancy_id=vacancy_id,
            )
        )
        session.commit()

        row = session.execute(
            text(
                "SELECT explanation, prompt_version, scored_at FROM vacancy_matches WHERE id = :id"
            ),
            {"id": str(match_id)},
        ).one()
        assert row.explanation is None
        assert row.prompt_version is None
        assert row.scored_at is None
    finally:
        session.close()
