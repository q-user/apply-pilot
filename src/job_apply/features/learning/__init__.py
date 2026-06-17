"""Learning-signals slice (M8, issue #63).

Captures structured "user rejected this match" events so the future
prompt-tuning pipeline (issue #29 / the M8 follow-ups) can read them
out by user, by prompt version, or by time window. The slice is a
small, self-contained vertical with its own table, repository, and
read-only HTTP endpoint; it does not own any background jobs.

Public surface (re-exported from this module):

* :class:`LearningSignal` — frozen value object describing a single
  signal row.
* :class:`LearningSignalRepository` — Protocol every implementation
  satisfies.
* :class:`InMemoryLearningSignalRepository` — list-backed fake.
* :class:`SqlLearningSignalRepository` — SQLAlchemy-backed production
  implementation.
* :class:`LearningSignalsService` — high-level facade.
* :class:`LearningSignalRead` — Pydantic DTO for the read endpoint.
"""

from __future__ import annotations

from job_apply.features.learning.models import LearningSignalRow
from job_apply.features.learning.repository import (
    InMemoryLearningSignalRepository,
    LearningSignalRepository,
    SqlLearningSignalRepository,
)
from job_apply.features.learning.schemas import LearningSignalRead
from job_apply.features.learning.service import (
    LearningSignal,
    LearningSignalsService,
)

__all__ = [
    "InMemoryLearningSignalRepository",
    "LearningSignal",
    "LearningSignalRead",
    "LearningSignalRepository",
    "LearningSignalRow",
    "LearningSignalsService",
    "SqlLearningSignalRepository",
]
