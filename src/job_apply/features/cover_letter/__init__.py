"""Cover letter vertical slice.

Public surface
--------------

* :class:`CoverLetterDraft` — ORM model (one row per draft version).
* :class:`CoverLetterService` — business logic (generate, regenerate,
  read latest, read history).
* :class:`CoverLetterDraftRepository` — Protocol contract.
* :class:`InMemoryCoverLetterDraftRepository` — fake for tests.
* :class:`SqlCoverLetterDraftRepository` — production implementation.
* :class:`CoverLetterGenerator` — Protocol the service depends on.
* :class:`StubCoverLetterGenerator` — network-free default.
* :class:`CoverLetterDraftRead` / :class:`CoverLetterRegenerateRequest` —
  public DTOs.

Endpoints
---------

* ``GET /cover-letters/by-match/{match_id}`` — latest draft for match.
* ``GET /cover-letters/by-match/{match_id}/history`` — every version
  (newest first).
* ``POST /cover-letters/regenerate/{match_id}`` — create a new version
  (or the first one when no drafts exist).

The slice keeps every version of a cover letter for a match
(``CoverLetterDraft.match_id`` is not unique). The
``(match_id, version)`` composite index on the table keeps
``get_latest_for_match`` and ``list_by_match`` cheap. The
``parent_draft_id`` / ``replaced_by_id`` pair forms a doubly-linked
list that callers can walk in either direction.
"""

from __future__ import annotations

from job_apply.features.cover_letter.generator import (
    CoverLetterGenerator,
    StubCoverLetterGenerator,
    compute_prompt_hash,
)
from job_apply.features.cover_letter.models import CoverLetterDraft
from job_apply.features.cover_letter.repository import (
    CoverLetterDraftRepository,
    InMemoryCoverLetterDraftRepository,
    SqlCoverLetterDraftRepository,
)
from job_apply.features.cover_letter.schemas import (
    CoverLetterDraftRead,
    CoverLetterRegenerateRequest,
)
from job_apply.features.cover_letter.service import (
    CoverLetterNotFoundError,
    CoverLetterService,
)

__all__ = [
    "CoverLetterDraft",
    "CoverLetterDraftRead",
    "CoverLetterDraftRepository",
    "CoverLetterGenerator",
    "CoverLetterNotFoundError",
    "CoverLetterRegenerateRequest",
    "CoverLetterService",
    "InMemoryCoverLetterDraftRepository",
    "SqlCoverLetterDraftRepository",
    "StubCoverLetterGenerator",
    "compute_prompt_hash",
]
