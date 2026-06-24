"""Per-source circuit breaker (M7, issue #61).

A single external job board returning 5xx (hh.ru is the canonical case)
must not stall the rest of the batch ingestion pipeline. This slice
introduces a small :class:`CircuitBreaker` state machine that gates
every :class:`~apply_pilot.features.sources.adapter.SourceAdapter` call:

::

    CLOSED ──(N failures)──> OPEN
       ▲                        │
       │                        │ reset_timeout elapsed
       │                        ▼
       └──(success)─── HALF_OPEN ──(failure)──> OPEN

* :attr:`CircuitState.CLOSED` — calls flow through. Consecutive
  failures are counted; reaching :attr:`BreakerSettings.failure_threshold`
  trips the breaker to :attr:`CircuitState.OPEN`.
* :attr:`CircuitState.OPEN` — calls are rejected with
  :class:`SourceUnavailableError`. After
  :attr:`BreakerSettings.reset_timeout_seconds` the breaker
  transitions to :attr:`CircuitState.HALF_OPEN` and admits a single
  trial call.
* :attr:`CircuitState.HALF_OPEN` — the single trial call decides the
  next state. A success closes the breaker; a failure reopens it (and
  the timer is reset for another :attr:`BreakerSettings.reset_timeout_seconds`).

The breaker is a cross-cutting concern that lives in :mod:`sources`
to avoid adding a new top-level slice. The
:class:`BreakeredSourceAdapter` wrapper decorates an existing
:class:`SourceAdapter` and consults the registry-keyed breaker
before forwarding every call. The wrapper is a structural
:class:`SourceAdapter` itself (it implements the Protocol), so it
sits in the same
:class:`~apply_pilot.features.sources.adapter.AdapterRegistry` slot as
the inner adapter.

Audit events
------------

State transitions emit structured events on the optional
:class:`~apply_pilot.features.audit.service.AuditService` collaborator:

* ``AuditEventType.SOURCE_DEGRADED`` — emitted exactly once per
  :attr:`CircuitState.CLOSED` → :attr:`CircuitState.OPEN` transition
  (and on the subsequent
  :attr:`CircuitState.HALF_OPEN` → :attr:`CircuitState.OPEN`
  re-opens).
* ``AuditEventType.SOURCE_RECOVERED`` — emitted exactly once per
  :attr:`CircuitState.HALF_OPEN` → :attr:`CircuitState.CLOSED`
  transition.

The events carry ``source`` (the adapter :attr:`name`) and
``state`` (:class:`CircuitState` value) so operators can correlate
alerts with concrete integrations. The events are independent of
the breaker itself — the breaker only knows about success/failure
counts; the wrapper observes state deltas and emits the audit row.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from apply_pilot.features.audit.models import AuditEventType
from apply_pilot.features.screening.models import ScreeningQuestion
from apply_pilot.features.sources.adapter import SourceAdapter, SourceQuery
from apply_pilot.features.sources.models import Vacancy

if TYPE_CHECKING:
    # ``ApplyResult`` lives in :mod:`apply_pilot.features.apply_worker.runtime`,
    # which transitively imports :mod:`apply_pilot.features.sources` (via the
    # matches/telegram/apply_worker dependency chain). Importing it under
    # :data:`typing.TYPE_CHECKING` keeps the import graph acyclic at runtime —
    # the Protocol below only references ``ApplyResult`` in a type annotation
    # and the wrapper only uses it as a return-type marker. Tests that need
    # the real class import it directly.
    from apply_pilot.features.apply_worker.models import ApplyJob
    from apply_pilot.features.apply_worker.runtime import ApplyResult

_LOG_PREFIX = "apply_pilot.features.sources.breaker."

#: Default clock — :func:`time.monotonic`. Tests inject a fake to drive
#: time-dependent transitions deterministically.
_DefaultClock: Callable[[], float] = time.monotonic


# ---------------------------------------------------------------------------
# State enum
# ---------------------------------------------------------------------------


class CircuitState(StrEnum):
    """The three breaker states.

    Values are stable strings — they are part of the audit-event
    ``state`` payload and may be persisted in observability sinks.
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BreakerSettings:
    """Tunables for a :class:`CircuitBreaker`.

    Attributes
    ----------
    failure_threshold:
        Number of consecutive failures that trips the breaker from
        :attr:`CircuitState.CLOSED` to :attr:`CircuitState.OPEN`. A
        success in :attr:`CircuitState.CLOSED` resets the counter.
    reset_timeout_seconds:
        Wall time (in seconds) the breaker stays in
        :attr:`CircuitState.OPEN` before admitting a trial call to
        :attr:`CircuitState.HALF_OPEN`. Measured with
        :func:`time.monotonic`, so DST / NTP skew cannot perturb it.
    half_open_max_calls:
        Number of trial calls admitted in :attr:`CircuitState.HALF_OPEN`
        before the breaker transitions. The current implementation
        supports ``1``; the field is reserved for a future fan-out
        that probes the source with parallel trial calls.
    """

    failure_threshold: int = 5
    reset_timeout_seconds: float = 60.0
    half_open_max_calls: int = 1


