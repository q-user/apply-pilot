"""Persistence gateway for the ``apply_worker`` slice (M5, issue #43).

Three implementations live here, mirroring the convention used by the
``cover_letter`` and ``matches`` slices:

* :class:`ApplyJobRepository` — Protocol defining the contract the
  service layer depends on.
* :class:`InMemoryApplyJobRepository` — dict-backed fake for tests.
* :class:`SqlApplyJobRepository` — production implementation backed by
  a SQLAlchemy ``Session``.

Contract
--------

The :class:`~job_apply.features.apply_worker.models.ApplyJob` row is the
queue entry the apply worker drains. The repository is the only
component that mutates the storage layer; the service layer is
responsible for ownership, status-transition rules, and the
``enqueue_for_match`` idempotency check.

``claim_next`` is the only concurrent path. The implementation must
atomically transition a row from ``queued`` to ``running`` and
increment ``attempts`` so two workers that race the same query do not
both claim the same job. SQLite locks the whole database so the
implementation can use a plain ``SELECT … UPDATE``; PostgreSQL
production deployments use ``SELECT ... FOR UPDATE SKIP LOCKED`` to
let multiple workers drain the queue in parallel.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from job_apply.features.apply_worker.models import (
    ApplyJob,
    ApplyJobStatus,
    ApplyStatusHistory,
)

# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ApplyJobRepository(Protocol):
    """Minimal interface the :class:`ApplyJobService` relies on.

    Read methods take user / match / job ids as plain UUIDs. Write
    methods accept fully-constructed ORM rows (for ``create``) or take
    the job id and explicit fields to mutate (for ``update_status``
    and ``mark_attempt``). The service is the only place that decides
    which fields to populate.
    """

    def create(self, job: ApplyJob) -> ApplyJob: ...
    def get_by_id(self, job_id: uuid.UUID) -> ApplyJob | None: ...
    def get_by_match(self, match_id: uuid.UUID) -> ApplyJob | None: ...
    def list_by_user(
        self,
        user_id: uuid.UUID,
        *,
        limit: int = 50,
    ) -> Sequence[ApplyJob]: ...
    def list_pending(self, *, limit: int = 50) -> Sequence[ApplyJob]: ...
    def claim_next(self) -> ApplyJob | None: ...
    def update_status(
        self,
        job_id: uuid.UUID,
        status: str,
        *,
        external_application_id: str | None = None,
        next_run_at: datetime | None = None,
        finished_at: datetime | None = None,
    ) -> ApplyJob: ...
    def mark_attempt(self, job_id: uuid.UUID, error: str) -> ApplyJob: ...


# ---------------------------------------------------------------------------
# History Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ApplyStatusHistoryRepository(Protocol):
    """Append-only gateway for :class:`ApplyStatusHistory` rows (M5, #49).

    The slice's contract is that history is written but never mutated
    or deleted through this protocol; the only writer is :meth:`create`
    and the readers are :meth:`list_by_job` (per-job timeline) and
    :meth:`list_by_user` (M6, #54 combined dashboard view across all of
    a user's apply jobs). All implementations mirror this contract: no
    update or delete methods are exposed.
    """

    def create(self, row: ApplyStatusHistory) -> ApplyStatusHistory: ...
    def list_by_job(self, job_id: uuid.UUID) -> Sequence[ApplyStatusHistory]: ...
    def list_by_user(
        self,
        user_id: uuid.UUID,
        *,
        job_id: uuid.UUID | None = None,
        to_status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[Sequence[ApplyStatusHistory], int]: ...


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------


class InMemoryApplyJobRepository:
    """Dict-backed repository for tests.

    Stores rows in a single ``_by_id`` dict plus a ``_by_match`` and
    ``_by_user`` index so the read methods can resolve lookups without
    a full scan. The secondary indices are populated by ``create`` and
    kept in sync with the primary table; the SQL implementation lets
    the database do the equivalent joins.
    """

    def __init__(self) -> None:
        self._by_id: dict[uuid.UUID, ApplyJob] = {}
        self._by_match: dict[uuid.UUID, uuid.UUID] = {}
        # ``_user_index`` maps each user id to the list of job ids
        # they own. Insertion order is preserved so ``list_by_user``
        # can deterministically order ties by insertion time (the SQL
        # implementation breaks ties on ``created_at`` / ``id`` desc).
        self._user_index: dict[uuid.UUID, list[uuid.UUID]] = {}

    def _user_list(self, user_id: uuid.UUID) -> list[uuid.UUID]:
        return self._user_index.setdefault(user_id, [])

    # -- writers ---------------------------------------------------------

    def create(self, job: ApplyJob) -> ApplyJob:
        if job.id is None:
            job.id = uuid.uuid4()
        # The Python-level ``__init__`` on the model already fills
        # ``status``, ``attempts``, and ``idempotency_key``, but the
        # in-memory repository mirrors what the SQL insert would
        # produce for a row that arrived through a different path
        # (e.g. an old test that constructed the ORM without the new
        # defaults).
        if job.status is None:
            job.status = ApplyJobStatus.QUEUED.value
        if job.attempts is None:
            job.attempts = 0
        if job.idempotency_key is None:
            from job_apply.features.apply_worker.models import compute_idempotency_key

            job.idempotency_key = compute_idempotency_key(job.user_id, job.vacancy_id, job.match_id)
        if job.created_at is None:
            job.created_at = datetime.now(UTC)
        self._by_id[job.id] = job
        self._by_match[job.match_id] = job.id
        self._user_list(job.user_id).append(job.id)
        return job

    def update_status(
        self,
        job_id: uuid.UUID,
        status: str,
        *,
        external_application_id: str | None = None,
        next_run_at: datetime | None = None,
        finished_at: datetime | None = None,
    ) -> ApplyJob:
        existing = self._by_id.get(job_id)
        if existing is None:
            raise KeyError(f"apply job {job_id} not found")
        existing.status = status
        if external_application_id is not None:
            existing.external_application_id = external_application_id
        if next_run_at is not None:
            existing.next_run_at = next_run_at
        if finished_at is not None:
            existing.finished_at = finished_at
        existing.updated_at = datetime.now(UTC)
        return existing

    def mark_attempt(self, job_id: uuid.UUID, error: str) -> ApplyJob:
        existing = self._by_id.get(job_id)
        if existing is None:
            raise KeyError(f"apply job {job_id} not found")
        existing.attempts = (existing.attempts or 0) + 1
        existing.last_error = error
        existing.updated_at = datetime.now(UTC)
        return existing

    # -- readers ---------------------------------------------------------

    def get_by_id(self, job_id: uuid.UUID) -> ApplyJob | None:
        return self._by_id.get(job_id)

    def get_by_match(self, match_id: uuid.UUID) -> ApplyJob | None:
        job_id = self._by_match.get(match_id)
        return self._by_id.get(job_id) if job_id is not None else None

    def list_by_user(
        self,
        user_id: uuid.UUID,
        *,
        limit: int = 50,
    ) -> Sequence[ApplyJob]:
        ids = self._user_list(user_id)
        rows = [self._by_id[i] for i in ids if i in self._by_id]
        rows.sort(key=lambda r: (r.created_at, r.id), reverse=True)
        return rows[:limit]

    def list_pending(self, *, limit: int = 50) -> Sequence[ApplyJob]:
        now = datetime.now(UTC)
        out: list[ApplyJob] = []
        for job in self._by_id.values():
            if job.status != ApplyJobStatus.QUEUED.value:
                continue
            if job.next_run_at is not None and job.next_run_at > now:
                continue
            out.append(job)
        out.sort(key=lambda r: (r.created_at, r.id))
        return out[:limit]

    def claim_next(self) -> ApplyJob | None:
        """Return the oldest claimable row, transition to ``running``.

        The in-memory implementation is not concurrent-safe; tests
        exercise the ``claim_next`` contract under the assumption that
        a single thread of control calls the service. The SQL
        implementation does the equivalent with a single transaction
        and the dialect-appropriate locking hint.
        """
        pending = list(self.list_pending(limit=1))
        if not pending:
            return None
        target = pending[0]
        target.status = ApplyJobStatus.RUNNING.value
        target.attempts = (target.attempts or 0) + 1
        target.started_at = datetime.now(UTC)
        target.updated_at = target.started_at
        return target


# ---------------------------------------------------------------------------
# In-memory history implementation
# ---------------------------------------------------------------------------


class InMemoryApplyStatusHistoryRepository:
    """List-backed fake for :class:`ApplyStatusHistory`.

    The repository keeps a single ``_rows`` list and a ``_by_job`` index
    so ``list_by_job`` can resolve the chronological slice for a job
    without scanning every row. Insertion order is preserved so two
    rows written within the same clock tick still return in the order
    the service appended them.

    The optional ``get_job_user_id`` callable powers the M6 #54
    cross-job ``list_by_user`` query: the in-memory implementation has
    no foreign-key metadata on history rows, so it asks the caller to
    resolve the owning user for a given job id. When the callable is
    not wired (legacy test fixtures that pre-date #54) ``list_by_user``
    returns an empty page so a misconfigured test fails loudly instead
    of leaking rows from every user.
    """

    def __init__(
        self,
        *,
        get_job_user_id: Callable[[uuid.UUID], uuid.UUID | None] | None = None,
    ) -> None:
        self._rows: list[ApplyStatusHistory] = []
        self._by_job: dict[uuid.UUID, list[ApplyStatusHistory]] = {}
        self._get_job_user_id = get_job_user_id

    def create(self, row: ApplyStatusHistory) -> ApplyStatusHistory:
        if row.id is None:
            row.id = uuid.uuid4()
        if row.created_at is None:
            row.created_at = datetime.now(UTC)
        self._rows.append(row)
        self._by_job.setdefault(row.job_id, []).append(row)
        return row

    def list_by_job(self, job_id: uuid.UUID) -> Sequence[ApplyStatusHistory]:
        """Return the rows for ``job_id`` in chronological order."""
        rows = list(self._by_job.get(job_id, ()))
        rows.sort(key=lambda r: (r.created_at, r.id))
        return rows

    def list_by_user(
        self,
        user_id: uuid.UUID,
        *,
        job_id: uuid.UUID | None = None,
        to_status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[Sequence[ApplyStatusHistory], int]:
        """Return the caller's history across all of their apply jobs.

        Rows are sorted newest-first (``created_at`` desc, ``id`` desc)
        so the dashboard can render the most recent transition at the
        top of the list. The total count is returned alongside the
        page so the HTTP layer can build a paginator without a second
        round-trip.
        """
        if self._get_job_user_id is None:
            # A misconfigured test (legacy fixture without the
            # user-resolver wiring) would otherwise return every row
            # in the fake, which is the worst possible failure mode
            # for a user-scoped query. Returning an empty page makes
            # the test fail loudly on the first assertion that
            # expects at least one row.
            return (), 0
        rows = [r for r in self._rows if self._get_job_user_id(r.job_id) == user_id]
        if job_id is not None:
            rows = [r for r in rows if r.job_id == job_id]
        if to_status is not None:
            rows = [r for r in rows if r.to_status == to_status]
        rows.sort(key=lambda r: (r.created_at, r.id), reverse=True)
        total = len(rows)
        page = rows[offset : offset + limit]
        return page, total


# ---------------------------------------------------------------------------
# SQLAlchemy implementation
# ---------------------------------------------------------------------------


class SqlApplyJobRepository:
    """SQLAlchemy-backed repository.

    Construct with either a fixed ``Session`` (caller-managed lifetime)
    or a ``session_factory`` callable (the FastAPI ``get_db`` pattern).
    Each operation opens a short-lived session unless a fixed session
    was supplied.
    """

    def __init__(
        self,
        *,
        session: Session | None = None,
        session_factory: Callable[[], Session] | None = None,
    ) -> None:
        if session is None and session_factory is None:
            raise RuntimeError("SqlApplyJobRepository requires a Session or session_factory")
        self._session = session
        self._session_factory = session_factory

    def _scope(self) -> Session:
        if self._session is not None:
            return self._session
        if self._session_factory is None:
            raise RuntimeError("SqlApplyJobRepository is not bound to a session")
        return self._session_factory()

    def _close_if_ephemeral(self, session: Session) -> None:
        if self._session is None:
            session.close()

    # -- writers ---------------------------------------------------------

    def create(self, job: ApplyJob) -> ApplyJob:
        session = self._scope()
        try:
            session.add(job)
            session.commit()
            session.refresh(job)
            return job
        except Exception:
            session.rollback()
            raise
        finally:
            self._close_if_ephemeral(session)

    def update_status(
        self,
        job_id: uuid.UUID,
        status: str,
        *,
        external_application_id: str | None = None,
        next_run_at: datetime | None = None,
        finished_at: datetime | None = None,
    ) -> ApplyJob:
        session = self._scope()
        try:
            existing = session.get(ApplyJob, job_id)
            if existing is None:
                raise KeyError(f"apply job {job_id} not found")
            existing.status = status
            if external_application_id is not None:
                existing.external_application_id = external_application_id
            if next_run_at is not None:
                existing.next_run_at = next_run_at
            if finished_at is not None:
                existing.finished_at = finished_at
            session.commit()
            session.refresh(existing)
            return existing
        except Exception:
            session.rollback()
            raise
        finally:
            self._close_if_ephemeral(session)

    def mark_attempt(self, job_id: uuid.UUID, error: str) -> ApplyJob:
        session = self._scope()
        try:
            existing = session.get(ApplyJob, job_id)
            if existing is None:
                raise KeyError(f"apply job {job_id} not found")
            existing.attempts = (existing.attempts or 0) + 1
            existing.last_error = error
            session.commit()
            session.refresh(existing)
            return existing
        except Exception:
            session.rollback()
            raise
        finally:
            self._close_if_ephemeral(session)

    # -- readers ---------------------------------------------------------

    def get_by_id(self, job_id: uuid.UUID) -> ApplyJob | None:
        session = self._scope()
        try:
            return session.get(ApplyJob, job_id)
        finally:
            self._close_if_ephemeral(session)

    def get_by_match(self, match_id: uuid.UUID) -> ApplyJob | None:
        session = self._scope()
        try:
            statement = select(ApplyJob).where(ApplyJob.match_id == match_id)
            return session.scalars(statement).first()
        finally:
            self._close_if_ephemeral(session)

    def list_by_user(
        self,
        user_id: uuid.UUID,
        *,
        limit: int = 50,
    ) -> Sequence[ApplyJob]:
        session = self._scope()
        try:
            statement = (
                select(ApplyJob)
                .where(ApplyJob.user_id == user_id)
                .order_by(ApplyJob.created_at.desc(), ApplyJob.id.desc())
                .limit(limit)
            )
            return list(session.scalars(statement).all())
        finally:
            self._close_if_ephemeral(session)

    def list_pending(self, *, limit: int = 50) -> Sequence[ApplyJob]:
        """Return claimable rows ordered by ``created_at`` ascending."""
        session = self._scope()
        try:
            now = datetime.now(UTC)
            statement = (
                select(ApplyJob)
                .where(
                    ApplyJob.status == ApplyJobStatus.QUEUED.value,
                    or_(
                        ApplyJob.next_run_at.is_(None),
                        ApplyJob.next_run_at <= now,
                    ),
                )
                .order_by(ApplyJob.created_at.asc(), ApplyJob.id.asc())
                .limit(limit)
            )
            return list(session.scalars(statement).all())
        finally:
            self._close_if_ephemeral(session)

    def claim_next(self) -> ApplyJob | None:
        """Atomically claim the oldest claimable row.

        The transition is committed before the method returns; the
        caller is guaranteed to see ``status == running`` and
        ``attempts`` incremented on a subsequent read.
        """
        session = self._scope()
        try:
            now = datetime.now(UTC)
            statement = (
                select(ApplyJob)
                .where(
                    ApplyJob.status == ApplyJobStatus.QUEUED.value,
                    or_(
                        ApplyJob.next_run_at.is_(None),
                        ApplyJob.next_run_at <= now,
                    ),
                )
                .order_by(ApplyJob.created_at.asc(), ApplyJob.id.asc())
                .limit(1)
            )
            # On PostgreSQL the production deployment uses
            # ``with_for_update(skip_locked=True)`` to let multiple
            # workers drain the queue in parallel. SQLite (used in the
            # tests) serialises access through the database lock, so
            # the lock hint is unnecessary there. We attach it through
            # ``getattr`` so the same code path runs on both engines.
            bind = session.bind
            if (
                bind is not None
                and getattr(bind, "dialect", None) is not None
                and bind.dialect.name == "postgresql"
            ):
                statement = statement.with_for_update(skip_locked=True)
            target = session.scalars(statement).first()
            if target is None:
                return None
            target.status = ApplyJobStatus.RUNNING.value
            target.attempts = (target.attempts or 0) + 1
            target.started_at = datetime.now(UTC)
            session.commit()
            session.refresh(target)
            return target
        except Exception:
            session.rollback()
            raise
        finally:
            self._close_if_ephemeral(session)


# ---------------------------------------------------------------------------
# SQLAlchemy history implementation
# ---------------------------------------------------------------------------


class SqlApplyStatusHistoryRepository:
    """SQLAlchemy-backed :class:`ApplyStatusHistory` repository.

    Mirrors the in-memory implementation: ``create`` appends a row,
    ``list_by_job`` returns the rows for a job in chronological order.
    Constructed with a fixed ``Session`` (caller-managed lifetime) or
    a ``session_factory`` callable (FastAPI's ``get_db`` pattern).
    """

    def __init__(
        self,
        *,
        session: Session | None = None,
        session_factory: Callable[[], Session] | None = None,
    ) -> None:
        if session is None and session_factory is None:
            raise RuntimeError(
                "SqlApplyStatusHistoryRepository requires a Session or session_factory"
            )
        self._session = session
        self._session_factory = session_factory

    def _scope(self) -> Session:
        if self._session is not None:
            return self._session
        if self._session_factory is None:
            raise RuntimeError("SqlApplyStatusHistoryRepository is not bound to a session")
        return self._session_factory()

    def _close_if_ephemeral(self, session: Session) -> None:
        if self._session is None:
            session.close()

    def create(self, row: ApplyStatusHistory) -> ApplyStatusHistory:
        session = self._scope()
        try:
            session.add(row)
            session.commit()
            session.refresh(row)
            return row
        except Exception:
            session.rollback()
            raise
        finally:
            self._close_if_ephemeral(session)

    def list_by_job(self, job_id: uuid.UUID) -> Sequence[ApplyStatusHistory]:
        session = self._scope()
        try:
            statement = (
                select(ApplyStatusHistory)
                .where(ApplyStatusHistory.job_id == job_id)
                .order_by(ApplyStatusHistory.created_at.asc(), ApplyStatusHistory.id.asc())
            )
            return list(session.scalars(statement).all())
        finally:
            self._close_if_ephemeral(session)

    def list_by_user(
        self,
        user_id: uuid.UUID,
        *,
        job_id: uuid.UUID | None = None,
        to_status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[Sequence[ApplyStatusHistory], int]:
        """Return the caller's history across all of their apply jobs.

        The query JOINs ``apply_status_history`` with ``apply_jobs`` so
        the ``user_id`` filter does not need to be denormalised onto the
        history table. Both the ``COUNT(*)`` and the page are issued in
        the same transaction so the dashboard's ``total`` always
        matches the page contents.

        The optional ``job_id`` and ``to_status`` filters are appended
        to the ``WHERE`` clause only when supplied so the planner can
        pick the best index for the common "list everything" case.
        """
        from sqlalchemy import func

        from job_apply.features.apply_worker.models import ApplyJob

        session = self._scope()
        try:
            # The shared filter conditions are applied to both the
            # ``COUNT(*)`` and the page query so a change to the filter
            # contract only needs to be made in one place.
            conditions = [ApplyJob.user_id == user_id]
            if job_id is not None:
                conditions.append(ApplyStatusHistory.job_id == job_id)
            if to_status is not None:
                conditions.append(ApplyStatusHistory.to_status == to_status)

            count_statement = (
                select(func.count())
                .select_from(ApplyStatusHistory)
                .join(ApplyJob, ApplyStatusHistory.job_id == ApplyJob.id)
                .where(*conditions)
            )
            total = int(session.scalar(count_statement) or 0)

            page_statement = (
                select(ApplyStatusHistory)
                .join(ApplyJob, ApplyStatusHistory.job_id == ApplyJob.id)
                .where(*conditions)
                .order_by(
                    ApplyStatusHistory.created_at.desc(),
                    ApplyStatusHistory.id.desc(),
                )
                .limit(limit)
                .offset(offset)
            )
            items = list(session.scalars(page_statement).all())
            return items, total
        finally:
            self._close_if_ephemeral(session)


__all__ = [
    "ApplyJobRepository",
    "ApplyStatusHistoryRepository",
    "InMemoryApplyJobRepository",
    "InMemoryApplyStatusHistoryRepository",
    "SqlApplyJobRepository",
    "SqlApplyStatusHistoryRepository",
]
