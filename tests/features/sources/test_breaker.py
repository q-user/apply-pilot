"""TDD tests for the source circuit-breaker slice (M7, issue #61).

A :class:`CircuitBreaker` is a small per-source state machine that
prevents a failing external job board (e.g. hh.ru returning 5xx) from
blocking the rest of the batch ingestion pipeline. The wrapper
:class:`BreakeredSourceAdapter` decorates an existing
:class:`~apply_pilot.features.sources.adapter.SourceAdapter` and consults
the breaker before forwarding every call.

State machine
-------------

::

    CLOSED ──(N failures)──> OPEN
       ▲                        │
       │                        │ reset_timeout elapsed
       │                        ▼
       └──(success)─── HALF_OPEN ──(failure)──> OPEN

* ``CLOSED`` — calls flow through. Consecutive failures are counted;
  reaching ``failure_threshold`` trips to ``OPEN``.
* ``OPEN`` — calls are rejected with
  :class:`SourceUnavailableError`. After ``reset_timeout_seconds`` the
  breaker transitions to ``HALF_OPEN`` and admits a single trial call.
* ``HALF_OPEN`` — a single trial call decides the next state. Success
  → ``CLOSED``; failure → ``OPEN`` (and the timer is reset).

The tests prefer DI / in-memory fakes — no ``Mock``. The clock is
injectable so the time-dependent transitions can be exercised
deterministically.
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import Callable
from typing import Any

import pytest

from apply_pilot.features.apply_worker.models import ApplyJob
from apply_pilot.features.apply_worker.runtime import ApplyResult
from apply_pilot.features.audit.models import AuditEventType
from apply_pilot.features.screening.models import ScreeningQuestion
from apply_pilot.features.sources.adapter import SourceAdapter, SourceQuery
from apply_pilot.features.sources.breaker import (
    BreakeredSourceAdapter,
    BreakerSettings,
    CircuitBreaker,
    CircuitState,
    InMemorySourceCircuitRegistry,
    SourceCircuitRegistry,
    SourceUnavailableError,
)
from apply_pilot.features.sources.models import Vacancy
from apply_pilot.features.sources.normalizer import VacancyNormalizer

# ---------------------------------------------------------------------------
# Fake clock
# ---------------------------------------------------------------------------


class _FakeClock:
    """A monotonic clock stand-in for tests.

    Tests advance the clock by calling :meth:`advance`. The default
    starting point is ``0.0`` so the math is obvious in assertions.
    """

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def _settings(
    *,
    failure_threshold: int = 3,
    reset_timeout_seconds: float = 10.0,
    half_open_max_calls: int = 1,
) -> BreakerSettings:
    """Return a :class:`BreakerSettings` with small tunables for fast tests."""
    return BreakerSettings(
        failure_threshold=failure_threshold,
        reset_timeout_seconds=reset_timeout_seconds,
        half_open_max_calls=half_open_max_calls,
    )


# ===========================================================================
# CircuitBreaker state machine
# ===========================================================================


class TestCircuitBreakerDefaults:
    def test_starts_closed(self) -> None:
        """A fresh breaker is in :attr:`CircuitState.CLOSED`."""
        breaker = CircuitBreaker(clock=_FakeClock())
        assert breaker.state is CircuitState.CLOSED

    def test_default_settings(self) -> None:
        """The default settings are documented in the dataclass docstring."""
        # Sanity: the dataclass accepts no args and returns sensible defaults.
        breaker = CircuitBreaker(clock=_FakeClock())
        assert breaker.failure_count == 0
        assert breaker.state is CircuitState.CLOSED

    def test_closed_allow_request_is_true(self) -> None:
        """A :attr:`CircuitState.CLOSED` breaker admits every call."""
        breaker = CircuitBreaker(clock=_FakeClock())
        assert breaker.allow_request() is True

    def test_record_success_in_closed_resets_counter(self) -> None:
        """A success in ``CLOSED`` zeroes the failure counter."""
        breaker = CircuitBreaker(clock=_FakeClock())
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.failure_count == 2

        breaker.record_success()

        assert breaker.failure_count == 0
        assert breaker.state is CircuitState.CLOSED


class TestCircuitBreakerTrips:
    def test_failures_increment_counter(self) -> None:
        """Each :meth:`record_failure` bumps the internal counter."""
        breaker = CircuitBreaker(clock=_FakeClock(), settings=_settings(failure_threshold=5))
        for i in range(1, 4):
            breaker.record_failure()
            assert breaker.failure_count == i

    def test_reaching_threshold_trips_to_open(self) -> None:
        """``failure_threshold`` consecutive failures trip the breaker."""
        breaker = CircuitBreaker(clock=_FakeClock(), settings=_settings(failure_threshold=3))
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.state is CircuitState.CLOSED

        breaker.record_failure()

        assert breaker.state is CircuitState.OPEN

    def test_threshold_one_trips_on_first_failure(self) -> None:
        """``failure_threshold=1`` trips on the very first failure."""
        breaker = CircuitBreaker(clock=_FakeClock(), settings=_settings(failure_threshold=1))
        breaker.record_failure()
        assert breaker.state is CircuitState.OPEN

    def test_success_after_partial_failures_resets_counter(self) -> None:
        """A success before threshold zeros the counter so the next run starts fresh."""
        breaker = CircuitBreaker(clock=_FakeClock(), settings=_settings(failure_threshold=3))
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_success()

        # Two more failures are not enough to trip — the success reset the counter.
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.state is CircuitState.CLOSED


class TestCircuitBreakerOpen:
    def test_open_rejects_requests(self) -> None:
        """Once open, :meth:`allow_request` returns ``False``."""
        breaker = CircuitBreaker(clock=_FakeClock(), settings=_settings(failure_threshold=2))
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.state is CircuitState.OPEN

        assert breaker.allow_request() is False

    def test_open_rejects_repeatedly_without_changing_state(self) -> None:
        """Repeated rejections in ``OPEN`` do not change the state."""
        breaker = CircuitBreaker(clock=_FakeClock(), settings=_settings(failure_threshold=1))
        breaker.record_failure()
        assert breaker.state is CircuitState.OPEN

        for _ in range(5):
            assert breaker.allow_request() is False
        assert breaker.state is CircuitState.OPEN

    def test_open_rejection_does_not_record_failure(self) -> None:
        """Rejections are a no-op on the counter — the breaker just refused the call."""
        clock = _FakeClock()
        breaker = CircuitBreaker(clock=clock, settings=_settings(failure_threshold=1))
        breaker.record_failure()
        opened_at = breaker.opened_at
        assert opened_at is not None

        breaker.allow_request()  # rejected; should not change anything

        assert breaker.failure_count == 1
        assert breaker.state is CircuitState.OPEN

    def test_record_failure_in_open_clamps_after_timeout_elapsed(self) -> None:
        """``OPEN`` failures clamp ``_opened_at`` to ``_opened_at + timeout`` (#143).

        Without the clamp, each failure refreshes ``_opened_at`` to
        ``clock()``, so a sustained outage that keeps producing failures
        pushes the half-open probe further into the future with no upper
        bound. The fix caps ``_opened_at`` at
        ``original + reset_timeout_seconds``, guaranteeing the half-open
        probe can never be pushed more than one reset window past the
        original trip — regardless of failure volume.
        """
        clock = _FakeClock()
        breaker = CircuitBreaker(
            clock=clock,
            settings=_settings(failure_threshold=1, reset_timeout_seconds=1.0),
        )
        breaker.record_failure()  # CLOSED → OPEN at t=0
        original_opened_at = breaker.opened_at
        assert original_opened_at is not None
        clamped_boundary = original_opened_at + 1.0

        # Simulate a sustained outage: 100 failures arrive back-to-back
        # after the original reset window has fully elapsed. Without the
        # clamp, every ``record_failure`` would push ``_opened_at``
        # further out — the half-open probe would never arrive. With the
        # clamp, ``_opened_at`` is pinned to the reset boundary
        # (``original + timeout``).
        clock.advance(2.0)  # well past the 1.0s reset window
        for _ in range(100):
            breaker.record_failure()

        # The clamp pins ``_opened_at`` to the original reset boundary
        # (``opened_at_original + reset_timeout_seconds``) regardless
        # of how many failures arrive. Without the clamp the final
        # ``_opened_at`` would be ``clock() == 2.0`` (or further), so
        # this assertion is the regression check.
        assert breaker.opened_at == pytest.approx(clamped_boundary)
        assert breaker.opened_at < clock()
        assert breaker.opened_at < original_opened_at + 2.0  # no drift past 2x timeout

        # The clamp is not a permanent hold: once the clamped boundary
        # has elapsed, the breaker transitions to HALF_OPEN on the next
        # state read.
        assert breaker.state is CircuitState.HALF_OPEN

    def test_open_transitions_to_half_open_after_clamped_window_without_requests(self) -> None:
        """After clamping in OPEN, if timeout elapsed, state should be HALF_OPEN
        without calling state/allow_request.

        This is a regression test for issue #208: when a failure occurs in OPEN state
        AFTER the timeout elapsed, _opened_at is clamped to original + reset_timeout,
        but the state doesn't transition to HALF_OPEN automatically. The transition
        only happens in allow_request() / state() - if no requests come in during
        the clamped window, the breaker stays OPEN indefinitely.
        """
        clock = _FakeClock()
        breaker = CircuitBreaker(
            clock=clock,
            settings=_settings(failure_threshold=1, reset_timeout_seconds=1.0),
        )
        breaker.record_failure()  # CLOSED -> OPEN at t=0
        original_opened_at = breaker.opened_at
        assert original_opened_at is not None

        # Advance clock well past the reset window
        clock.advance(2.0)  # well past the 1.0s reset window

        # Record a failure in OPEN state - this triggers clamping
        breaker.record_failure()

        # At this point, _opened_at should be clamped to original + timeout (1.0)
        # and clock() = 2.0, which is >= 1.0, so the breaker should be HALF_OPEN
        # But currently it stays OPEN because _refresh_state() is only called
        # in state() and allow_request()
        # This test SHOULD pass after the fix
        assert breaker.state is CircuitState.HALF_OPEN

    def test_open_should_transition_to_half_open_internally_after_clamped_timeout(self) -> None:
        """The breaker should transition to HALF_OPEN internally after clamped timeout,
        even if no external call reads state or calls allow_request.

        This test demonstrates the bug: after record_failure() clamps _opened_at,
        the internal state should transition to HALF_OPEN if the clamped boundary
        has elapsed. Currently it stays OPEN until someone calls state() or
        allow_request().
        """
        clock = _FakeClock()
        breaker = CircuitBreaker(
            clock=clock,
            settings=_settings(failure_threshold=1, reset_timeout_seconds=1.0),
        )
        breaker.record_failure()  # CLOSED -> OPEN at t=0
        original_opened_at = breaker.opened_at
        assert original_opened_at is not None

        # Advance clock well past the reset window
        clock.advance(2.0)  # well past the 1.0s reset window

        # Record a failure in OPEN state - this triggers clamping
        breaker.record_failure()

        # After clamping, _opened_at should be at original + timeout = 1.0
        # And clock() = 2.0, so elapsed = 1.0 >= reset_timeout_seconds = 1.0
        # The breaker SHOULD be in HALF_OPEN state internally
        # But we can't check it without calling state() which triggers _refresh_state()
        # So we need a different way to verify the internal state

        # Use _refresh_state directly to see what the internal state would be
        breaker._refresh_state()
        assert breaker._state is CircuitState.HALF_OPEN


class TestCircuitBreakerHalfOpen:
    def test_reset_timeout_transitions_to_half_open(self) -> None:
        """After ``reset_timeout_seconds`` the breaker admits a trial call."""
        clock = _FakeClock()
        breaker = CircuitBreaker(
            clock=clock, settings=_settings(failure_threshold=1, reset_timeout_seconds=10.0)
        )
        breaker.record_failure()
        assert breaker.state is CircuitState.OPEN

        clock.advance(9.999)
        assert breaker.allow_request() is False  # not yet

        clock.advance(0.002)  # total 10.001
        assert breaker.state is CircuitState.HALF_OPEN
        assert breaker.allow_request() is True

    def test_half_open_allows_exactly_one_call(self) -> None:
        """A single trial call is admitted; further calls in HALF_OPEN are rejected."""
        clock = _FakeClock()
        breaker = CircuitBreaker(
            clock=clock,
            settings=_settings(
                failure_threshold=1, reset_timeout_seconds=5.0, half_open_max_calls=1
            ),
        )
        breaker.record_failure()
        clock.advance(5.0)
        assert breaker.state is CircuitState.HALF_OPEN

        assert breaker.allow_request() is True  # trial call admitted
        # After the single trial admission, subsequent allow_request() returns False
        # until the call result is recorded and the breaker transitions.
        assert breaker.allow_request() is False

    def test_half_open_success_closes_breaker(self) -> None:
        """A success in ``HALF_OPEN`` transitions to ``CLOSED`` and resets counters."""
        clock = _FakeClock()
        breaker = CircuitBreaker(
            clock=clock,
            settings=_settings(failure_threshold=1, reset_timeout_seconds=5.0),
        )
        breaker.record_failure()
        clock.advance(5.0)
        assert breaker.state is CircuitState.HALF_OPEN

        breaker.record_success()

        assert breaker.state is CircuitState.CLOSED
        assert breaker.failure_count == 0
        assert breaker.opened_at is None

    def test_half_open_failure_reopens_breaker(self) -> None:
        """A failure in ``HALF_OPEN`` transitions back to ``OPEN`` and resets timer."""
        clock = _FakeClock()
        breaker = CircuitBreaker(
            clock=clock,
            settings=_settings(failure_threshold=1, reset_timeout_seconds=5.0),
        )
        breaker.record_failure()
        first_opened = breaker.opened_at
        clock.advance(5.0)
        assert breaker.state is CircuitState.HALF_OPEN

        breaker.record_failure()

        assert breaker.state is CircuitState.OPEN
        # The opened_at is reset so the next probe is another full reset_timeout away.
        assert breaker.opened_at is not None
        assert breaker.opened_at > first_opened  # type: ignore[operator]

    def test_half_open_reopened_resets_reset_window(self) -> None:
        """After HALF_OPEN→OPEN, a fresh ``reset_timeout`` window is enforced."""
        clock = _FakeClock()
        breaker = CircuitBreaker(
            clock=clock,
            settings=_settings(failure_threshold=1, reset_timeout_seconds=5.0),
        )
        breaker.record_failure()
        clock.advance(5.0)  # OPEN → HALF_OPEN
        assert breaker.state is CircuitState.HALF_OPEN

        breaker.record_failure()  # HALF_OPEN → OPEN again
        assert breaker.state is CircuitState.OPEN

        # A short advance is not enough — the window is fresh.
        clock.advance(4.999)
        assert breaker.allow_request() is False

        clock.advance(0.002)
        assert breaker.state is CircuitState.HALF_OPEN


# ===========================================================================
# InMemorySourceCircuitRegistry
# ===========================================================================


class TestInMemorySourceCircuitRegistry:
    def test_get_or_create_returns_same_instance(self) -> None:
        """Repeated lookups for the same source return the same breaker."""
        registry = InMemorySourceCircuitRegistry(settings=_settings())

        first = registry.get_or_create("hh")
        second = registry.get_or_create("hh")

        assert first is second

    def test_get_or_create_creates_new_for_unknown_name(self) -> None:
        """Unknown source names get a fresh breaker."""
        registry = InMemorySourceCircuitRegistry(settings=_settings())

        hh = registry.get_or_create("hh")
        habr = registry.get_or_create("habr")

        assert hh is not habr
        assert registry.list() == {"hh", "habr"}

    def test_get_returns_none_for_unknown(self) -> None:
        """An unknown source has no breaker — :meth:`get` returns ``None``."""
        registry = InMemorySourceCircuitRegistry()
        assert registry.get("nope") is None

    def test_get_returns_existing_breaker(self) -> None:
        """:meth:`get` returns the breaker created via :meth:`get_or_create`."""
        registry = InMemorySourceCircuitRegistry(settings=_settings())
        breaker = registry.get_or_create("hh")
        assert registry.get("hh") is breaker

    def test_reset_clears_all_breakers(self) -> None:
        """:meth:`reset` drops every breaker; lookups re-create from defaults."""
        registry = InMemorySourceCircuitRegistry(settings=_settings())
        registry.get_or_create("hh")
        registry.get_or_create("habr")

        registry.reset()

        assert registry.list() == set()
        assert registry.get("hh") is None
        # And a fresh lookup gives a clean breaker.
        fresh = registry.get_or_create("hh")
        assert fresh.state is CircuitState.CLOSED
        assert fresh.failure_count == 0

    def test_satisfies_protocol(self) -> None:
        """The in-memory implementation satisfies :class:`SourceCircuitRegistry`."""
        registry = InMemorySourceCircuitRegistry()
        assert isinstance(registry, SourceCircuitRegistry)


# ===========================================================================
# SourceUnavailableError
# ===========================================================================


class TestSourceUnavailableError:
    def test_carries_source_and_state(self) -> None:
        """The exception exposes ``source`` and ``state`` attributes for callers."""
        exc = SourceUnavailableError(source="hh", state=CircuitState.OPEN)
        assert exc.source == "hh"
        assert exc.state is CircuitState.OPEN

    def test_message_includes_source_and_state(self) -> None:
        """The default message names the source and state."""
        exc = SourceUnavailableError(source="habr", state=CircuitState.OPEN)
        message = str(exc)
        assert "habr" in message
        assert "open" in message.lower()

    def test_is_exception(self) -> None:
        """The class is a regular :class:`Exception` subclass."""
        assert issubclass(SourceUnavailableError, Exception)


# ===========================================================================
# BreakeredSourceAdapter — fakes
# ===========================================================================


@dataclasses.dataclass
class _RecordingAdapter:
    """A :class:`SourceAdapter`-shaped fake that records calls and is programmable.

    The fake satisfies the :class:`SourceAdapter` Protocol structurally
    (it has ``name``, ``search``, ``normalize``, ``extract_screening_questions``,
    ``apply``) so the wrapper can be type-checked as a Protocol. Each method
    is replaced by a test-supplied callable — when the callable is set to
    raise, the wrapper must observe the failure and the breaker must trip.
    """

    name: str
    search_fn: Callable[[SourceQuery], Any] = lambda q: []
    normalize_fn: Callable[[dict[str, Any]], Vacancy] = lambda r: Vacancy(
        source="stub", source_id="0", title="", description=None, url=None
    )
    extract_fn: Callable[[dict[str, Any]], list[ScreeningQuestion]] = lambda r: []
    apply_fn: Callable[[ApplyJob], Any] = lambda j: ApplyResult(
        success=True, external_application_id=None, error=None, retryable=False
    )

    async def search(self, query: SourceQuery) -> list[dict[str, Any]]:
        return self.search_fn(query)

    def normalize(self, raw: dict[str, Any]) -> Vacancy:
        return self.normalize_fn(raw)

    def extract_screening_questions(self, raw: dict[str, Any]) -> list[ScreeningQuestion]:
        return self.extract_fn(raw)

    async def apply(self, job: ApplyJob) -> ApplyResult:
        return self.apply_fn(job)


class _RecordingAuditService:
    """In-memory :class:`AuditService` substitute for tests.

    Captures every :meth:`log_event` call. The real ``AuditService`` is
    integration-tested elsewhere; here we only need to assert that the
    wrapper emits the right event types at the right transitions.
    """

    def __init__(self) -> None:
        self.events: list[tuple[AuditEventType, dict[str, Any] | None]] = []

    def log_event(
        self,
        event_type: AuditEventType,
        user_id: uuid.UUID | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        self.events.append((event_type, details))


def _wrap(
    inner: SourceAdapter,
    *,
    settings: BreakerSettings | None = None,
    clock: _FakeClock | None = None,
    audit: _RecordingAuditService | None = None,
) -> tuple[BreakeredSourceAdapter, _FakeClock]:
    """Build a :class:`BreakeredSourceAdapter` with a controllable clock."""
    clock = clock or _FakeClock()
    settings = settings or _settings()
    registry = InMemorySourceCircuitRegistry(settings=settings, clock=clock)
    wrapper = BreakeredSourceAdapter(
        inner=inner,
        registry=registry,
        clock=clock,
        audit_service=audit,  # type: ignore[arg-type]
    )
    return wrapper, clock


# ---------------------------------------------------------------------------
# BreakeredSourceAdapter — search()
# ---------------------------------------------------------------------------


class TestBreakeredSourceAdapterSearch:
    def test_returns_underlying_result_when_closed(self) -> None:
        """A success in ``CLOSED`` returns the inner adapter's result verbatim."""
        inner = _RecordingAdapter(name="hh", search_fn=lambda q: [{"id": "1"}, {"id": "2"}])
        wrapper, _ = _wrap(inner)

        result = asyncio.run(wrapper.search(SourceQuery(text="python")))

        assert result == [{"id": "1"}, {"id": "2"}]

    def test_records_success(self) -> None:
        """A success in ``CLOSED`` resets the failure counter (smoke check)."""
        # The breaker starts at 0, so the easier check is: even after one
        # prior failure below threshold, a success keeps the breaker CLOSED.
        inner = _RecordingAdapter(name="hh", search_fn=lambda q: [])
        wrapper, _ = _wrap(inner, settings=_settings(failure_threshold=3))
        registry = wrapper._registry  # type: ignore[attr-defined]
        breaker = registry.get_or_create("hh")
        breaker.record_failure()
        breaker.record_failure()

        asyncio.run(wrapper.search(SourceQuery()))

        assert breaker.state is CircuitState.CLOSED
        assert breaker.failure_count == 0

    def test_records_failure_on_exception(self) -> None:
        """A raised exception increments the failure counter and is re-raised."""

        def boom(_q: SourceQuery) -> list[dict[str, Any]]:
            raise RuntimeError("hh 5xx")

        inner = _RecordingAdapter(name="hh", search_fn=boom)
        wrapper, _ = _wrap(inner, settings=_settings(failure_threshold=2))
        registry = wrapper._registry  # type: ignore[attr-defined]
        breaker = registry.get_or_create("hh")

        with pytest.raises(RuntimeError, match="hh 5xx"):
            asyncio.run(wrapper.search(SourceQuery()))
        assert breaker.failure_count == 1

        with pytest.raises(RuntimeError, match="hh 5xx"):
            asyncio.run(wrapper.search(SourceQuery()))
        assert breaker.state is CircuitState.OPEN

    def test_rejects_when_breaker_open(self) -> None:
        """When the breaker is ``OPEN``, ``search`` raises :class:`SourceUnavailableError`."""
        inner = _RecordingAdapter(name="hh")
        wrapper, _ = _wrap(inner, settings=_settings(failure_threshold=1))
        registry = wrapper._registry  # type: ignore[attr-defined]
        breaker = registry.get_or_create("hh")
        breaker.record_failure()
        assert breaker.state is CircuitState.OPEN

        with pytest.raises(SourceUnavailableError) as excinfo:
            asyncio.run(wrapper.search(SourceQuery()))

        assert excinfo.value.source == "hh"
        assert excinfo.value.state is CircuitState.OPEN

    def test_open_rejection_does_not_call_inner(self) -> None:
        """The inner adapter is *not* invoked while the breaker is ``OPEN``."""
        calls: list[SourceQuery] = []

        def tracker(q: SourceQuery) -> list[dict[str, Any]]:
            calls.append(q)
            return []

        inner = _RecordingAdapter(name="hh", search_fn=tracker)
        wrapper, _ = _wrap(inner, settings=_settings(failure_threshold=1))
        registry = wrapper._registry  # type: ignore[attr-defined]
        registry.get_or_create("hh").record_failure()
        assert wrapper._registry.get("hh").state is CircuitState.OPEN  # type: ignore[attr-defined]

        for _ in range(3):
            with pytest.raises(SourceUnavailableError):
                asyncio.run(wrapper.search(SourceQuery()))

        assert calls == []  # inner was never called

    def test_recovers_after_timeout(self) -> None:
        """After ``reset_timeout_seconds`` the trial call in ``HALF_OPEN`` closes the breaker."""
        clock = _FakeClock()
        inner = _RecordingAdapter(name="hh", search_fn=lambda q: [{"id": "ok"}])
        wrapper, _ = _wrap(
            inner, settings=_settings(failure_threshold=1, reset_timeout_seconds=5.0), clock=clock
        )
        registry = wrapper._registry  # type: ignore[attr-defined]
        breaker = registry.get_or_create("hh")
        breaker.record_failure()
        assert breaker.state is CircuitState.OPEN

        clock.advance(5.0)

        result = asyncio.run(wrapper.search(SourceQuery()))

        assert result == [{"id": "ok"}]
        assert breaker.state is CircuitState.CLOSED


# ---------------------------------------------------------------------------
# BreakeredSourceAdapter — normalize() / extract_screening_questions()
# ---------------------------------------------------------------------------


class TestBreakeredSourceAdapterNormalize:
    def test_returns_underlying_vacancy_when_closed(self) -> None:
        """A success in ``CLOSED`` returns the inner adapter's :class:`Vacancy`."""
        vacancy = Vacancy(
            source="hh",
            source_id="1",
            title="Engineer",
            description=None,
            url=None,
        )
        inner = _RecordingAdapter(name="hh", normalize_fn=lambda r: vacancy)
        wrapper, _ = _wrap(inner)

        assert wrapper.normalize({"id": "1"}) is vacancy

    def test_rejects_when_breaker_open(self) -> None:
        """``normalize`` raises :class:`SourceUnavailableError` in ``OPEN``."""
        inner = _RecordingAdapter(name="hh")
        wrapper, _ = _wrap(inner, settings=_settings(failure_threshold=1))
        registry = wrapper._registry  # type: ignore[attr-defined]
        registry.get_or_create("hh").record_failure()

        with pytest.raises(SourceUnavailableError) as excinfo:
            wrapper.normalize({"id": "1"})

        assert excinfo.value.source == "hh"
        assert excinfo.value.state is CircuitState.OPEN

    def test_records_failure_when_normalize_raises(self) -> None:
        """A normalise exception trips the breaker after enough failures."""

        def boom(_r: dict[str, Any]) -> Vacancy:
            raise ValueError("bad payload")

        inner = _RecordingAdapter(name="hh", normalize_fn=boom)
        wrapper, _ = _wrap(inner, settings=_settings(failure_threshold=2))
        registry = wrapper._registry  # type: ignore[attr-defined]
        breaker = registry.get_or_create("hh")

        with pytest.raises(ValueError):
            wrapper.normalize({"id": "1"})
        with pytest.raises(ValueError):
            wrapper.normalize({"id": "1"})

        assert breaker.state is CircuitState.OPEN


class TestBreakeredSourceAdapterScreening:
    def test_returns_underlying_questions_when_closed(self) -> None:
        """A success in ``CLOSED`` returns the inner adapter's screening list."""
        question = ScreeningQuestion(
            vacancy_id=uuid.uuid4(),
            question_text="Why?",
            question_order=0,
        )
        inner = _RecordingAdapter(name="hh", extract_fn=lambda r: [question])
        wrapper, _ = _wrap(inner)

        assert wrapper.extract_screening_questions({"id": "1"}) == [question]

    def test_rejects_when_breaker_open(self) -> None:
        """``extract_screening_questions`` raises :class:`SourceUnavailableError` in ``OPEN``."""
        inner = _RecordingAdapter(name="hh")
        wrapper, _ = _wrap(inner, settings=_settings(failure_threshold=1))
        registry = wrapper._registry  # type: ignore[attr-defined]
        registry.get_or_create("hh").record_failure()

        with pytest.raises(SourceUnavailableError):
            wrapper.extract_screening_questions({"id": "1"})


# ---------------------------------------------------------------------------
# BreakeredSourceAdapter — apply()
# ---------------------------------------------------------------------------


class TestBreakeredSourceAdapterApply:
    def test_returns_underlying_result_when_closed(self) -> None:
        """A success in ``CLOSED`` returns the inner adapter's :class:`ApplyResult`."""
        result = ApplyResult(
            success=True,
            external_application_id="neg-1",
            error=None,
            retryable=False,
        )
        inner = _RecordingAdapter(name="hh", apply_fn=lambda j: result)
        wrapper, _ = _wrap(inner)

        job = ApplyJob(
            match_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            vacancy_id=uuid.uuid4(),
        )

        assert asyncio.run(wrapper.apply(job)) is result

    def test_rejects_when_breaker_open(self) -> None:
        """``apply`` raises :class:`SourceUnavailableError` in ``OPEN``."""
        inner = _RecordingAdapter(name="hh")
        wrapper, _ = _wrap(inner, settings=_settings(failure_threshold=1))
        registry = wrapper._registry  # type: ignore[attr-defined]
        registry.get_or_create("hh").record_failure()

        job = ApplyJob(
            match_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            vacancy_id=uuid.uuid4(),
        )

        with pytest.raises(SourceUnavailableError):
            asyncio.run(wrapper.apply(job))

    def test_records_failure_when_apply_raises(self) -> None:
        """An apply failure trips the breaker after enough failures."""
        calls: list[ApplyJob] = []

        def boom(j: ApplyJob) -> ApplyResult:
            calls.append(j)
            raise RuntimeError("hh negotiation 5xx")

        inner = _RecordingAdapter(name="hh", apply_fn=boom)
        wrapper, _ = _wrap(inner, settings=_settings(failure_threshold=2))
        registry = wrapper._registry  # type: ignore[attr-defined]
        breaker = registry.get_or_create("hh")

        for _ in range(2):
            with pytest.raises(RuntimeError):
                asyncio.run(
                    wrapper.apply(
                        ApplyJob(
                            match_id=uuid.uuid4(),
                            user_id=uuid.uuid4(),
                            vacancy_id=uuid.uuid4(),
                        )
                    )
                )

        assert breaker.state is CircuitState.OPEN
        assert len(calls) == 2  # inner called twice before the breaker tripped


# ---------------------------------------------------------------------------
# BreakeredSourceAdapter — audit events
# ---------------------------------------------------------------------------


class TestBreakeredSourceAdapterAudit:
    def test_emits_source_degraded_when_breaker_trips(self) -> None:
        """The wrapper emits ``SOURCE_DEGRADED`` on the CLOSED→OPEN transition."""

        def boom(_q: SourceQuery) -> list[dict[str, Any]]:
            raise RuntimeError("hh 5xx")

        inner = _RecordingAdapter(name="hh", search_fn=boom)
        audit = _RecordingAuditService()
        wrapper, _ = _wrap(
            inner,
            settings=_settings(failure_threshold=2),
            audit=audit,
        )

        with pytest.raises(RuntimeError):
            asyncio.run(wrapper.search(SourceQuery()))
        # First failure: no audit event yet (still CLOSED).
        assert audit.events == []

        with pytest.raises(RuntimeError):
            asyncio.run(wrapper.search(SourceQuery()))

        # Second failure: breaker is now OPEN — exactly one SOURCE_DEGRADED event.
        assert [e for e, _ in audit.events] == [AuditEventType.SOURCE_DEGRADED]
        degraded_details = audit.events[0][1]
        assert degraded_details is not None
        assert degraded_details["source"] == "hh"
        assert degraded_details["state"] == CircuitState.OPEN.value

    def test_does_not_emit_degraded_repeatedly(self) -> None:
        """Once OPEN, repeated rejections do not emit more degraded events."""

        def boom(_q: SourceQuery) -> list[dict[str, Any]]:
            raise RuntimeError("hh 5xx")

        inner = _RecordingAdapter(name="hh", search_fn=boom)
        audit = _RecordingAuditService()
        wrapper, _ = _wrap(
            inner,
            settings=_settings(failure_threshold=1),
            audit=audit,
        )
        with pytest.raises(RuntimeError):
            asyncio.run(wrapper.search(SourceQuery()))  # trips

        # Subsequent rejections — breaker is already OPEN.
        for _ in range(3):
            with pytest.raises(SourceUnavailableError):
                asyncio.run(wrapper.search(SourceQuery()))

        assert [e for e, _ in audit.events] == [AuditEventType.SOURCE_DEGRADED]

    def test_emits_source_recovered_on_half_open_success(self) -> None:
        """A successful trial call in ``HALF_OPEN`` emits ``SOURCE_RECOVERED``."""

        def boom(_q: SourceQuery) -> list[dict[str, Any]]:
            raise RuntimeError("hh 5xx")

        clock = _FakeClock()
        inner = _RecordingAdapter(name="hh", search_fn=boom)
        audit = _RecordingAuditService()
        wrapper, _ = _wrap(
            inner,
            settings=_settings(failure_threshold=1, reset_timeout_seconds=5.0),
            clock=clock,
            audit=audit,
        )
        # Trip through the wrapper so the SOURCE_DEGRADED event is emitted.
        with pytest.raises(RuntimeError):
            asyncio.run(wrapper.search(SourceQuery()))
        assert [e for e, _ in audit.events] == [AuditEventType.SOURCE_DEGRADED]

        # Heal the inner adapter, advance the clock past the reset window.
        inner.search_fn = lambda q: [{"id": "ok"}]
        clock.advance(5.0)
        result = asyncio.run(wrapper.search(SourceQuery()))

        assert result == [{"id": "ok"}]
        assert [e for e, _ in audit.events] == [
            AuditEventType.SOURCE_DEGRADED,
            AuditEventType.SOURCE_RECOVERED,
        ]
        recovered_details = audit.events[1][1]
        assert recovered_details is not None
        assert recovered_details["source"] == "hh"
        assert recovered_details["state"] == CircuitState.CLOSED.value

    def test_no_audit_service_does_not_crash(self) -> None:
        """The wrapper works without an audit service — events are not emitted."""

        def boom(_q: SourceQuery) -> list[dict[str, Any]]:
            raise RuntimeError("hh 5xx")

        inner = _RecordingAdapter(name="hh", search_fn=boom)
        wrapper, _ = _wrap(inner, settings=_settings(failure_threshold=1))

        with pytest.raises(RuntimeError):
            asyncio.run(wrapper.search(SourceQuery()))
        with pytest.raises(SourceUnavailableError):
            asyncio.run(wrapper.search(SourceQuery()))

        # No crash; the state is still correct.
        registry = wrapper._registry  # type: ignore[attr-defined]
        assert registry.get("hh").state is CircuitState.OPEN


# ---------------------------------------------------------------------------
# BreakeredSourceAdapter — Protocol conformance + name forwarding
# ---------------------------------------------------------------------------


class TestBreakeredSourceAdapterProtocol:
    def test_satisfies_source_adapter_protocol(self) -> None:
        """The wrapper is a structural :class:`SourceAdapter`."""
        inner = _RecordingAdapter(name="hh")
        wrapper, _ = _wrap(inner)
        # The Protocol is ``runtime_checkable``; structural conformance is enough.
        assert isinstance(wrapper, SourceAdapter)

    def test_name_is_forwarded_from_inner(self) -> None:
        """``wrapper.name`` mirrors ``inner.name``."""
        inner = _RecordingAdapter(name="habr-careers")
        wrapper, _ = _wrap(inner)
        assert wrapper.name == "habr-careers"

    def test_breakers_are_independent_per_source(self) -> None:
        """Two wrappers around different sources have independent breakers."""
        hh = _RecordingAdapter(name="hh", search_fn=lambda q: [])
        habr = _RecordingAdapter(name="habr", search_fn=lambda q: [])
        clock = _FakeClock()
        registry = InMemorySourceCircuitRegistry(
            settings=_settings(failure_threshold=1), clock=clock
        )

        hh_wrapper = BreakeredSourceAdapter(
            inner=hh, registry=registry, clock=clock, audit_service=None
        )
        habr_wrapper = BreakeredSourceAdapter(
            inner=habr, registry=registry, clock=clock, audit_service=None
        )

        def boom(_q: SourceQuery) -> list[dict[str, Any]]:
            raise RuntimeError("hh down")

        hh.search_fn = boom
        with pytest.raises(RuntimeError):
            asyncio.run(hh_wrapper.search(SourceQuery()))

        # hh is OPEN...
        assert registry.get("hh").state is CircuitState.OPEN
        # ...but habr is still CLOSED. Touch the habr wrapper to force
        # the lazy breaker creation before asserting state.
        asyncio.run(habr_wrapper.search(SourceQuery()))
        assert registry.get("habr").state is CircuitState.CLOSED

        # And habr's calls still go through.
        result = asyncio.run(habr_wrapper.search(SourceQuery()))
        assert result == []


# ---------------------------------------------------------------------------
# BreakerSettings
# ---------------------------------------------------------------------------


class TestBreakerSettings:
    def test_default_settings_are_sane(self) -> None:
        """The default tunables match the documented defaults."""
        s = BreakerSettings()
        assert s.failure_threshold > 0
        assert s.reset_timeout_seconds > 0
        assert s.half_open_max_calls >= 1

    def test_settings_are_frozen(self) -> None:
        """``BreakerSettings`` is immutable (frozen dataclass)."""
        s = BreakerSettings()
        with pytest.raises((AttributeError, dataclasses.FrozenInstanceError)):
            s.failure_threshold = 1  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Cross-check: the wrapper composes with the real :class:`VacancyNormalizer`
# ---------------------------------------------------------------------------


class TestBreakeredSourceAdapterWithRealNormalizer:
    def test_normalize_runs_inner_when_closed(self) -> None:
        """The wrapper transparently forwards to the inner adapter."""
        normalizer = VacancyNormalizer()
        raw = {
            "id": "v-1",
            "name": "Backend Engineer",
            "area": {"name": "Moscow"},
            "salary": None,
            "employer": {"name": "Acme"},
            "schedule": None,
            "experience": None,
            "key_skills": [],
            "published_at": "2025-01-01T00:00:00+0000",
        }
        inner = _RecordingAdapter(name="hh", normalize_fn=lambda r: normalizer.normalize("hh", r))
        wrapper, _ = _wrap(inner)

        vacancy = wrapper.normalize(raw)

        assert vacancy.source == "hh"
        assert vacancy.source_id == "v-1"
        assert vacancy.title == "Backend Engineer"