# ---------------------------------------------------------------------------
# SourceUnavailableError
# ---------------------------------------------------------------------------


class SourceUnavailableError(Exception):
    """Raised when a :class:`SourceAdapter` is rejected by the breaker.

    The exception carries the offending source name and the breaker
    state that caused the rejection so the caller can log/branch on
    them. A :class:`BreakeredSourceAdapter` raises this from every
    :class:`SourceAdapter` method when the breaker is in
    :attr:`CircuitState.OPEN`.
    """

    def __init__(self, *, source: str, state: CircuitState) -> None:
        self.source = source
        self.state = state
        super().__init__(f"Source {source!r} is {state.value}; calls are temporarily rejected")


# ---------------------------------------------------------------------------
# CircuitBreaker
# ---------------------------------------------------------------------------


class CircuitBreaker:
    """A small per-source circuit breaker state machine.

    The breaker is intentionally lock-free: it is driven by calls that
    arrive from a single asyncio event loop (the application process)
    and is not shared across processes. A future worker process can
    swap in a Redis-backed implementation; the in-memory version is
    the one wired into the production wiring.

    Callers drive the state machine through three methods:

    * :meth:`allow_request` — consulted *before* the wrapped call.
    * :meth:`record_success` — called when the wrapped call succeeds.
    * :meth:`record_failure` — called when the wrapped call raises.

    Time is read from an injectable ``clock`` (default
    :func:`time.monotonic`) so tests can advance the clock
    deterministically.
    """

    def __init__(
        self,
        *,
        settings: BreakerSettings | None = None,
        clock: Callable[[], float] = _DefaultClock,
    ) -> None:
        self._settings = settings or BreakerSettings()
        self._clock = clock
        self._state: CircuitState = CircuitState.CLOSED
        self._failure_count: int = 0
        #: Monotonic time the breaker last entered :attr:`CircuitState.OPEN`.
        #: ``None`` when the breaker is not :attr:`CircuitState.OPEN`.
        self._opened_at: float | None = None
        #: Monotonic time the breaker *originally* entered
        #: :attr:`CircuitState.OPEN` for the current open window. Stays
        #: fixed across ``_opened_at`` refreshes (issue #143) so the
        #: half-open probe can never be pushed further out than one
        #: :attr:`BreakerSettings.reset_timeout_seconds` after the
        #: original trip. ``None`` when the breaker is not
        #: :attr:`CircuitState.OPEN`.
        self._opened_at_original: float | None = None
        #: Number of trial calls admitted in the current
        #: :attr:`CircuitState.HALF_OPEN` window. Compared against
        #: :attr:`BreakerSettings.half_open_max_calls`.
        self._half_open_in_flight: int = 0
        self._logger = logging.getLogger(f"{_LOG_PREFIX}CircuitBreaker")

    # ------------------------------------------------------------------
    # Read-only state
    # ------------------------------------------------------------------

    @property
    def state(self) -> CircuitState:
        """Return the current :class:`CircuitState` (after lazy time-based transitions)."""
        self._refresh_state()
        return self._state

    @property
    def failure_count(self) -> int:
        """Return the number of consecutive failures since the last success."""
        return self._failure_count

    @property
    def opened_at(self) -> float | None:
        """Return the monotonic time the breaker last entered ``OPEN``, or ``None``."""
        return self._opened_at

    @property
    def settings(self) -> BreakerSettings:
        """Return the :class:`BreakerSettings` this breaker was built with."""
        return self._settings

    @property
    def clock(self) -> Callable[[], float]:
        """Return the clock the breaker reads for time-based transitions."""
        return self._clock

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------

    def allow_request(self) -> bool:
        """Return whether a call to the wrapped source should be admitted.

        The :attr:`CircuitState.OPEN` → :attr:`CircuitState.HALF_OPEN`
        transition is applied lazily here (driven by the clock), so
        a caller that has been polling does not need to know about
        the timeout itself — once the timeout elapses, the next
        :meth:`allow_request` call returns ``True`` and admits a
        single trial call.
        """
        self._refresh_state()
        if self._state is CircuitState.CLOSED:
            return True
        if self._state is CircuitState.OPEN:
            return False
        # HALF_OPEN: admit up to half_open_max_calls trial calls.
        if self._half_open_in_flight < self._settings.half_open_max_calls:
            self._half_open_in_flight += 1
            return True
        return False

    def record_success(self) -> None:
        """Mark the latest call as successful.

        In :attr:`CircuitState.CLOSED` this resets the failure
        counter. In :attr:`CircuitState.HALF_OPEN` this transitions
        back to :attr:`CircuitState.CLOSED`.
        """
        self._refresh_state()
        if self._state is CircuitState.HALF_OPEN:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._opened_at = None
            self._opened_at_original = None
            self._half_open_in_flight = 0
            return
        # CLOSED — reset the counter so a fresh run of failures
        # starts from zero.
        self._failure_count = 0

    def record_failure(self) -> None:
        """Mark the latest call as failed.

        In :attr:`CircuitState.CLOSED` this increments the failure
        counter; reaching :attr:`BreakerSettings.failure_threshold`
        trips the breaker to :attr:`CircuitState.OPEN`.

        In :attr:`CircuitState.HALF_OPEN` a failure immediately
        re-opens the breaker (and resets the ``reset_timeout``
        window so the next probe has to wait a full timeout again).

        In :attr:`CircuitState.OPEN` a failure refreshes the reset
        timer, but only up to ``reset_timeout_seconds`` past the
        *original* trip — clamping prevents a sustained outage from
        pushing the half-open probe arbitrarily far into the future
        (issue #143).
        """
        # Note: we intentionally do *not* call ``_refresh_state`` here.
        # A failure reported after the timeout has elapsed should be
        # treated as an OPEN failure (clamping ``_opened_at`` back to
        # the original reset boundary) rather than as a HALF_OPEN probe
        # failure (which would re-open with ``_opened_at = clock()`` and
        # restart the timer from scratch). The OPEN → HALF_OPEN
        # transition is driven by :meth:`allow_request` and
        # :meth:`state` instead, which is sufficient for callers that
        # gate every request on the breaker.
        if self._state is CircuitState.OPEN:
            # Clamp ``_opened_at`` to ``original + reset_timeout``
            # so a sustained outage cannot push the half-open probe
            # further into the future with no upper bound
            # (issue #143).
            assert self._opened_at_original is not None
            self._opened_at = min(
                self._clock(),
                self._opened_at_original + self._settings.reset_timeout_seconds,
            )
            # Do NOT transition to HALF_OPEN here. The transition is
            # handled by ``_refresh_state`` (called via ``state`` and
            # ``allow_request``). Transitioning here would cause
            # subsequent failures in the same outage to re-open the
            # breaker with a fresh timestamp (issue #208).
            return
        if self._state is CircuitState.HALF_OPEN:
            self._open(now=self._clock())
            return
        # CLOSED
        self._failure_count += 1
        if self._failure_count >= self._settings.failure_threshold:
            self._open(now=self._clock())

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _refresh_state(self) -> None:
        """Apply the time-based OPEN → HALF_OPEN transition if the timeout elapsed."""
        if self._state is not CircuitState.OPEN:
            return
        if self._opened_at is None:
            return
        elapsed = self._clock() - self._opened_at
        if elapsed >= self._settings.reset_timeout_seconds:
            self._state = CircuitState.HALF_OPEN
            self._half_open_in_flight = 0
            self._logger.info(
                "circuit_breaker.half_open",
                extra={
                    "event": "circuit_breaker.half_open",
                    "elapsed_seconds": elapsed,
                    "reset_timeout_seconds": self._settings.reset_timeout_seconds,
                },
            )

    def _open(self, *, now: float) -> None:
        """Transition to :attr:`CircuitState.OPEN` and stamp the open time."""
        self._state = CircuitState.OPEN
        self._opened_at = now
        self._opened_at_original = now
        self._failure_count = self._settings.failure_threshold
        self._half_open_in_flight = 0


