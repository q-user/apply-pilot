"""Resumes persistence gateway.

The repository is the only module allowed to touch the ``resumes`` table.
It translates domain operations into SQLAlchemy operations and returns
ORM objects; the service layer is responsible for mapping them to DTOs.

The class is intentionally small for the M1 skeleton: a single user can
own many resumes, so the principal read paths are
``get(resume_id)`` (with a user-id check enforced at the service layer)
and ``list_for_user(user_id)``.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from apply_pilot.features.resumes.models import Resume


class ResumesRepository:
    """SQLAlchemy-backed repository for :class:`Resume` aggregates."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def create(
        self,
        *,
        user_id: uuid.UUID,
        filename: str,
        content_type: str,
        size: int,
        raw_text: str,
        plain_text: str,
    ) -> Resume:
        """Insert a new resume row and return the freshly-persisted ORM object."""
        resume = Resume(
            user_id=user_id,
            filename=filename,
            content_type=content_type,
            size=size,
            raw_text=raw_text,
            plain_text=plain_text,
        )
        self._db.add(resume)
        self._db.commit()
        self._db.refresh(resume)
        return resume

    def get(self, resume_id: uuid.UUID) -> Resume | None:
        """Return the resume with the given id, or ``None`` if it does not exist."""
        return self._db.get(Resume, resume_id)

    def list_for_user(self, user_id: uuid.UUID) -> Sequence[Resume]:
        """Return every resume owned by ``user_id``, newest first."""
        statement = (
            select(Resume)
            .where(Resume.user_id == user_id)
            .order_by(Resume.created_at.desc(), Resume.id.desc())
        )
        return list(self._db.scalars(statement).all())


__all__ = ["ResumesRepository"]
