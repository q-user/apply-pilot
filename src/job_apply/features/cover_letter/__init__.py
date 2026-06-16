"""Cover letter vertical slice (M3, issue #31).

Public surface
--------------

* :class:`CoverLetterDraft` — ORM model (one row per match, the
  ``UNIQUE(match_id)`` constraint is the M3 #31 contract).
* :class:`CoverLetterDraftStatus` — lifecycle enum.
* :class:`CoverLetterDraftRepository` — Protocol contract.
* :class:`InMemoryCoverLetterDraftRepository` — fake for tests.
* :class:`SqlCoverLetterDraftRepository` — production implementation.
* :class:`CoverLetterService` — business logic.
* :func:`build_cover_letter_prompt` — pure prompt-rendering function.
* :data:`COVER_LETTER_PROMPT_V1` / :data:`DEFAULT_PROMPT_VERSION` —
  the canonical prompt template and its ``<name>@<semver>`` stamp.

The slice covers exactly one use case: given a :class:`VacancyMatch`,
generate the very first :class:`CoverLetterDraft` using the user's
resume and the vacancy / search-profile / style context. The
version-history / regenerate workflow (issue #32) and the HTTP API
(#32 follow-up) are intentionally out of scope here.
"""

from __future__ import annotations

from job_apply.features.cover_letter.models import (
    CoverLetterDraft,
    CoverLetterDraftStatus,
)
from job_apply.features.cover_letter.repository import (
    CoverLetterDraftRepository,
    InMemoryCoverLetterDraftRepository,
    SqlCoverLetterDraftRepository,
)
from job_apply.features.cover_letter.service import (
    COVER_LETTER_PROMPT_V1,
    DEFAULT_PROMPT_VERSION,
    CoverLetterDependencyMissingError,
    CoverLetterService,
    build_cover_letter_prompt,
)

__all__ = [
    "COVER_LETTER_PROMPT_V1",
    "DEFAULT_PROMPT_VERSION",
    "CoverLetterDependencyMissingError",
    "CoverLetterDraft",
    "CoverLetterDraftRepository",
    "CoverLetterDraftStatus",
    "CoverLetterService",
    "InMemoryCoverLetterDraftRepository",
    "SqlCoverLetterDraftRepository",
    "build_cover_letter_prompt",
]