# ---------------------------------------------------------------------------
# SourceCircuitRegistry
# ---------------------------------------------------------------------------


@runtime_checkable
class SourceCircuitRegistry(Protocol):
    """Per-source :class:`CircuitBreaker` registry.

    The cross-source orchestration code (the future
    :class:`BreakeredSourceAdapter` wiring in :mod:`apply_pilot.app`,
    any admin health view) talks to the breaker through this
    Protocol. The in-memory implementation
    (:class:`InMemorySourceCircuitRegistry`) is the default; tests
    substitute a fake as long as it implements the same surface.
    """

    def get_or_create(self, source_name: str) -> CircuitBreaker:
        """Return the breaker for ``source_name``, creating it on first lookup."""
        ...

    def get(self, source_name: str) -> CircuitBreaker | None:
        """Return the breaker for ``source_name`` or ``None`` when none exists."""
        ...

    def list(self) -> set[str]:
        """Return the set of source names that have a registered breaker."""
        ...

    def reset(self) -> None:
        """Drop every breaker — useful for tests and admin ``force-reset`` actions."""
        ...


class InMemorySourceCircuitRegistry:
    """Dict-backed :class:`SourceCircuitRegistry` for dev and tests.

    The registry keeps one :class:`CircuitBreaker` per source name. New
    breakers are created lazily on the first :meth:`get_or_create` call
    using the registry's default :class:`BreakerSettings` and clock.
    Settings are snapshotted at creation time, so changing the
    registry's defaults later does not perturb existing breakers — a
    deliberate choice that keeps observability predictable.

    The class is intentionally not thread-safe — the worker and the
    API both run in the same asyncio event loop, and dict mutations
    happen between ``await`` points only.
    """

    __slots__ = ("_breakers", "_clock", "_settings")

    def __init__(
        self,
        *,
        settings: BreakerSettings | None = None,
        clock: Callable[[], float] = _DefaultClock,
    ) -> None:
        self._settings: BreakerSettings = settings or BreakerSettings()
        self._clock: Callable[[], float] = clock
        self._breakers: dict[str, CircuitBreaker] = {}

    def get_or_create(self, source_name: str) -> CircuitBreaker:
        """Return the breaker for ``source_name``; create one if it does not exist."""
        breaker = self._breakers.get(source_name)
        if breaker is None:
            breaker = CircuitBreaker(settings=self._settings, clock=self._clock)
            self._breakers[source_name] = breaker
        return breaker

    def get(self, source_name: str) -> CircuitBreaker | None:
        """Return the breaker for ``source_name`` or ``None`` when none exists."""
        return self._breakers.get(source_name)

    def list(self) -> set[str]:
        """Return the set of source names that have a registered breaker."""
        return set(self._breakers.keys())

    def reset(self) -> None:
        """Drop every breaker; lookups will recreate them on demand."""
        self._breakers.clear()


