"""Retry policy for the ``apply_worker`` slice (M5, issue #47).

The :class:`RetryPolicy` owns the math that decides when a failed
:class:`~job_apply.features.apply_worker.models.ApplyJob` is requeued
and when it is dead-lettered. The class is intentionally small and
free of side effects so it can be exercised in isolation:

* :meth:`RetryPolicy.compute_next_run_at` — returns the next time a
  retryable failure should run, computed as
  ``min(base_delay * (multiplier ** (attempts - 1)), max_delay)`` with
  an optional ±10% jitter band.
* :meth:`RetryPolicy.should_retry` — ``True`` while the attempts
  counter is strictly below ``max_attempts``.

The class is collaborator-injected into the
:class:`~job_apply.features.apply_worker.service.ApplyJobService`
through its constructor. The service does not import this module's
defaults — wiring in :mod:`api` builds the policy from
:class:`job_apply.config.ApplyWorkerSettings` so the same code path
serves tests, local development, and production.

Jitter is deterministic when a seeded ``random.Random`` is supplied
(via the ``rng`` keyword argument). The default is the module-level
``random`` function so production usage does not have to think about
seeding; tests pass a seeded instance so the band assertions are
reproducible.
"""

from __future__ import annotations

import random as _random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

#: The jitter band is ±10% of the unjittered delay, applied *after*
#: the ``max_delay`` cap. The constant lives in this module (rather
#: than being a constructor argument) because every operational review
#: has converged on "10% is enough to break thundering herds and not
#: so much that a runaway delay becomes plausible".
_JITTER_RATIO: float = 0.1


class _SupportsRandom(Protocol):
    """Subset of :class:`random.Random` we rely on for jitter.

    Defined as a :class:`Protocol` so tests can pass either a seeded
    :class:`random.Random` instance or any other object that exposes
    a ``random()`` callable — keeps the dependency narrow.
    """

    def random(self) -> float: ...


@dataclass(frozen=True)
class RetryPolicy:
    """Exponential-backoff policy for the apply-worker queue.

    Attributes:
        max_attempts: Maximum number of attempts before the job is
            dead-lettered. ``should_retry(attempts)`` is ``True`` while
            ``attempts < max_attempts``. A job that has been tried
            ``max_attempts`` times and is still failing is parked in
            :attr:`ApplyJobStatus.DEAD_LETTER`.
        base_delay_seconds: Delay applied after the first attempt.
            Subsequent attempts grow as
            ``base_delay * (multiplier ** (attempts - 1))``.
        max_delay_seconds: Hard cap on the backoff delay. Once the
            unjittered value would exceed this, the cap wins. Jitter
            is then applied on top of the capped value.
        backoff_multiplier: Geometric growth factor between attempts.
        jitter: When ``True`` (the default), each computed delay is
            perturbed by a uniformly random factor in
            ``[1 - 0.1, 1 + 0.1]``. The point of jitter is to break
            thundering herds when many workers fail at once.

    The dataclass is frozen so callers can safely share one instance
    across the worker pool. Configuration changes require constructing
    a new policy.
    """

    max_attempts: int = 3
    base_delay_seconds: float = 2.0
    max_delay_seconds: float = 300.0
    backoff_multiplier: float = 2.0
    jitter: bool = True

    def compute_next_run_at(
        self,
        attempts: int,
        *,
        now: datetime | None = None,
        rng: _SupportsRandom | None = None,
    ) -> datetime:
        """Return the earliest time a retryable failure may be re-tried.

        Args:
            attempts: The number of attempts that have already been
                recorded on the row (post-``mark_attempt``). The
                formula uses ``attempts`` directly, so passing the
                freshly-incremented value yields the delay the worker
                should wait *after* the current failure.
            now: Reference "now" used for the addition. Defaults to
                :func:`datetime.now(UTC)`. The parameter is exposed so
                tests can pin a deterministic timestamp.
            rng: Random source for the jitter band. Defaults to the
                module-level :mod:`random` module so production code
                does not have to think about seeding. Tests pass a
                seeded ``random.Random(seed)`` instance for
                reproducibility.

        Returns:
            ``now + delay`` where ``delay`` is the (optionally
            jittered) exponential backoff.
        """
        if attempts < 1:
            raise ValueError(f"attempts must be a positive integer; got {attempts}")
        reference = now if now is not None else datetime.now(UTC)
        if reference.tzinfo is None:
            raise ValueError("'now' must be timezone-aware (UTC)")

        # 2 ** (attempts - 1) is geometric growth; the *floating-point*
        # ``**`` is what we want because ``backoff_multiplier`` is
        # allowed to be a float (e.g. 1.5 for gentler growth).
        raw = self.base_delay_seconds * (self.backoff_multiplier ** (attempts - 1))
        capped = min(raw, self.max_delay_seconds)

        delay = capped
        if self.jitter and capped > 0:
            source = rng if rng is not None else _random
            # ``uniform(1 - 0.1, 1 + 0.1)`` produces a multiplier in
            # ``[0.9, 1.1]``; the result is a float that we add to
            # ``now`` as seconds. Using ``random()`` directly (and
            # mapping to the band ourselves) keeps the Protocol
            # minimal — a plain callable returning a ``float`` works.
            jitter_factor = 1.0 + (source.random() * 2 - 1) * _JITTER_RATIO
            delay = capped * jitter_factor

        return reference + timedelta(seconds=delay)

    def should_retry(self, attempts: int) -> bool:
        """Return ``True`` while ``attempts`` is strictly below ``max_attempts``.

        The service layer calls this *after* ``mark_attempt`` has
        incremented ``attempts``; the typical call site is
        ``if policy.should_retry(job.attempts): requeue()``.
        """
        return attempts < self.max_attempts


__all__ = [
    "RetryPolicy",
]
