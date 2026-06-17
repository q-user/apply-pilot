"""Matches vertical slice.

Links canonical :class:`Vacancy` rows to user-owned
:class:`SearchProfile` rows. The slice owns:

* :mod:`.models` — ``VacancyMatch`` ORM model and the ``MatchStatus`` enum.
* :mod:`.schemas` — request/response DTOs.
* :mod:`.repository` — Protocol + in-memory + SQLAlchemy implementations.
* :mod:`.service` — business rules (idempotency, skip-on-conflict bulk
  insertion, ownership checks).
* :mod:`.api` — FastAPI router.
"""

from __future__ import annotations

from apply_pilot.features.matches.models import MatchStatus, VacancyMatch
from apply_pilot.features.matches.repository import (
    InMemoryVacancyMatchRepository,
    SqlVacancyMatchRepository,
    VacancyMatchRepository,
)
from apply_pilot.features.matches.schemas import VacancyMatchRead, VacancyMatchStatusUpdate
from apply_pilot.features.matches.service import (
    MatchNotFoundError,
    MatchOwnershipError,
    MatchService,
)

__all__ = [
    "InMemoryVacancyMatchRepository",
    "MatchNotFoundError",
    "MatchOwnershipError",
    "MatchService",
    "MatchStatus",
    "SqlVacancyMatchRepository",
    "VacancyMatch",
    "VacancyMatchRead",
    "VacancyMatchRepository",
    "VacancyMatchStatusUpdate",
]
