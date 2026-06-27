"""Business logic for the ``cover_letter`` slice (M3, issue #31).

The :class:`CoverLetterService` is the single use case the slice
exposes: given a :class:`VacancyMatch`, look up the user, vacancy,
search profile, resume, and style; ask the LLM to write the cover
letter; persist the result as a :class:`CoverLetterDraft`.

Contract
--------

* ``match_id`` is ``UNIQUE`` on the ``cover_letter_drafts`` table.
  Re-calling :meth:`CoverLetterService.generate_for_match` for a match
  that already has a draft **updates the existing row's content**
  rather than creating a duplicate. The service is the only place
  that knows about the "upsert on UNIQUE" contract — the repository
  exposes a simple ``create`` plus an explicit ``get_by_match`` lookup
  so the upsert logic stays in one place.
* The LLM dependency is the shared :class:`LLMClient` Protocol from
  :mod:`apply_pilot.features.scoring.llm`. There is no per-slice
  ``CoverLetterGenerator`` abstraction: the slice composes the prompt
  itself and hands a plain string to the LLM.
* The slice is collaborator-injected. The constructor accepts the
  cross-slice repositories the use case needs; tests provide
  in-memory fakes, production wires the SQLAlchemy implementations.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Protocol

from apply_pilot.features.cover_letter.models import (
    CoverLetterDraft,
    CoverLetterDraftStatus,
)
from apply_pilot.features.cover_letter.repository import CoverLetterDraftRepository
from apply_pilot.features.cover_letter_style.models import CoverLetterStyle
from apply_pilot.features.matches.repository import VacancyMatchRepository
from apply_pilot.features.resumes.models import Resume
from apply_pilot.features.scoring.llm import LLMClient
from apply_pilot.features.search_profiles.models import SearchProfile
from apply_pilot.features.sources.models import Vacancy
from apply_pilot.features.users.models import User

#: The default ``prompt_version`` stamp applied to a freshly-generated
#: draft when no override is provided. Format: ``<prompt_name>@<semver>``.
DEFAULT_PROMPT_VERSION: str = "cover_letter@1.0.0"

#: The canonical cover-letter prompt template. Plain string
#: interpolation — the LLM expects a single user message with the
#: vacancy, profile, resume, and style context stitched together.
COVER_LETTER_PROMPT_V1: str = (
    "You are writing a cover letter for a job application.\n"
    "\n"
    "== Vacancy ==\n"
    "Title: {vacancy_title}\n"
    "Employer: {vacancy_employer}\n"
    "Location: {vacancy_location}\n"
    "Schedule: {vacancy_schedule}\n"
    "Experience: {vacancy_experience}\n"
    "Skills: {vacancy_skills}\n"
    "Description:\n{vacancy_description}\n"
    "\n"
    "== Search profile ==\n"
    "Title: {profile_title}\n"
    "Keywords: {profile_keywords}\n"
    "Location: {profile_location}\n"
    "Schedule: {profile_schedule}\n"
    "Salary range: {profile_salary}\n"
    "\n"
    "== Resume ==\n"
    "{resume_text}\n"
    "\n"
    "== Style preferences ==\n"
    "Tone: {style_tone}\n"
    "Length: {style_length}\n"
    "Focus areas: {style_focus_areas}\n"
    "Avoid phrases: {style_avoid_phrases}\n"
    "Extra instructions: {style_extra_instructions}\n"
    "\n"
    "Write the cover letter. Do not include a header or salutation "
    "addressed to a specific person — leave that for the user to fill in."
)


# ---------------------------------------------------------------------------
# Reader contracts
# ---------------------------------------------------------------------------


class _UserReaderContract(Protocol):
    """The slice's view of the user repository."""

    def get_by_id(self, user_id: uuid.UUID) -> User | None: ...


class _VacancyReaderContract(Protocol):
    """The slice's view of the vacancy repository."""

    def get_by_id(self, vacancy_id: uuid.UUID) -> Vacancy | None: ...


class _ProfileReaderContract(Protocol):
    """The slice's view of the search-profile repository."""

    def get_by_id(self, profile_id: uuid.UUID) -> SearchProfile | None: ...


class _ResumeReaderContract(Protocol):
    """The slice's view of the resume repository.

    The full :class:`ResumesRepository` exposes ``list_for_user``; the
    cover-letter service only needs the "most recent resume for a user"
    lookup, so the contract is a single method. Tests provide an
    in-memory implementation; production wires a thin adapter around
    :class:`ResumesRepository`.
    """

    def get_active_by_user(self, user_id: uuid.UUID) -> Resume | None: ...


class _StyleReaderContract(Protocol):
    """The slice's view of the cover-letter style repository."""

    def get_by_user(self, user_id: uuid.UUID) -> CoverLetterStyle | None: ...


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CoverLetterDependencyMissingError(LookupError):
    """A cross-slice lookup the service depends on returned ``None``.

    The slice treats a missing match / vacancy / profile / user /
    resume as a programmer error — every call site is expected to
    verify the precondition before invoking the service. The error
    is raised eagerly so a wiring mistake never silently produces
    a draft with ``NULL`` foreign keys.
    """

    code: str = "cover_letter_dependency_missing"


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------


