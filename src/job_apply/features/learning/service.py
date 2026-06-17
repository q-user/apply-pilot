"""High-level facade for the learning-signals slice (M8, issue #63).

The module exposes:

* :class:`LearningSignal` — frozen value object (the public surface).
* :class:`LearningSignalsService` — high-level facade around
  :class:`LearningSignalRepository`.

The :class:`LearningSignalRepository` Protocol and the two
implementations live in
:mod:`job_apply.features.learning.repository`. Keeping the value
object in the same module as the facade makes it easy to import
both from a single name — the future prompt-tuning pipeline will
just import :class:`LearningSignal` from
:mod:`job_apply.features.learning.service`.

The service is fire-and-forget: a write that fails to persist is
logged and swallowed, mirroring the contract of
:class:`job_apply.features.audit.service.AuditService`. A learning
signal that fails to persist must never break the user-facing
``/reject`` action.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from job_apply.features.learning.models import LearningSignal
from job_apply.features.learning.repository import LearningSignalRepository

_LOGGER = logging.getLogger("job_apply.features.learning.service")


# ---------------------------------------------------------------------------
# Service facade
# ---------------------------------------------------------------------------


class LearningSignalsService:
    """High-level facade around :class:`LearningSignalRepository`.

    The only public write method the rest of the app needs today is
    :meth:`record_rejection`; future producers (``dismissal``,
    ``low_score``) will get their own methods. The service is
    fire-and-forget: a write that fails to persist is logged and
    swallowed, mirroring the contract of
    :class:`job_apply.features.audit.service.AuditService`.
    """

    def __init__(self, repo: LearningSignalRepository) -> None:
        self._repo = repo

    def record_rejection(
        self,
        *,
        user_id: uuid.UUID,
        match_id: uuid.UUID,
        vacancy_id: uuid.UUID,
        search_profile_id: uuid.UUID,
        reason: str | None,
        score: float | None,
        prompt_version: str | None,
    ) -> LearningSignal:
        """Build and persist a ``rejection`` learning signal.

        ``score`` and ``prompt_version`` are the score / prompt the
        match carried at the moment the user rejected it. They are
        ``None`` on freshly-ingested matches. ``reason`` is the
        free-form text from the ``/reject <match_id> [reason]``
        command, or ``None`` when the user did not supply one.
        """
        signal = LearningSignal(
            id=uuid.uuid4(),
            user_id=user_id,
            match_id=match_id,
            vacancy_id=vacancy_id,
            search_profile_id=search_profile_id,
            rejection_reason=reason,
            prompt_version=prompt_version,
            score=score,
            signal_type="rejection",
            created_at=datetime.now(UTC),
        )
        try:
            return self._repo.record(signal)
        except Exception:
            _LOGGER.exception(
                "learning.record_rejection.failed",
                extra={"event": "learning.record_rejection.failed", "user_id": str(user_id)},
            )
            return signal

    def list_for_user(self, user_id: uuid.UUID, *, limit: int = 100) -> Sequence[LearningSignal]:
        """Read signals for a user; thin pass-through to the repository."""
        return self._repo.list_for_user(user_id, limit=limit)

    @property
    def repo(self) -> LearningSignalRepository:
        """Expose the repository for tests that need to assert state."""
        return self._repo


__all__ = ["LearningSignal", "LearningSignalsService"]