# ---------------------------------------------------------------------------
# BreakeredSourceAdapter
# ---------------------------------------------------------------------------


class BreakeredSourceAdapter:
    """Decorator that gates a :class:`SourceAdapter` with a :class:`CircuitBreaker`.

    The wrapper satisfies the :class:`SourceAdapter` Protocol
    structurally (it carries :attr:`name` and the four lifecycle
    methods), so it slots into the existing
    :class:`~apply_pilot.features.sources.adapter.AdapterRegistry` in
    place of the inner adapter. Callers observe the same surface; the
    only behavioural difference is that:

    * Every call consults the breaker first; in
      :attr:`CircuitState.OPEN` the wrapper raises
      :class:`SourceUnavailableError` *without* touching the inner
      adapter.
    * Successes and failures are recorded on the breaker, so a
      sustained outage trips the breaker to
      :attr:`CircuitState.OPEN` after
      :attr:`BreakerSettings.failure_threshold` consecutive
      failures.
    * State transitions emit audit events on the optional
      ``audit_service`` collaborator.

    The wrapper is intentionally a thin decorator — it does not
    swallow the inner adapter's exceptions, transform its results, or
    do any retries of its own. Retries are the
    :class:`~apply_pilot.features.apply_worker.retry.ApplyRetryPolicy`'s
    job; circuit-breaking is the wrapper's job; the two stay
    composable.
    """

    def __init__(
        self,
        *,
        inner: SourceAdapter,
        registry: SourceCircuitRegistry,
        clock: Callable[[], float] = _DefaultClock,
        audit_service: Any | None = None,
    ) -> None:
        self._inner = inner
        self._registry = registry
        self._clock = clock
        # ``audit_service`` is typed as :class:`Any` to keep the
        # breaker slice free of a runtime import of the audit
        # service module. The protocol it implements is
        # ``log_event(event_type, user_id=None, details=None)`` —
        # the only method we call.
        self._audit_service = audit_service
        self._logger = logging.getLogger(f"{_LOG_PREFIX}BreakeredSourceAdapter")

    @property
    def name(self) -> str:
        """Return the inner adapter's :attr:`name`."""
        return self._inner.name

    @property
    def inner(self) -> SourceAdapter:
        """Return the wrapped :class:`SourceAdapter` (read-only escape hatch)."""
        return self._inner

    @property
    def registry(self) -> SourceCircuitRegistry:
        """Return the :class:`SourceCircuitRegistry` this wrapper consults."""
        return self._registry

    # ------------------------------------------------------------------
    # SourceAdapter surface
    # ------------------------------------------------------------------

    async def search(self, query: SourceQuery) -> list[dict[str, Any]]:
        """Forward :meth:`SourceAdapter.search` through the breaker.

        Raises:
            SourceUnavailableError: when the breaker is in
                :attr:`CircuitState.OPEN`.
        """
        return await self._call("search", lambda: self._inner.search(query))  # type: ignore[return-value]

    def normalize(self, raw: dict[str, Any]) -> Vacancy:
        """Forward :meth:`SourceAdapter.normalize` through the breaker.

        Raises:
            SourceUnavailableError: when the breaker is in
                :attr:`CircuitState.OPEN`.
        """
        return self._call_sync("normalize", lambda: self._inner.normalize(raw))

    def extract_screening_questions(self, raw: dict[str, Any]) -> list[ScreeningQuestion]:
        """Forward :meth:`SourceAdapter.extract_screening_questions` through the breaker.

        Raises:
            SourceUnavailableError: when the breaker is in
                :attr:`CircuitState.OPEN`.
        """
        return self._call_sync(
            "extract_screening_questions",
            lambda: self._inner.extract_screening_questions(raw),
        )

    async def apply(self, job: ApplyJob) -> ApplyResult:
        """Forward :meth:`SourceAdapter.apply` through the breaker.

        Raises:
            SourceUnavailableError: when the breaker is in
                :attr:`CircuitState.OPEN`.
        """
        return await self._call("apply", lambda: self._inner.apply(job))  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Call helpers — keep the audit / state observation logic in one place
    # ------------------------------------------------------------------

    async def _call(
        self,
        operation: str,
        runner: Callable[[], Any],
    ) -> Any:
        """Run an awaitable ``runner`` through the breaker.

        ``runner`` is a thunk that returns a coroutine; the helper
        awaits the coroutine and routes the result / exception
        through :meth:`_record_outcome`.
        """
        breaker = self._registry.get_or_create(self._inner.name)
        if not breaker.allow_request():
            self._on_rejected(breaker, operation)
            raise SourceUnavailableError(source=self._inner.name, state=breaker.state)

        try:
            result = await runner()
        except SourceUnavailableError:
            # An inner adapter that itself raised SourceUnavailableError
            # is treated as a breaker-level rejection, not a failure.
            # This keeps the breaker from self-reinforcing on a stuck
            # state.
            self._on_rejected(breaker, operation)
            raise
        except BaseException as exc:
            self._on_failure(breaker, operation, exc)
            raise
        else:
            self._on_success(breaker, operation)
            return result

    def _call_sync(
        self,
        operation: str,
        runner: Callable[[], Any],
    ) -> Any:
        """Run a synchronous ``runner`` through the breaker."""
        breaker = self._registry.get_or_create(self._inner.name)
        if not breaker.allow_request():
            self._on_rejected(breaker, operation)
            raise SourceUnavailableError(source=self._inner.name, state=breaker.state)

        try:
            result = runner()
        except SourceUnavailableError:
            self._on_rejected(breaker, operation)
            raise
        except BaseException as exc:
            self._on_failure(breaker, operation, exc)
            raise
        else:
            self._on_success(breaker, operation)
            return result

    # ------------------------------------------------------------------
    # State-delta observation
    # ------------------------------------------------------------------

    def _on_success(self, breaker: CircuitBreaker, operation: str) -> None:
        """Observe a successful call: record the success and check for recovery."""
        previous_state = breaker.state
        breaker.record_success()
        if previous_state is CircuitState.HALF_OPEN and breaker.state is CircuitState.CLOSED:
            self._emit_recovered(operation)

    def _on_failure(
        self,
        breaker: CircuitBreaker,
        operation: str,
        exc: BaseException,
    ) -> None:
        """Observe a failed call: record the failure and check for degradation."""
        previous_state = breaker.state
        breaker.record_failure()
        if previous_state is not CircuitState.OPEN and breaker.state is CircuitState.OPEN:
            self._emit_degraded(operation, exc)
        self._logger.warning(
            "circuit_breaker.call_failed",
            extra={
                "event": "circuit_breaker.call_failed",
                "source": self._inner.name,
                "operation": operation,
                "state": breaker.state.value,
                "exception_type": type(exc).__name__,
            },
        )

    def _on_rejected(self, breaker: CircuitBreaker, operation: str) -> None:
        """Observe a call rejected by the breaker (no inner invocation)."""
        self._logger.info(
            "circuit_breaker.call_rejected",
            extra={
                "event": "circuit_breaker.call_rejected",
                "source": self._inner.name,
                "operation": operation,
                "state": breaker.state.value,
            },
        )

    def _emit_degraded(self, operation: str, exc: BaseException | None = None) -> None:
        """Emit a ``SOURCE_DEGRADED`` audit event and a structured log line."""
        details: dict[str, object] = {
            "source": self._inner.name,
            "state": CircuitState.OPEN.value,
            "operation": operation,
        }
        if exc is not None:
            details["exception_type"] = type(exc).__name__
        self._logger.warning(
            "circuit_breaker.source_degraded",
            extra={"event": "circuit_breaker.source_degraded", **details},
        )
        self._log_audit(AuditEventType.SOURCE_DEGRADED, details)

    def _emit_recovered(self, operation: str) -> None:
        """Emit a ``SOURCE_RECOVERED`` audit event and a structured log line."""
        details: dict[str, object] = {
            "source": self._inner.name,
            "state": CircuitState.CLOSED.value,
            "operation": operation,
        }
        self._logger.info(
            "circuit_breaker.source_recovered",
            extra={"event": "circuit_breaker.source_recovered", **details},
        )
        self._log_audit(AuditEventType.SOURCE_RECOVERED, details)

    def _log_audit(self, event_type: AuditEventType, details: dict[str, object]) -> None:
        """Forward an event to the injected audit service, swallowing audit errors.

        The wrapper never fails a call because of an audit error: a
        misbehaving audit sink must not turn a recoverable source
        outage into a permanent one.
        """
        if self._audit_service is None:
            return
        try:
            self._audit_service.log_event(
                event_type=event_type,
                user_id=None,
                details=details,
            )
        except Exception:
            self._logger.exception(
                "circuit_breaker.audit_event_failed",
                extra={
                    "event": "circuit_breaker.audit_event_failed",
                    "audit_event": event_type.value,
                    "source": self._inner.name,
                },
            )


__all__ = [
    "BreakerSettings",
    "BreakeredSourceAdapter",
    "CircuitBreaker",
    "CircuitState",
    "InMemorySourceCircuitRegistry",
    "SourceCircuitRegistry",
    "SourceUnavailableError",
]
