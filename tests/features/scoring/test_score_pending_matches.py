"""Regression tests for :meth:`ScoringService.score_pending_matches`.

Covers the fail-soft contract documented in the method's docstring:
a per-match scoring error must be logged and the loop must continue
to the next match. The original implementation had no ``try/except``
around ``await self.score_match(match)``, so a single failing match
aborted the entire batch (issue #141).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime

import pytest

from apply_pilot.features.matches.models import MatchStatus, VacancyMatch
from apply_pilot.features.matches.repository import InMemoryVacancyMatchRepository
from apply_pilot.features.scoring.llm import (
    WILDCARD_PROMPT,
    InMemoryLLMClient,
    LLMScorer,
)
from apply_pilot.features.scoring.service import ScoringService
from apply_pilot.features.search_profiles.models import SearchProfile
from apply_pilot.features.sources.models import Vacancy


def _vacancy(*, source_id: str) -> Vacancy:
    v = Vacancy(
        source="hh",
        source_id=source_id,
        title="Backend Python",
        description="FastAPI + Postgres",
        url=f"https://hh.ru/vacancy/{source_id}",
        salary_from=200_000,
        salary_to=350_000,
        salary_currency="RUR",
        salary_gross=False,
        employer_name="Acme",
        location="Москва",
        schedule="remote",
        experience="5+ years",
        skills=["Python", "FastAPI"],
        raw_data={"id": source_id},
    )
    v.id = uuid.uuid4()
    return v


def _profile(user_id: uuid.UUID) -> SearchProfile:
    p = SearchProfile(
        user_id=user_id,
        title="Backend Python",
        keywords="python, fastapi",
        salary_min=200_000,
        salary_max=400_000,
        location="Москва",
        schedule="remote",
        is_active=True,
    )
    p.id = uuid.uuid4()
    return p


def _pending_match(profile: SearchProfile, vacancy: Vacancy) -> VacancyMatch:
    match = VacancyMatch(
        search_profile_id=profile.id,
        vacancy_id=vacancy.id,
        status=MatchStatus.NEW.value,
    )
    match.id = uuid.uuid4()
    match.created_at = datetime.now(UTC)
    match.search_profile = profile  # type: ignore[attr-defined]
    match.vacancy = vacancy  # type: ignore[attr-defined]
    return match


class _FlakyScoreMatchService(ScoringService):
    """Scoring service that raises for one specific match id.

    The base service's ``score_match`` is wrapped so the test can inject
    a per-match failure deterministically. Other matches fall through
    to the parent's real implementation, exercising the end-to-end
    pipeline.
    """

    def __init__(self, *, fail_match_id: uuid.UUID, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._fail_match_id = fail_match_id
        self.calls: list[uuid.UUID] = []

    async def score_match(self, match: VacancyMatch, **_: object) -> VacancyMatch:
        self.calls.append(match.id)
        if match.id == self._fail_match_id:
            raise RuntimeError("boom: scoring backend exploded")
        return await super().score_match(match, **_)


class TestScorePendingMatchesFailSoft:
    @pytest.mark.asyncio
    async def test_per_match_error_does_not_abort_batch(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        repo = InMemoryVacancyMatchRepository()
        user_id = uuid.uuid4()
        profile = _profile(user_id)

        v1 = _vacancy(source_id="v-1")
        v2 = _vacancy(source_id="v-2")
        v3 = _vacancy(source_id="v-3")
        m1 = _pending_match(profile, v1)
        m2 = _pending_match(profile, v2)
        m3 = _pending_match(profile, v3)
        for m in (m1, m2, m3):
            repo.create(m)

        client = InMemoryLLMClient(
            responses={
                WILDCARD_PROMPT: json.dumps({"score": 60, "explanation": "ok", "confidence": 0.5})
            }
        )
        scorer = LLMScorer(llm=client)
        service: ScoringService = _FlakyScoreMatchService(
            fail_match_id=m2.id,
            scorer=scorer,
            match_repo=repo,
        )

        with caplog.at_level(logging.ERROR, logger="apply_pilot.features.scoring.service"):
            # Must NOT raise even though match #2 blows up.
            scored = await service.score_pending_matches()

        # Fail-soft: loop continued past the failing match, scoring the
        # remaining 2 successfully.
        assert scored == 2
        # All three matches were attempted (m1 ok, m2 raised, m3 ok).
        assert service.calls == [m1.id, m2.id, m3.id]  # type: ignore[attr-defined]
        # The successful matches got persisted.
        assert repo.get_by_id(m1.id).score == 60
        assert repo.get_by_id(m3.id).score == 60
        # The failing match stays unscored.
        assert repo.get_by_id(m2.id).score is None
        # And we logged the failure with the match id and exception class.
        failure_records = [
            r for r in caplog.records if "scoring failed for match_id" in r.getMessage()
        ]
        assert len(failure_records) == 1
        assert str(m2.id) in failure_records[0].getMessage()
        assert "RuntimeError" in failure_records[0].getMessage()
