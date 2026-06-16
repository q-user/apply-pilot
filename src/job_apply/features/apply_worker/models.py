"""ORM model for the ``apply_worker`` queue (M5, issue #43).

An :class:`ApplyJob` is the storage row that the apply worker drains
to actually submit applications to hh.ru (or, in tests, a fake HH
client). Every :term:`accepted <VACANCY_MATCH_ACCEPTED>` vacancy match
becomes exactly one row — the ``UNIQUE(match_id)`` constraint is the
contract.

Lifecycle
---------

The :class:`ApplyJobStatus` enum is the canonical set of states the
worker walks a job through:

* :attr:`QUEUED`      — newly enqueued, waiting for a worker to claim.
* :attr:`RUNNING`     — a worker has claimed the row and is performing
                       the HTTP call to hh.
* :attr:`SUCCEEDED`   — hh accepted the application; the row stores
                       ``external_application_id`` for traceability.
* :attr:`FAILED`      — the worker hit a transient error and is
                       backing off; ``last_error`` carries the message.
* :attr:`CANCELLED`   — the user cancelled the job; the worker must
                       skip it.
* :attr:`DEAD_LETTER` — the retry budget was exhausted; the row is
                       parked for manual inspection.

Fields
------

* ``id``                       — UUID primary key.
* ``match_id``                 — FK to ``vacancy_matches.id`` (issue
                                 #10). ``UNIQUE`` so a re-acceptance
                                 does not spawn a second row.
* ``user_id``                  — FK to ``users.id`` (issue #11).
                                 Denormalised so ownership checks
                                 never have to join through the match
                                 table.
* ``vacancy_id``               — FK to ``vacancies.id`` (issue #6).
                                 Denormalised so the worker can
                                 ``SELECT`` the queue without joining
                                 through ``vacancy_matches``.
* ``status``                   — one of :class:`ApplyJobStatus`.
* ``attempts``                 — number of times the worker has tried
                                 this job (incremented on every
                                 :meth:`ApplyJobRepository.mark_attempt`).
* ``last_error``               — text of the most recent error; reset
                                 on a successful run.
* ``next_run_at``              — earliest UTC timestamp at which the
                                 worker may re-attempt the job.
                                 ``NULL`` for fresh rows.
* ``idempotency_key``          — SHA-256 hex digest of
                                 ``f"{user_id}|{vacancy_id}|{match_id}"``.
                                 ``UNIQUE`` so a misbehaving caller
                                 cannot double-enqueue the same match.
* ``external_application_id``  — hh's identifier for the resulting
                                 application; ``NULL`` until
                                 :attr:`ApplyJobStatus.SUCCEEDED`.
* ``created_at`` / ``updated_at`` — server-side timestamps.
* ``started_at`` / ``finished_at`` — wall-clock boundaries the worker
                                 stamps at claim / completion.

Indexes
-------

* ``ix_apply_jobs_status_next_run_at`` composite on
  ``(status, next_run_at)`` backs the worker's queue scan
  (``WHERE status = 'queued' AND next_run_at <= now()``).
* ``ix_apply_jobs_user_id`` single column on ``user_id`` for the
  per-user listing on the dashboard.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from job_apply.db import Base
from job_apply.shared.types import GUID


class ApplyJobStatus(StrEnum):
    """Stable set of lifecycle states for an :class:`ApplyJob`.

    The set is intentionally closed: adding a new state is a breaking
    change for any consumer that branches on the string value, and
    renaming an existing value breaks every historical row.

    The M5 #43 contract is "queued / running / succeeded / failed /
    cancelled / dead_letter" — the worker is the only place that
    transitions rows through these states.
    """

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DEAD_LETTER = "dead_letter"


def compute_idempotency_key(
    user_id: uuid.UUID | str,
    vacancy_id: uuid.UUID | str,
    match_id: uuid.UUID | str,
) -> str:
    """Return a stable SHA-256 hex digest of the enqueue triple.

    The format is ``f"{user_id}|{vacancy_id}|{match_id}"``; pipe
    delimiters are illegal in UUID strings so the concatenation is
    unambiguous regardless of input order.

    Accepts UUIDs **or** their ``str()`` form so call sites that hold
    raw token strings (the HTTP layer) do not have to parse them
    twice.
    """
    payload = f"{user_id}|{vacancy_id}|{match_id}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ApplyJob(Base):
    """A queue row that asks the apply worker to submit a job application."""

    __tablename__ = "apply_jobs"
    __table_args__ = (
        UniqueConstraint(
            "match_id",
            name="uq_apply_jobs_match_id",
        ),
        UniqueConstraint(
            "idempotency_key",
            name="uq_apply_jobs_idempotency_key",
        ),
        Index(
            "ix_apply_jobs_status_next_run_at",
            "status",
            "next_run_at",
        ),
        Index(
            "ix_apply_jobs_user_id",
            "user_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)

    match_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("vacancy_matches.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    vacancy_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("vacancies.id", ondelete="CASCADE"),
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=ApplyJobStatus.QUEUED.value,
        server_default=ApplyJobStatus.QUEUED.value,
    )
    attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)

    external_application_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __init__(self, **kwargs: Any) -> None:
        # Apply Python-level defaults so a freshly-constructed
        # ``ApplyJob(...)`` carries the same values the SQL insert would
        # produce. The SQLAlchemy ``default=`` / ``server_default=``
        # settings are still authoritative at flush time — this override
        # only fills the in-memory attribute, which is what the model's
        # public contract promises.
        if "idempotency_key" not in kwargs and all(
            kwargs.get(field) is not None for field in ("user_id", "vacancy_id", "match_id")
        ):
            kwargs["idempotency_key"] = compute_idempotency_key(
                kwargs["user_id"],
                kwargs["vacancy_id"],
                kwargs["match_id"],
            )
        if "status" not in kwargs or kwargs["status"] is None:
            kwargs["status"] = ApplyJobStatus.QUEUED.value
        if "attempts" not in kwargs or kwargs["attempts"] is None:
            kwargs["attempts"] = 0
        super().__init__(**kwargs)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"ApplyJob(id={self.id!s}, match_id={self.match_id!s}, "
            f"status={self.status!r}, attempts={self.attempts})"
        )


class ApplyStatusHistory(Base):
    """An append-only record of a single :class:`ApplyJob` status transition.

    Every status-changing operation the apply worker performs (enqueue,
    claim, complete, fail, cancel) writes one row. Rows are immutable
    from the slice's perspective — the storage layer enforces this
    contract by exposing only :meth:`ApplyStatusHistoryRepository.create`
    and the read methods.

    Fields
    ------

    * ``id``            — UUID primary key.
    * ``job_id``        — FK to ``apply_jobs.id``. ``CASCADE`` delete so
                          a hard-deleted job takes its history with it.
    * ``from_status``   — the status the job transitioned *from*; ``NULL``
                          for the initial creation row (no prior state).
    * ``to_status``     — the status the job transitioned *to*; one of
                          :class:`ApplyJobStatus`.
    * ``error``         — error message for the transition; ``NULL`` for
                          successful transitions.
    * ``metadata_json`` — JSON-encoded extra context (retry attempt
                          number, retryable flag, etc.). ``NULL`` when no
                          extra context is relevant.
    * ``created_at``    — server-side timestamp.

    Indexes
    -------

    * ``ix_apply_status_history_job_id_created_at`` composite on
      ``(job_id, created_at)`` backs ``list_by_job`` and keeps the
      per-job timeline query cheap as the table grows.
    """

    __tablename__ = "apply_status_history"
    __table_args__ = (
        Index(
            "ix_apply_status_history_job_id_created_at",
            "job_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)

    job_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("apply_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )

    from_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    to_status: Mapped[str] = mapped_column(String(50), nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __init__(self, **kwargs: Any) -> None:
        if "id" not in kwargs or kwargs["id"] is None:
            kwargs["id"] = uuid.uuid4()
        if "created_at" not in kwargs or kwargs["created_at"] is None:
            # The SQL column carries a ``server_default`` for the flush
            # path, but freshly-constructed Python instances need an
            # in-memory value too so callers can read ``row.created_at``
            # before the row is persisted. This mirrors the convention
            # used by the in-memory repository for :class:`ApplyJob`.
            kwargs["created_at"] = datetime.now(UTC)
        super().__init__(**kwargs)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"ApplyStatusHistory(id={self.id!s}, job_id={self.job_id!s}, "
            f"from_status={self.from_status!r}, to_status={self.to_status!r})"
        )


__all__ = [
    "ApplyJob",
    "ApplyJobStatus",
    "ApplyStatusHistory",
    "compute_idempotency_key",
]
