"""Business logic for the ``cover_letter`` slice (M3, issues #31 + #32).

The :class:`CoverLetterService` owns the version-history semantics
introduced in issue #32:

* Every call to :meth:`generate_for_match` either creates the very
  first draft (``version == 1``) **or** a new draft whose
  ``version`` is one above the current maximum.
* Every regeneration updates the previous draft's ``replaced_by_id``
  to point at the new draft. The chain forms a doubly-linked list
  (``parent_draft_id`` ← → ``replaced_by_id``) that callers can walk
  in either direction.
* The service never mutates a draft's ``text`` or ``version`` in
  place; new versions always live in their own row.

The service is collaborator-injected: tests build it with the
in-memory repository and a fake generator, the FastAPI dependency in
:mod:`api` builds it with the SQLAlchemy repository and the production
generator.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from job_apply.features.cover_letter.generator import (
    CoverLetterGenerator,
    compute_prompt_hash,
)
from job_apply.features.cover_letter.models import CoverLetterDraft
from job_apply.features.cover_letter.repository import CoverLetterDraftRepository
from job_apply.features.cover_letter.schemas import CoverLetterDraftRead
from job_apply.shared.errors import NotFoundError


class CoverLetterNotFoundError(NotFoundError):
    """The requested cover-letter draft or history does not exist."""

    code: str = "cover_letter_draft_not_found"


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


def _draft_to_dto(draft: CoverLetterDraft) -> CoverLetterDraftRead:
    """Map an ORM row to the public DTO.

    The ORM row and the DTO share the same field set, so the mapping is
    a one-to-one copy. The function exists so that the boundary is
    explicit and so that adding internal fields to the model does not
    accidentally leak them to the HTTP layer.
    """
    return CoverLetterDraftRead(
        id=draft.id,
        match_id=draft.match_id,
        user_id=draft.user_id,
        version=draft.version,
        text=draft.text,
        style=draft.style,
        user_comment=draft.user_comment,
        generation_prompt_hash=draft.generation_prompt_hash,
        parent_draft_id=draft.parent_draft_id,
        replaced_by_id=draft.replaced_by_id,
        created_at=draft.created_at,
        updated_at=draft.updated_at,
    )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class CoverLetterService:
    """Versioned cover-letter generation and lookup.

    The service surface is intentionally small — one writer
    (:meth:`generate_for_match`), one regenerate helper
    (:meth:`regenerate_for_match`), and two readers
    (:meth:`get_latest_for_match`, :meth:`get_history_for_match`).
    """

    def __init__(
        self,
        repository: CoverLetterDraftRepository,
        generator: CoverLetterGenerator,
    ) -> None:
        self._repo = repository
        self._generator = generator

    @property
    def repo(self) -> CoverLetterDraftRepository:
        """Expose the repository for tests that need to assert state."""
        return self._repo

    # -- writers ---------------------------------------------------------

    def generate_for_match(
        self,
        match_id: uuid.UUID,
        *,
        user_id: uuid.UUID,
        style: str | None = None,
        user_comment: str | None = None,
    ) -> CoverLetterDraftRead:
        """Create the first draft for ``match_id`` if none exist.

        If the match already has drafts, this method delegates to
        :meth:`regenerate_for_match` so the call site is symmetric —
        callers do not need to know whether the match is brand-new or
        already has history.
        """
        existing = self._repo.get_latest_for_match(match_id)
        if existing is not None:
            return self.regenerate_for_match(
                match_id,
                user_id=user_id,
                style=style,
                user_comment=user_comment,
            )
        text = self._generator.generate(match_id, style=style, user_comment=user_comment)
        draft = CoverLetterDraft(
            match_id=match_id,
            user_id=user_id,
            version=1,
            text=text,
            style=style,
            user_comment=user_comment,
            parent_draft_id=None,
            replaced_by_id=None,
            generation_prompt_hash=compute_prompt_hash(
                match_id=match_id, style=style, user_comment=user_comment
            ),
        )
        created = self._repo.create(draft)
        return _draft_to_dto(created)

    def regenerate_for_match(
        self,
        match_id: uuid.UUID,
        *,
        user_id: uuid.UUID,
        style: str | None = None,
        user_comment: str | None = None,
    ) -> CoverLetterDraftRead:
        """Create a new version for ``match_id``.

        The previous draft (if any) is back-linked to the new one via
        its ``replaced_by_id`` column. The new draft's ``version`` is
        one above the current maximum; ``parent_draft_id`` points at
        the previous draft.
        """
        previous = self._repo.get_latest_for_match(match_id)
        new_version = (previous.version + 1) if previous is not None else 1
        text = self._generator.generate(match_id, style=style, user_comment=user_comment)
        draft = CoverLetterDraft(
            match_id=match_id,
            user_id=user_id,
            version=new_version,
            text=text,
            style=style,
            user_comment=user_comment,
            parent_draft_id=previous.id if previous is not None else None,
            replaced_by_id=None,
            generation_prompt_hash=compute_prompt_hash(
                match_id=match_id, style=style, user_comment=user_comment
            ),
        )
        created = self._repo.create(draft)
        if previous is not None:
            # Update the previous draft to point at the new one. We do
            # this **after** the insert so a failed insert does not
            # leave the previous draft pointing at a non-existent row.
            self._repo.update_replaced_by(previous.id, created.id)
        return _draft_to_dto(created)

    # -- readers ---------------------------------------------------------

    def get_latest_for_match(
        self,
        match_id: uuid.UUID,
        *,
        user_id: uuid.UUID,
    ) -> CoverLetterDraftRead | None:
        """Return the highest-``version`` draft for ``match_id``.

        Returns ``None`` when the match has no drafts yet — the API
        translates that to a 404 in the HTTP layer.
        """
        draft = self._repo.get_latest_for_match(match_id)
        if draft is None:
            return None
        return _draft_to_dto(draft)

    def get_history_for_match(
        self,
        match_id: uuid.UUID,
        *,
        user_id: uuid.UUID,
    ) -> list[CoverLetterDraftRead]:
        """Return every draft for ``match_id``, newest ``version`` first.

        Returns an empty list when the match has no drafts. The list
        is suitable for serialising directly to JSON; each entry is a
        full :class:`CoverLetterDraftRead` DTO.
        """
        drafts: Sequence[CoverLetterDraft] = self._repo.list_by_match(match_id)
        return [_draft_to_dto(d) for d in drafts]


__all__ = [
    "CoverLetterNotFoundError",
    "CoverLetterService",
]
