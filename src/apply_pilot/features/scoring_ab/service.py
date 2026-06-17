"""Use-case service for the scoring A/B experiment slice (issue #65).

The :class:`ScoringExperimentService` is the orchestration surface the
:class:`~apply_pilot.features.scoring.service.ScoringService` consults on
every scoring call. It owns:

* :meth:`assign_variant` — deterministic, weight-based hash bucketing
  that maps a ``(user_id, experiment_name)`` pair to one of the
  experiment's variants. The same pair always returns the same
  variant; the empirical distribution across many users follows the
  configured weights.
* :meth:`record_outcome` — appends a single outcome row to the
  underlying repository. The method is fire-and-forget: errors are
  logged and swallowed so a misbehaving experiment store never breaks
  the scoring hot path.

Hash bucketing
--------------

The bucket is computed as ``hash((user_id, experiment_name))`` mapped
into ``[0.0, 1.0)`` and then projected onto the cumulative-weight
range. ``hashlib.sha256`` is used instead of Python's built-in
:func:`hash` (which is randomised per-process) so the same
``(user_id, experiment_name)`` always returns the same variant across
process restarts, worker boundaries, and test runs.

The bucket key is ``(user_id, experiment_name)``; ``vacancy_id`` is
deliberately excluded so the same user always lands in the same
variant across many vacancies. The spec for issue #65 makes the
"independent of vacancy_id" invariant explicit and the test in
:mod:`tests.features.scoring_ab.test_service` enforces it.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from apply_pilot.features.scoring_ab.experiments import (
    ScoringExperimentRepository,
    ScoringVariant,
)

_LOGGER = logging.getLogger("apply_pilot.features.scoring_ab.service")


#: Callable that resolves the user id from a domain object. The default
#: (used by the in-memory tests) reads the ``user_id`` off the
#: attached search profile; the production wiring passes a join-based
#: resolver. The protocol keeps the service compileable without
#: depending on a concrete type.
MatchToUserId = Callable[[object], uuid.UUID]


@runtime_checkable
class _HasUserId(Protocol):
    """Duck-typed protocol the service reads the user id from."""

    user_id: uuid.UUID


def _default_match_to_user_id(match: object) -> uuid.UUID:
    """Default resolver: read ``user_id`` off the match's search profile.

    The in-memory tests attach :attr:`search_profile` to the match and
    the SQL production wiring passes a join-based resolver. This
    fallback exists for tests that only have a search profile directly
    on the match.
    """
    user_id = getattr(match, "user_id", None)
    if user_id is None:
        raise RuntimeError("cannot resolve user_id from match; pass a `match_to_user_id` callable")
    return user_id


# ---------------------------------------------------------------------------
# Bucketing
# ---------------------------------------------------------------------------

#: Number of distinct buckets the hash is mapped into. The granularity
#: is the full 32-bit range of ``sha256``'s first four bytes — large
#: enough that the cumulative-weight projection never has to deal
#: with quantization artifacts for an 80/20 split.
_BUCKET_COUNT: int = 1 << 32


def _bucket_for(user_id: uuid.UUID, experiment_name: str) -> float:
    """Map a ``(user_id, experiment_name)`` pair to a value in ``[0.0, 1.0)``.

    Uses :mod:`hashlib` (sha256) so the bucket is stable across
    process restarts; Python's built-in :func:`hash` would change
    between interpreter invocations.
    """
    key = f"{user_id}:{experiment_name}".encode()
    digest = hashlib.sha256(key).digest()
    # Use the first 4 bytes as an unsigned 32-bit int. Bytes-to-int is
    # deterministic and endian-portable so the bucket is identical on
    # any platform.
    raw = int.from_bytes(digest[:4], byteorder="big", signed=False)
    return raw / _BUCKET_COUNT


def _pick_variant(variants: list[ScoringVariant], bucket: float) -> ScoringVariant | None:
    """Project ``bucket`` onto the cumulative-weight range of ``variants``.

    Returns the first variant whose cumulative weight is strictly
    greater than the bucket. Variants are processed in the order they
    were declared by the caller; the repository's ``list_all`` orders
    them by name for stable behaviour.
    """
    cumulative = 0.0
    for variant in variants:
        cumulative += variant.weight
        if bucket < cumulative:
            return variant
    # Numerical edge case (bucket == 1.0 or accumulated rounding
    # error) — return the last variant.
    return variants[-1] if variants else None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ScoringExperimentService:
    """Orchestrate variant assignment + outcome recording for the A/B slice."""

    __slots__ = ("_repo",)

    def __init__(self, repo: ScoringExperimentRepository) -> None:
        self._repo = repo

    @property
    def repo(self) -> ScoringExperimentRepository:
        """Return the injected repository (read-only)."""
        return self._repo

    def assign_variant(
        self,
        *,
        user_id: uuid.UUID,
        vacancy_id: uuid.UUID,
        experiment_name: str,
    ) -> ScoringVariant | None:
        """Return the variant the user is bucketed into for ``experiment_name``.

        The bucket is deterministic in ``(user_id, experiment_name)``;
        ``vacancy_id`` is accepted for API symmetry but does not
        influence the result. Returns ``None`` when no active
        experiment exists for ``experiment_name`` — callers fall
        through to the baseline (registry) prompt version.
        """
        experiment = self._repo.get_active(experiment_name)
        if experiment is None:
            return None
        if not experiment.variants:
            return None
        bucket = _bucket_for(user_id, experiment.name)
        return _pick_variant(experiment.variants, bucket)

    def record_outcome(
        self,
        *,
        experiment_id: uuid.UUID,
        variant_name: str,
        user_id: uuid.UUID,
        vacancy_id: uuid.UUID,
        score: float,
        accepted: bool,
    ) -> None:
        """Append a single outcome row.

        Errors are caught and logged — the scoring hot path must not
        fail because the experiment store is misbehaving. The
        in-memory implementation never raises on a missing
        ``experiment_id``; the SQL implementation enforces the FK
        constraint and the service treats an FK violation as a
        fire-and-forget loss.
        """
        try:
            self._repo.record_outcome(
                experiment_id=experiment_id,
                variant_name=variant_name,
                user_id=user_id,
                vacancy_id=vacancy_id,
                score=score,
                accepted=accepted,
            )
        except Exception:
            _LOGGER.exception(
                "scoring_ab.record_outcome.failed",
                extra={"event": "scoring_ab.record_outcome.failed"},
            )


__all__ = [
    "MatchToUserId",
    "ScoringExperimentService",
]
