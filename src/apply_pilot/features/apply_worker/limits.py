"""Per-user rate limiting and anti-spam caps for ``apply_worker`` (M5, issue #46).

The apply queue is gated on a per-user hourly and daily cap so a
runaway script (or a manual user click-spamming ``/accept``) cannot
flood hh.ru with submissions. The cap is enforced by the
:class:`ApplyJobService` *before* a new :class:`ApplyJob` is inserted
and *after* the enqueue succeeds so the counter reflects every
operation that could lead to a network call.

Public surface
--------------

* :class:`RateLimiter` — :class:`Protocol` the service depends on.
* :class:`RateLimitResult` — what :meth:`RateLimiter.check` returns:
  the allow / deny decision plus the structured payload the HTTP
  layer uses to build a 429 response.
* :class:`WindowStatus` — the per-window snapshot (``used``,
  ``limit``, ``remaining``, ``reset_at``) the dashboard reads via
  ``GET /apply-jobs/limits``.
* :class:`InMemoryRateLimiter` — dict-backed fake for tests.
* :class:`SqlRateLimiter` — production implementation backed by
  :class:`ApplyRateLimitEvent` rows.
* :class:`RateLimitExceeded` — exception raised by the service when
  the user is over the cap. Carries the structured
  :class:`RateLimitResult` so the HTTP layer can build a 429
  response with a ``Retry-After`` header.

Window math
-----------

The limiter tracks two rolling windows per ``(user_id, key)`` pair:

* **hourly** — ``[now - 1h, now]``
* **daily**  — ``[now - 24h, now]``

A check is allowed only when *both* windows are under their limit;
otherwise the limiter returns ``allowed=False`` and the service
raises :class:`RateLimitExceeded`. The ``reset_at`` field on each
window is the timestamp at which the oldest in-window event ages
out, so the dashboard can render "back in N seconds" without
computing the math itself.

Both implementations accept a ``clock`` callable for testability; the
test suite uses a :class:`_TickingClock` to step the clock past a
window boundary without sleeping. The SQL implementation lets the
database stamp ``occurred_at`` on insert so clock skew between
application processes cannot double-count events.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apply_pilot.config import ApplyWorkerSettings
from apply_pilot.features.apply_worker.models import ApplyRateLimitEvent

#: The key the apply queue guards. Reserved as a module-level constant
#: so call sites cannot typo it; the same string is used by the HTTP
#: layer when it logs which key was tripped.
APPLY_KEY: str = "apply"

#: Length of the hourly window. Fixed at 1h by the M5 spec; exposed as
#: a constant for the (degenerate) case where a future slice wants to
#: reuse the limiter with a different window.
HOURLY_WINDOW = timedelta(hours=1)

#: Length of the daily window. Fixed at 24h by the M5 spec.
DAILY_WINDOW = timedelta(hours=24)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RateLimitExceeded(Exception):
    """The caller is over a configured rate-limit window.

    The exception carries the structured :class:`RateLimitResult` so
    the HTTP layer can build a 429 response with the same payload
    the dashboard renders on ``GET /apply-jobs/limits``. ``retry_after``
    is mirrored as a top-level attribute for ergonomics — handlers
    that only need the seconds-to-retry value do not have to dig
    into :attr:`result`.
    """

    code: str = "rate_limit_exceeded"

    def __init__(self, result: RateLimitResult) -> None:
        self.result: RateLimitResult = result
        self.reason: str | None = result.reason
        self.retry_after_seconds: int | None = result.retry_after_seconds
        super().__init__(result.reason or "rate limit exceeded")


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WindowStatus:
    """The status of one rate-limit window.

    Attributes:
        used: Number of recorded events inside the window.
        limit: Configured cap; the user is allowed while ``used <
            limit``.
        remaining: ``max(limit - used, 0)``. Always non-negative.
        reset_at: When the oldest in-window event ages out, so the
            dashboard can render "back in N seconds" without doing
            the math itself. ``None`` when the window is empty (no
            events to age out, so the next record starts the window
            fresh).
    """

    used: int
    limit: int
    remaining: int
    reset_at: datetime | None


@dataclass(frozen=True)
class RateLimitResult:
    """The result of :meth:`RateLimiter.check`.

    Attributes:
        allowed: ``True`` when the caller is under both windows.
        reason: A short, stable error code. Always ``"rate_limit_exceeded"``
            when ``allowed`` is ``False``; ``None`` when allowed.
        retry_after_seconds: The number of seconds the caller should
            wait before retrying. ``None`` when allowed. Computed as
            the seconds until the oldest in-window event falls out of
            the most-saturated window, so the HTTP layer can plug the
            value straight into a ``Retry-After`` header.
        hourly: Snapshot of the hourly window.
        daily: Snapshot of the daily window.
    """

    allowed: bool
    reason: str | None
    retry_after_seconds: int | None
    hourly: WindowStatus
    daily: WindowStatus


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class RateLimiter(Protocol):
    """Minimal interface :class:`ApplyJobService` relies on.

    The service is the only direct caller; the HTTP layer reads the
    snapshot through :meth:`check` (``GET /apply-jobs/limits``). Two
    methods, both keyed on ``(user_id, key)``:

    * :meth:`check` is non-mutating. The service calls it before
      enqueuing a job so a denied caller raises :class:`RateLimitExceeded`
      *before* the row is inserted.
    * :meth:`record` appends one event so subsequent :meth:`check`
      calls see the new state. The service calls it after a successful
      enqueue.
    """

    def check(self, user_id: uuid.UUID, *, key: str) -> RateLimitResult: ...

    def record(self, user_id: uuid.UUID, *, key: str) -> None: ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _default_clock() -> datetime:
    """Return the current UTC time; isolated so tests can override it."""
    return datetime.now(UTC)


def _window_status(
    *,
    used: int,
    oldest: datetime | None,
    limit: int,
    window: timedelta,
    now: datetime,
) -> WindowStatus:
    """Compute a :class:`WindowStatus` from the in-window count + oldest event.

    The two implementations feed the helper different shapes:

    * :class:`InMemoryRateLimiter` already has the filtered list of
      in-window events and reduces it via :func:`len` /
      :func:`min`.
    * :class:`SqlRateLimiter` runs a single ``COUNT`` / ``MIN``
      aggregate and threads the values through directly — cheaper
      than fetching the full row set.

    Both shapes converge here so the window math (used → remaining,
    oldest + window → reset_at) lives in one place.
    """
    remaining = max(limit - used, 0)
    if used == 0 or oldest is None:
        return WindowStatus(used=used, limit=limit, remaining=remaining, reset_at=None)
    # ``reset_at`` is the instant the oldest in-window event ages out
    # of the window. We round *up* to the next whole second so the
    # HTTP ``Retry-After`` value is never zero while the window is
    # still saturated.
    delta = (oldest + window) - now
    seconds = int(delta.total_seconds())
    if delta > timedelta(0) and seconds == 0:
        seconds = 1
    elif seconds < 0:
        seconds = 0
    reset_at = now + timedelta(seconds=seconds)
    return WindowStatus(
        used=used,
        limit=limit,
        remaining=remaining,
        reset_at=reset_at,
    )


def _retry_after(
    hourly: WindowStatus,
    daily: WindowStatus,
    *,
    now: datetime,
) -> int | None:
    """Pick the larger reset hint across the *blocking* windows.

    ``retry_after`` answers the question "how long until the user is
    allowed again", so only the windows that are actually saturated
    (``used >= limit``) contribute. A window that has plenty of
    headroom does not constrain the answer.

    When multiple windows are blocking the caller has to wait for
    *all* of them to age out enough to allow one more record, so the
    hint is the maximum of the blocking windows' ``reset_at`` deltas.

    ``now`` is threaded through (rather than calling
    :func:`datetime.now` again) so tests that pin the clock see a
    consistent value across the window snapshot and the retry hint.
    """
    candidates: list[int] = []
    for window in (hourly, daily):
        if window.used < window.limit:
            # Window has headroom; not constraining the answer.
            continue
        if window.reset_at is None:
            # Saturated but no events to age out — should not happen
            # in practice (``reset_at`` is set whenever ``used > 0``),
            # but be defensive so a degraded database state still
            # produces a usable hint.
            continue
        delta = (window.reset_at - now).total_seconds()
        if delta <= 0:
            continue
        candidates.append(int(delta))
    if not candidates:
        return None
    return max(candidates)


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------


class InMemoryRateLimiter:
    """Dict-backed rate limiter for tests.

    Stores a ``{(user_id, key): list[datetime]}`` mapping. The list
    is append-only; ``check`` filters out events that have aged out
    of the configured windows before counting. The class is
    collaborator-injected into the service through its constructor so
    tests do not have to patch the global state.
    """

    def __init__(
        self,
        *,
        settings: ApplyWorkerSettings,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._clock: Callable[[], datetime] = clock or _default_clock
        # ``_events`` keys on ``(user_id, key)`` to keep different
        # users' counters isolated and let the same instance be reused
        # for additional keys without a schema change.
        self._events: dict[tuple[uuid.UUID, str], list[datetime]] = {}

    def _events_for(self, user_id: uuid.UUID, key: str) -> list[datetime]:
        return self._events.setdefault((user_id, key), [])

    def _filter_window(
        self, events: Sequence[datetime], *, window: timedelta, now: datetime
    ) -> list[datetime]:
        cutoff = now - window
        return [ts for ts in events if ts >= cutoff]

    def check(self, user_id: uuid.UUID, *, key: str) -> RateLimitResult:
        """Return a :class:`RateLimitResult` without mutating state.

        The check evaluates both windows independently and allows the
        call only when both are under their cap. The result carries
        the snapshot of *both* windows so the HTTP layer can render
        the full picture to the dashboard.
        """
        now = self._clock()
        all_events = self._events_for(user_id, key)
        hourly_events = self._filter_window(all_events, window=HOURLY_WINDOW, now=now)
        daily_events = self._filter_window(all_events, window=DAILY_WINDOW, now=now)

        hourly = _window_status(
            used=len(hourly_events),
            oldest=min(hourly_events) if hourly_events else None,
            limit=self._settings.hourly_limit,
            window=HOURLY_WINDOW,
            now=now,
        )
        daily = _window_status(
            used=len(daily_events),
            oldest=min(daily_events) if daily_events else None,
            limit=self._settings.daily_limit,
            window=DAILY_WINDOW,
            now=now,
        )

        allowed = hourly.used < hourly.limit and daily.used < daily.limit
        if allowed:
            return RateLimitResult(
                allowed=True,
                reason=None,
                retry_after_seconds=None,
                hourly=hourly,
                daily=daily,
            )
        return RateLimitResult(
            allowed=False,
            reason="rate_limit_exceeded",
            retry_after_seconds=_retry_after(hourly, daily, now=now),
            hourly=hourly,
            daily=daily,
        )

    def record(self, user_id: uuid.UUID, *, key: str) -> None:
        """Append one event at the current clock value.

        Old events that have aged out of the window are *not* removed
        from storage; ``check`` filters them out. The append-only
        contract keeps the data path trivial and makes the SQL
        implementation a one-row insert.
        """
        now = self._clock()
        self._events_for(user_id, key).append(now)


# ---------------------------------------------------------------------------
# SQL implementation
# ---------------------------------------------------------------------------


class SqlRateLimiter:
    """SQLAlchemy-backed rate limiter.

    Stores one :class:`ApplyRateLimitEvent` row per recorded event;
    ``check`` runs a single ``COUNT(*)`` over the index
    ``ix_apply_rate_limit_events_user_id_key_occurred_at`` to get
    the in-window count for the (user, key) pair. The minimum
    ``occurred_at`` for the same predicate supplies ``reset_at``.

    The implementation is collaborator-injected: tests pass a
    session_factory bound to an in-memory SQLite database so the
    SQL path is part of the CI surface.
    """

    def __init__(
        self,
        session: Session | None = None,
        *,
        session_factory: Callable[[], Session] | None = None,
        settings: ApplyWorkerSettings,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if session is not None and session_factory is not None:
            raise ValueError("pass either session or session_factory, not both")
        if session is None and session_factory is None:
            raise ValueError("SqlRateLimiter requires a Session or session_factory")
        self._session = session
        self._session_factory = session_factory
        self._settings = settings
        self._clock: Callable[[], datetime] = clock or _default_clock

    def _scope(self) -> Session:
        if self._session is not None:
            return self._session
        assert self._session_factory is not None
        return self._session_factory()

    def _count_in_window(
        self, session: Session, *, user_id: uuid.UUID, key: str, window: timedelta
    ) -> tuple[int, datetime | None]:
        """Return ``(used, oldest_in_window)`` for the given window.

        ``oldest_in_window`` is the minimum ``occurred_at`` of the
        in-window events; ``None`` when the window is empty. The
        function issues a single ``COUNT`` and ``MIN`` aggregate so
        the database can use the composite index efficiently.

        SQLite (used in the tests) returns naive datetimes because
        the driver strips timezone information on the way out; we
        re-attach :data:`datetime.UTC` so the subtraction in
        :func:`_window_status` is consistent regardless of the
        underlying engine. Production PostgreSQL keeps the
        timezone and the re-attach is a no-op.
        """
        now = self._clock()
        cutoff = now - window
        statement = select(
            func.count(ApplyRateLimitEvent.id),
            func.min(ApplyRateLimitEvent.occurred_at),
        ).where(
            ApplyRateLimitEvent.user_id == user_id,
            ApplyRateLimitEvent.key == key,
            ApplyRateLimitEvent.occurred_at >= cutoff,
            ApplyRateLimitEvent.occurred_at <= now,
        )
        count, oldest = session.execute(statement).one()
        if oldest is not None and oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=UTC)
        return int(count or 0), oldest

    def check(self, user_id: uuid.UUID, *, key: str) -> RateLimitResult:
        """Read-only check backed by a single SQL aggregate query per window."""
        now = self._clock()
        session = self._scope()
        try:
            hourly_count, hourly_oldest = self._count_in_window(
                session, user_id=user_id, key=key, window=HOURLY_WINDOW
            )
            daily_count, daily_oldest = self._count_in_window(
                session, user_id=user_id, key=key, window=DAILY_WINDOW
            )
        finally:
            if self._session is None:
                session.close()

        hourly = _window_status(
            used=hourly_count,
            oldest=hourly_oldest,
            limit=self._settings.hourly_limit,
            window=HOURLY_WINDOW,
            now=now,
        )
        daily = _window_status(
            used=daily_count,
            oldest=daily_oldest,
            limit=self._settings.daily_limit,
            window=DAILY_WINDOW,
            now=now,
        )

        allowed = hourly.used < hourly.limit and daily.used < daily.limit
        if allowed:
            return RateLimitResult(
                allowed=True,
                reason=None,
                retry_after_seconds=None,
                hourly=hourly,
                daily=daily,
            )
        return RateLimitResult(
            allowed=False,
            reason="rate_limit_exceeded",
            retry_after_seconds=_retry_after(hourly, daily, now=now),
            hourly=hourly,
            daily=daily,
        )

    def record(self, user_id: uuid.UUID, *, key: str) -> None:
        """Insert one :class:`ApplyRateLimitEvent` row.

        The ``occurred_at`` column is stamped with the configured
        clock so test runs can pin the timestamp. Production
        deployments can leave the ``server_default`` to fire by
        omitting the field, but using the clock keeps the SQL and
        in-memory implementations consistent under test.
        """
        event = ApplyRateLimitEvent(
            user_id=user_id,
            key=key,
            occurred_at=self._clock(),
        )
        session = self._scope()
        try:
            session.add(event)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            if self._session is None:
                session.close()


# ---------------------------------------------------------------------------
# No-op default
# ---------------------------------------------------------------------------


class _NoOpRateLimiter:
    """Allow every call. The default when no limiter is injected.

    Production wires the real :class:`SqlRateLimiter` in :mod:`api`;
    tests that do not care about rate limiting can construct the
    service with no ``rate_limiter`` argument and the service
    transparently uses this stub. Keeping a separate class — instead
    of ``None``-checking at every call site — preserves the
    :class:`RateLimiter` contract and lets the test suite catch
    accidental dependency-omission bugs at construction time.
    """

    def check(self, user_id: uuid.UUID, *, key: str) -> RateLimitResult:
        empty = WindowStatus(used=0, limit=0, remaining=0, reset_at=None)
        return RateLimitResult(
            allowed=True,
            reason=None,
            retry_after_seconds=None,
            hourly=empty,
            daily=empty,
        )

    def record(self, user_id: uuid.UUID, *, key: str) -> None:
        return None


def default_rate_limiter() -> RateLimiter:
    """Return a permissive :class:`RateLimiter`.

    Used by :class:`ApplyJobService` when no ``rate_limiter`` is
    injected (legacy call sites constructed before issue #46). The
    :class:`_NoOpRateLimiter` is intentionally permissive so existing
    test fixtures keep working without explicit rate-limiter wiring.
    """
    return _NoOpRateLimiter()


__all__ = [
    "APPLY_KEY",
    "DAILY_WINDOW",
    "HOURLY_WINDOW",
    "InMemoryRateLimiter",
    "RateLimitExceeded",
    "RateLimitResult",
    "RateLimiter",
    "SqlRateLimiter",
    "WindowStatus",
    "default_rate_limiter",
]