def _format_list(values: Sequence[str] | None) -> str:
    """Render a list of strings for the prompt.

    Empty / ``None`` is rendered as ``"(none)"`` so the LLM never sees
    a bare ``[]`` and has to guess whether that means "user picked
    nothing" or "field is missing". Keeping the wording consistent
    makes the prompt easier to diff in tests.
    """
    if not values:
        return "(none)"
    return ", ".join(values)


def _format_salary(min_value: int | None, max_value: int | None) -> str:
    """Render a salary range for the prompt.

    Both ``None`` → ``"(unspecified)"``. Only one bound → renders that
    bound alone. Both → ``"<min> - <max>"``.
    """
    if min_value is None and max_value is None:
        return "(unspecified)"
    if min_value is None:
        return f"up to {max_value}"
    if max_value is None:
        return f"from {min_value}"
    return f"{min_value} - {max_value}"


def build_cover_letter_prompt(
    *,
    vacancy: Vacancy,
    profile: SearchProfile,
    resume_text: str,
    style: CoverLetterStyle,
    template: str = COVER_LETTER_PROMPT_V1,
) -> str:
    """Render the cover-letter prompt from the four domain inputs.

    The function is pure: it does not touch the LLM, the database, or
    any global state. The only moving part is ``template``, exposed
    for tests and for the (future) prompt-version registry to swap
    the canonical template at runtime.
    """
    return template.format(
        vacancy_title=vacancy.title,
        vacancy_employer=vacancy.employer_name or "(unspecified)",
        vacancy_location=vacancy.location or "(unspecified)",
        vacancy_schedule=vacancy.schedule or "(unspecified)",
        vacancy_experience=vacancy.experience or "(unspecified)",
        vacancy_skills=_format_list(vacancy.skills),
        vacancy_description=vacancy.description or "(no description)",
        profile_title=profile.title,
        profile_keywords=profile.keywords or "(unspecified)",
        profile_location=profile.location or "(unspecified)",
        profile_schedule=profile.schedule or "(unspecified)",
        profile_salary=_format_salary(profile.salary_min, profile.salary_max),
        resume_text=resume_text,
        style_tone=style.tone,
        style_length=style.length,
        style_focus_areas=_format_list(style.focus_areas),
        style_avoid_phrases=_format_list(style.avoid_phrases),
        style_extra_instructions=style.extra_instructions or "(none)",
    )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class CoverLetterService:
    """Generate the first :class:`CoverLetterDraft` for a match.

    The service surface is intentionally small: one writer
    (:meth:`generate_for_match`) and a small set of read-through
    helpers exposed for tests and the (future) API.
    """

    def __init__(
        self,
        llm: LLMClient,
        match_repo: VacancyMatchRepository,
        user_repo: _UserReaderContract,
        vacancy_repo: _VacancyReaderContract,
        profile_repo: _ProfileReaderContract,
        resume_repo: _ResumeReaderContract,
        style_repo: _StyleReaderContract,
        draft_repo: CoverLetterDraftRepository,
        *,
        prompt_version: str = DEFAULT_PROMPT_VERSION,
        template: str = COVER_LETTER_PROMPT_V1,
    ) -> None:
        self._llm = llm
        self._match_repo = match_repo
        self._user_repo = user_repo
        self._vacancy_repo = vacancy_repo
        self._profile_repo = profile_repo
        self._resume_repo = resume_repo
        self._style_repo = style_repo
        self._draft_repo = draft_repo
        self._prompt_version = prompt_version
        self._template = template

    @property
    def draft_repo(self) -> CoverLetterDraftRepository:
        """Expose the repository for tests that need to assert state."""
        return self._draft_repo

    @property
    def match_repo(self) -> VacancyMatchRepository:
        """Expose the match repository for the action handlers.

        The ``/regenerate`` Telegram action (M4, issue #40) needs to
        look up the match to verify ownership before asking the LLM
        to regenerate the cover letter. Exposing the repo here keeps
        the handler independent of the :class:`MatchService` surface
        while still letting it enforce the "this match belongs to
        you" precondition.
        """
        return self._match_repo

    @property
    def prompt_version(self) -> str:
        """Return the ``prompt_version`` stamp applied to new drafts."""
        return self._prompt_version

    # ------------------------------------------------------------------
    # Generation
    # ------------------------------------------------------------------

    async def generate_for_match(self, match_id: uuid.UUID) -> CoverLetterDraft:
        """Generate (or refresh) the cover letter for ``match_id``.

        The flow:

        1. Resolve the match → vacancy, profile, user.
        2. Resolve the user's active resume.
        3. Resolve the user's cover-letter style (or fall back to
           defaults when the user has none).
        4. Build the prompt and ask the LLM.
        5. If a draft already exists for the match, mutate its
           ``content`` in place and return it. Otherwise insert a new
           row in ``"draft"`` status and return it.
        """
        match = self._match_repo.get_by_id(match_id)
        if match is None:
            raise CoverLetterDependencyMissingError(f"match {match_id} not found")
        vacancy = self._vacancy_repo.get_by_id(match.vacancy_id)
        if vacancy is None:
            raise CoverLetterDependencyMissingError(
                f"vacancy {match.vacancy_id} not found for match {match_id}"
            )
        profile = self._profile_repo.get_by_id(match.search_profile_id)
        if profile is None:
            raise CoverLetterDependencyMissingError(
                f"search profile {match.search_profile_id} not found for match {match_id}"
            )
        user = self._user_repo.get_by_id(profile.user_id)
        if user is None:
            raise CoverLetterDependencyMissingError(
                f"user {profile.user_id} not found for match {match_id}"
            )
        resume = self._resume_repo.get_active_by_user(user.id)
        if resume is None:
            raise CoverLetterDependencyMissingError(f"no active resume for user {user.id}")
        style_row = self._style_repo.get_by_user(user.id)
        if style_row is None:
            # No persisted style → fall back to the in-memory defaults
            # from the cover_letter_style slice. ``CoverLetterStyle``
            # is a small enough model that constructing one in place
            # is cheaper than importing the service.
            style_row = CoverLetterStyle(user_id=user.id)

        prompt = build_cover_letter_prompt(
            vacancy=vacancy,
            profile=profile,
            resume_text=resume.plain_text,
            style=style_row,
            template=self._template,
        )
        content = await self._llm.complete(prompt)
        model_used = getattr(self._llm, "model", None)

        existing = self._draft_repo.get_by_match(match_id)
        if existing is not None:
            # ``update_content`` is the durable path: the SQL repo
            # closes its session inside ``get_by_match``, so the
            # instance it returns is detached and a direct attribute
            # write on it would be silently lost. Issue #144.
            return self._draft_repo.update_content(  # type: ignore[invalid-return-type]
                match_id=match_id,
                content=content,
                prompt_version=self._prompt_version,
                model_used=model_used,
            )

        draft = CoverLetterDraft(
            match_id=match_id,
            user_id=user.id,
            content=content,
            prompt_version=self._prompt_version,
            model_used=model_used,
            status=CoverLetterDraftStatus.DRAFT.value,
        )
        return self._draft_repo.create(draft)

    async def regenerate_for_match(self, match_id: uuid.UUID) -> CoverLetterDraft:
        """Regenerate the cover letter for ``match_id`` (M4, issue #40).

        Unlike :meth:`generate_for_match`, this method **requires** a
        draft to already exist for the match — the user is asking for
        a fresh version of something they have already seen, not the
        first one. A missing draft is surfaced as
        :class:`CoverLetterDependencyMissingError` so the caller can
        show a "use ``/review`` to generate one first" hint.

        On success the existing draft is mutated in place:

        * ``content`` is replaced with the new LLM output;
        * ``prompt_version`` is stamped from ``self._prompt_version``;
        * ``model_used`` is updated from the LLM client;
        * ``version`` is bumped (``1 → 2 → 3 …``);
        * ``updated_at`` is set to ``now()``.

        The match's ``UNIQUE`` constraint means there is at most one
        draft per match, so there is no "create a second row" branch
        to worry about. The function returns the same row it just
        mutated, which the action handler records in the audit log
        and renders in the chat reply.
        """
        existing = self._draft_repo.get_by_match(match_id)
        if existing is None:
            raise CoverLetterDependencyMissingError(f"no cover letter draft for match {match_id}")

        match = self._match_repo.get_by_id(match_id)
        if match is None:
            raise CoverLetterDependencyMissingError(f"match {match_id} not found")
        vacancy = self._vacancy_repo.get_by_id(match.vacancy_id)
        if vacancy is None:
            raise CoverLetterDependencyMissingError(
                f"vacancy {match.vacancy_id} not found for match {match_id}"
            )
        profile = self._profile_repo.get_by_id(match.search_profile_id)
        if profile is None:
            raise CoverLetterDependencyMissingError(
                f"search profile {match.search_profile_id} not found for match {match_id}"
            )
        user = self._user_repo.get_by_id(profile.user_id)
        if user is None:
            raise CoverLetterDependencyMissingError(
                f"user {profile.user_id} not found for match {match_id}"
            )
        resume = self._resume_repo.get_active_by_user(user.id)
        if resume is None:
            raise CoverLetterDependencyMissingError(f"no active resume for user {user.id}")
        style_row = self._style_repo.get_by_user(user.id)
        if style_row is None:
            style_row = CoverLetterStyle(user_id=user.id)

        prompt = build_cover_letter_prompt(
            vacancy=vacancy,
            profile=profile,
            resume_text=resume.plain_text,
            style=style_row,
            template=self._template,
        )
        content = await self._llm.complete(prompt)
        model_used = getattr(self._llm, "model", None)

        existing.content = content
        existing.prompt_version = self._prompt_version
        existing.model_used = model_used
        existing.version = existing.version + 1
        existing.updated_at = datetime.now(UTC)
        return existing


__all__ = [
    "COVER_LETTER_PROMPT_V1",
    "DEFAULT_PROMPT_VERSION",
    "CoverLetterDependencyMissingError",
    "CoverLetterService",
    "build_cover_letter_prompt",
]
