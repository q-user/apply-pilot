"""TDD tests for the :class:`ReviewActionHandler` (M4, issue #36).

The handler is the use-case for the ``/review <match_id>`` Telegram
command. It:

* resolves the local ``user_id`` from the ``telegram_user_id`` of the
  incoming update;
* loads the target match and verifies ownership through
  :class:`MatchService`;
* loads the underlying :class:`Vacancy` and the latest
  :class:`CoverLetterDraft` (if any);
* renders a Markdown-formatted vacancy review card via the pure
  :func:`render_review_card` function;
* returns a :class:`SendMessageRequest` carrying the rendered card.

All collaborators are wired through the constructor with the in-memory
fakes so the slice is exercised end-to-end without external I/O and
without ``Mock``.
"""

from __future__ import annotations

import uuid

import pytest

from job_apply.features.cover_letter.models import CoverLetterDraft
from job_apply.features.cover_letter.repository import InMemoryCoverLetterDraftRepository
from job_apply.features.matches.models import MatchStatus, VacancyMatch
from job_apply.features.matches.repository import InMemoryVacancyMatchRepository
from job_apply.features.matches.service import MatchService
from job_apply.features.search_profiles.models import SearchProfile
from job_apply.features.search_profiles.repository import InMemorySearchProfileRepository
from job_apply.features.sources.models import Vacancy
from job_apply.features.sources.repository import InMemoryVacancyRepository
from job_apply.features.telegram.actions.review import (
    ReviewActionHandler,
    render_review_card,
)
from job_apply.features.telegram.bot import TelegramBot, TelegramSettings
from job_apply.features.telegram.dto import SendMessageRequest
from job_apply.features.telegram.repository import InMemoryTelegramAccountRepository

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _vacancy(
    source_id: str = "hh-rev-1",
    *,
    title: str = "Senior Backend Engineer",
    employer: str | None = "Acme Corp",
    location: str | None = "Remote",
    salary_from: int | None = 150_000,
    salary_to: int | None = 250_000,
    salary_currency: str = "RUR",
    skills: list[str] | None = None,
) -> Vacancy:
    """Build a fully-populated :class:`Vacancy` for review-card tests."""
    v = Vacancy(
        source="hh",
        source_id=source_id,
        title=title,
        employer_name=employer,
        location=location,
        salary_from=salary_from,
        salary_to=salary_to,
        salary_currency=salary_currency,
        salary_gross=False,
        skills=skills or ["Python", "FastAPI", "PostgreSQL"],
        description="We are looking for a senior backend engineer to join our team.",
        raw_data={"id": source_id, "name": title},
    )
    v.id = uuid.uuid4()
    return v


def _profile(user_id: uuid.UUID) -> SearchProfile:
    """Build a :class:`SearchProfile` owned by ``user_id``."""
    p = SearchProfile(user_id=user_id, title="Python", is_active=True)
    p.id = uuid.uuid4()
    return p


@pytest.fixture
def user_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def other_user_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def telegram_user_id() -> int:
    return 707070


@pytest.fixture
def profile_repo() -> InMemorySearchProfileRepository:
    return InMemorySearchProfileRepository()


@pytest.fixture
def match_repo(
    profile_repo: InMemorySearchProfileRepository,
) -> InMemoryVacancyMatchRepository:
    return InMemoryVacancyMatchRepository(list_user_profiles=profile_repo.list_by_user)


@pytest.fixture
def match_service(
    match_repo: InMemoryVacancyMatchRepository,
    profile_repo: InMemorySearchProfileRepository,
) -> MatchService:
    return MatchService(match_repo=match_repo, profile_repo=profile_repo)


@pytest.fixture
def vacancy_repo() -> InMemoryVacancyRepository:
    return InMemoryVacancyRepository()


@pytest.fixture
def cover_letter_repo() -> InMemoryCoverLetterDraftRepository:
    return InMemoryCoverLetterDraftRepository()


@pytest.fixture
def telegram_account_repo() -> InMemoryTelegramAccountRepository:
    return InMemoryTelegramAccountRepository()


@pytest.fixture
def handler(
    match_service: MatchService,
    vacancy_repo: InMemoryVacancyRepository,
    cover_letter_repo: InMemoryCoverLetterDraftRepository,
    telegram_account_repo: InMemoryTelegramAccountRepository,
) -> ReviewActionHandler:
    return ReviewActionHandler(
        match_service=match_service,
        vacancy_repo=vacancy_repo,
        cover_letter_repo=cover_letter_repo,
        telegram_account_repo=telegram_account_repo,
    )


def _seed_match(
    match_repo: InMemoryVacancyMatchRepository,
    profile_repo: InMemorySearchProfileRepository,
    vacancy_repo: InMemoryVacancyRepository,
    *,
    user_id: uuid.UUID,
    status: str = MatchStatus.SCORED.value,
    score: int | None = 85,
    explanation: str | None = "Strong match for a senior backend role.",
    vacancy: Vacancy | None = None,
) -> tuple[SearchProfile, Vacancy, VacancyMatch]:
    """Create a profile, vacancy, and match owned by ``user_id``.

    Returns the (profile, vacancy, match) tuple so the caller can
    build the cover letter on top of the match.
    """
    profile = _profile(user_id)
    profile_repo.create(profile)
    vacancy = vacancy or _vacancy()
    vacancy_repo.upsert(vacancy)
    match = VacancyMatch(
        search_profile_id=profile.id,
        vacancy_id=vacancy.id,
        status=status,
        score=score,
        explanation=explanation,
    )
    created = match_repo.create(match)
    return profile, vacancy, created


def _link_telegram(
    repo: InMemoryTelegramAccountRepository,
    *,
    user_id: uuid.UUID,
    telegram_user_id: int,
) -> None:
    """Create a Telegram account linking ``telegram_user_id`` to ``user_id``."""
    repo.create(user_id=user_id, telegram_user_id=telegram_user_id, username="alice")


# ---------------------------------------------------------------------------
# parse_review_command
# ---------------------------------------------------------------------------


def test_parse_review_command() -> None:
    """``/review <match_id>`` parses to a ReviewCommand with the match_id."""
    match_id = "11111111-1111-1111-1111-111111111111"
    from job_apply.features.telegram.actions.review import parse_review_command

    command = parse_review_command(f"/review {match_id}")

    assert command is not None
    assert command.match_id == uuid.UUID(match_id)


def test_parse_review_command_without_args_returns_none() -> None:
    """``/review`` with no args must return None so the caller shows help text."""
    from job_apply.features.telegram.actions.review import parse_review_command

    assert parse_review_command("/review") is None
    assert parse_review_command("/review   ") is None


def test_parse_review_command_with_invalid_uuid_returns_none() -> None:
    """``/review <garbage>`` must return None so the caller shows usage text."""
    from job_apply.features.telegram.actions.review import parse_review_command

    assert parse_review_command("/review not-a-uuid") is None


# ---------------------------------------------------------------------------
# render_review_card
# ---------------------------------------------------------------------------


def test_render_review_card_with_full_data() -> None:
    """Every card field and the action buttons render in the card body."""
    # Build the inputs directly — render is a pure function and does
    # not need the in-memory fakes to be populated.
    user = uuid.uuid4()
    profile = _profile(user)
    vacancy = _vacancy(
        title="Senior Backend Engineer",
        employer="Acme Corp",
        location="Remote",
        salary_from=150_000,
        salary_to=250_000,
        salary_currency="RUR",
        skills=["Python", "FastAPI", "PostgreSQL"],
    )
    match = VacancyMatch(
        id=uuid.uuid4(),
        search_profile_id=profile.id,
        vacancy_id=vacancy.id,
        status=MatchStatus.SCORED.value,
        score=85,
        explanation="Strong match for a senior backend role.",
    )
    cover_letter = CoverLetterDraft(
        id=uuid.uuid4(),
        match_id=match.id,
        user_id=user,
        content="Dear hiring team, ...",
        prompt_version="cover_letter@1.0.0",
    )

    card = render_review_card(match, vacancy, cover_letter=cover_letter)

    # Title.
    assert "Senior Backend Engineer" in card
    # Employer.
    assert "Acme Corp" in card
    # Location.
    assert "Remote" in card
    # Salary range — both bounds present.
    assert "150" in card and "250" in card
    # Match score.
    assert "85" in card and "100" in card
    # Explanation text.
    assert "Strong match" in card
    # Skills tags — at least one skill present.
    assert "Python" in card
    # Cover letter status.
    assert "ready" in card.lower() or "generated" in card.lower()
    # Action buttons (next-step commands).
    assert "/accept" in card
    assert "/reject" in card
    assert "/defer" in card
    assert "/regenerate" in card
    # Match id is referenced somewhere so the user can copy it
    # (the renderer MarkdownV2-escapes the dashes, so the literal
    # string would not match — we check for the escape pattern).
    for segment in str(match.id).split("-"):
        assert segment in card


def test_render_review_card_without_cover_letter() -> None:
    """A match with no cover letter must render a 'not generated' status."""
    user = uuid.uuid4()
    profile = _profile(user)
    vacancy = _vacancy()
    match = VacancyMatch(
        id=uuid.uuid4(),
        search_profile_id=profile.id,
        vacancy_id=vacancy.id,
        status=MatchStatus.SCORED.value,
        score=70,
        explanation="Decent match.",
    )

    card = render_review_card(match, vacancy)

    # Cover letter status reflects the absent draft.
    assert "not generated" in card.lower() or "not yet" in card.lower()
    # Action buttons still listed (the user can trigger generation).
    assert "/regenerate" in card


def test_render_review_card_truncates_long_explanation() -> None:
    """An explanation over the limit is truncated and a marker is appended."""
    user = uuid.uuid4()
    profile = _profile(user)
    vacancy = _vacancy()
    long_explanation = "match. " * 100  # ~700 chars
    match = VacancyMatch(
        id=uuid.uuid4(),
        search_profile_id=profile.id,
        vacancy_id=vacancy.id,
        status=MatchStatus.SCORED.value,
        score=60,
        explanation=long_explanation,
    )

    card = render_review_card(match, vacancy)

    # The full explanation is NOT in the card — the renderer truncates.
    assert long_explanation not in card
    # The card mentions truncation explicitly so the user can ask for more.
    assert "..." in card


def test_render_review_card_handles_missing_score() -> None:
    """A match with no LLM score must still render — score shown as N/A."""
    user = uuid.uuid4()
    profile = _profile(user)
    vacancy = _vacancy()
    match = VacancyMatch(
        id=uuid.uuid4(),
        search_profile_id=profile.id,
        vacancy_id=vacancy.id,
        status=MatchStatus.NEW.value,
        score=None,
        explanation=None,
    )

    card = render_review_card(match, vacancy)

    # No crash; score is rendered as N/A.
    assert "n/a" in card.lower()


def test_render_review_card_handles_missing_salary() -> None:
    """A vacancy with no salary must render with an 'unspecified' marker."""
    user = uuid.uuid4()
    profile = _profile(user)
    vacancy = _vacancy(salary_from=None, salary_to=None)
    match = VacancyMatch(
        id=uuid.uuid4(),
        search_profile_id=profile.id,
        vacancy_id=vacancy.id,
        status=MatchStatus.SCORED.value,
        score=50,
    )

    card = render_review_card(match, vacancy)

    assert "unspecified" in card.lower() or "n/a" in card.lower()


def test_render_review_card_handles_missing_skills() -> None:
    """A vacancy with no skills renders a 'none' marker rather than crashing."""
    user = uuid.uuid4()
    profile = _profile(user)
    vacancy = _vacancy(skills=[])
    match = VacancyMatch(
        id=uuid.uuid4(),
        search_profile_id=profile.id,
        vacancy_id=vacancy.id,
        status=MatchStatus.SCORED.value,
        score=50,
    )

    card = render_review_card(match, vacancy)

    # No crash; skills section renders the empty marker.
    assert "skills" in card.lower()


def test_render_review_card_escapes_markdown_special_chars() -> None:
    """User-supplied text (title, employer) is escaped for MarkdownV2."""
    user = uuid.uuid4()
    profile = _profile(user)
    # Title with MarkdownV2 special characters: asterisk, underscore, dot.
    vacancy = _vacancy(title="C++ Engineer (Backend)._remote*", employer="Foo *Bar* Inc.")
    match = VacancyMatch(
        id=uuid.uuid4(),
        search_profile_id=profile.id,
        vacancy_id=vacancy.id,
        status=MatchStatus.SCORED.value,
        score=70,
    )

    card = render_review_card(match, vacancy)

    # The literal unescaped special characters should not appear in
    # user-content fields. A `*` is rendered as `\*` etc.
    # We assert the special characters are not present unescaped inside
    # the title region: the renderer must escape them.
    # At least the title region is checked: the original substring
    # "Foo *Bar* Inc." with bare asterisks must NOT appear.
    assert "Foo *Bar* Inc." not in card
    # The escaped form is present.
    assert r"Foo \*Bar\* Inc\." in card or r"Foo \*Bar\*" in card


# ---------------------------------------------------------------------------
# ReviewActionHandler.handle
# ---------------------------------------------------------------------------


def test_handle_review_returns_card_for_owner(
    handler: ReviewActionHandler,
    match_repo: InMemoryVacancyMatchRepository,
    profile_repo: InMemorySearchProfileRepository,
    vacancy_repo: InMemoryVacancyRepository,
    cover_letter_repo: InMemoryCoverLetterDraftRepository,
    telegram_account_repo: InMemoryTelegramAccountRepository,
    user_id: uuid.UUID,
    telegram_user_id: int,
) -> None:
    """The owner of the match gets back the rendered review card."""
    _, vacancy, match = _seed_match(
        match_repo, profile_repo, vacancy_repo, user_id=user_id, score=85
    )
    _link_telegram(telegram_account_repo, user_id=user_id, telegram_user_id=telegram_user_id)

    response = handler.handle(
        chat_id=100,
        telegram_user_id=telegram_user_id,
        match_id=match.id,
    )

    assert isinstance(response, SendMessageRequest)
    assert response.chat_id == 100
    # Card contains the title and the action buttons.
    assert vacancy.title in response.text
    assert "/accept" in response.text
    assert "/reject" in response.text
    assert "/defer" in response.text
    assert "/regenerate" in response.text
    # The match id is included so the user can copy it for the
    # follow-up commands (dashes are MarkdownV2-escaped in the
    # rendered output, so we check the prefix).
    assert str(match.id).split("-")[0] in response.text


def test_handle_review_includes_cover_letter_status(
    handler: ReviewActionHandler,
    match_repo: InMemoryVacancyMatchRepository,
    profile_repo: InMemorySearchProfileRepository,
    vacancy_repo: InMemoryVacancyRepository,
    cover_letter_repo: InMemoryCoverLetterDraftRepository,
    telegram_account_repo: InMemoryTelegramAccountRepository,
    user_id: uuid.UUID,
    telegram_user_id: int,
) -> None:
    """The card reflects the presence of the latest cover letter draft."""
    _, _, match = _seed_match(match_repo, profile_repo, vacancy_repo, user_id=user_id)
    _link_telegram(telegram_account_repo, user_id=user_id, telegram_user_id=telegram_user_id)

    # No draft yet.
    response = handler.handle(
        chat_id=100,
        telegram_user_id=telegram_user_id,
        match_id=match.id,
    )
    assert "not generated" in response.text.lower()

    # Now add a draft and re-request.
    draft = CoverLetterDraft(
        match_id=match.id,
        user_id=user_id,
        content="Hello, ...",
        prompt_version="cover_letter@1.0.0",
    )
    cover_letter_repo.create(draft)

    response = handler.handle(
        chat_id=100,
        telegram_user_id=telegram_user_id,
        match_id=match.id,
    )
    # Status flipped to "ready" / "generated".
    assert "ready" in response.text.lower() or "generated" in response.text.lower()


def test_handle_review_rejects_unknown_match(
    handler: ReviewActionHandler,
    telegram_account_repo: InMemoryTelegramAccountRepository,
    user_id: uuid.UUID,
    telegram_user_id: int,
) -> None:
    """A request for a non-existent match returns a friendly 'not found' message."""
    _link_telegram(telegram_account_repo, user_id=user_id, telegram_user_id=telegram_user_id)
    unknown_id = uuid.uuid4()

    response = handler.handle(
        chat_id=100,
        telegram_user_id=telegram_user_id,
        match_id=unknown_id,
    )

    assert isinstance(response, SendMessageRequest)
    text = response.text.lower()
    assert "not found" in text or "unknown" in text


def test_handle_review_rejects_non_owner(
    handler: ReviewActionHandler,
    match_repo: InMemoryVacancyMatchRepository,
    profile_repo: InMemorySearchProfileRepository,
    vacancy_repo: InMemoryVacancyRepository,
    telegram_account_repo: InMemoryTelegramAccountRepository,
    user_id: uuid.UUID,
    other_user_id: uuid.UUID,
    telegram_user_id: int,
) -> None:
    """A user trying to review another user's match gets an error and no card content."""
    _, _, match = _seed_match(
        match_repo, profile_repo, vacancy_repo, user_id=other_user_id, score=80
    )
    _link_telegram(telegram_account_repo, user_id=user_id, telegram_user_id=telegram_user_id)

    response = handler.handle(
        chat_id=100,
        telegram_user_id=telegram_user_id,
        match_id=match.id,
    )

    assert isinstance(response, SendMessageRequest)
    text = response.text.lower()
    assert "forbidden" in text or "not" in text or "cannot" in text or "don't" in text
    # No card content leaks: action buttons must not be rendered.
    assert "/accept" not in response.text


def test_handle_review_rejects_unlinked_telegram_account(
    handler: ReviewActionHandler,
    match_repo: InMemoryVacancyMatchRepository,
    profile_repo: InMemorySearchProfileRepository,
    vacancy_repo: InMemoryVacancyRepository,
    user_id: uuid.UUID,
    telegram_user_id: int,
) -> None:
    """An update from a Telegram user with no linked account is refused."""
    _, _, match = _seed_match(match_repo, profile_repo, vacancy_repo, user_id=user_id)

    response = handler.handle(
        chat_id=100,
        telegram_user_id=telegram_user_id,
        match_id=match.id,
    )

    assert isinstance(response, SendMessageRequest)
    text = response.text.lower()
    assert "link" in text or "not linked" in text or "unknown" in text


# ---------------------------------------------------------------------------
# Bot dispatcher integration
# ---------------------------------------------------------------------------


def _review_update(
    text: str,
    *,
    chat_id: int = 600,
    telegram_user_id: int = 600,
) -> dict:
    """Build a minimal Telegram Update carrying a /review text message."""
    return {
        "update_id": 6000,
        "message": {
            "message_id": 60,
            "date": 0,
            "chat": {"id": chat_id, "type": "private"},
            "from": {
                "id": telegram_user_id,
                "is_bot": False,
                "first_name": "Eve",
            },
            "text": text,
        },
    }


def test_dispatcher_routes_review_command(
    match_service: MatchService,
    vacancy_repo: InMemoryVacancyRepository,
    cover_letter_repo: InMemoryCoverLetterDraftRepository,
    telegram_account_repo: InMemoryTelegramAccountRepository,
    match_repo: InMemoryVacancyMatchRepository,
    profile_repo: InMemorySearchProfileRepository,
    user_id: uuid.UUID,
) -> None:
    """The bot delegates ``/review <id>`` to the ReviewActionHandler."""
    _, vacancy, match = _seed_match(
        match_repo, profile_repo, vacancy_repo, user_id=user_id, score=85
    )
    telegram_user_id = 600
    _link_telegram(telegram_account_repo, user_id=user_id, telegram_user_id=telegram_user_id)

    handler = ReviewActionHandler(
        match_service=match_service,
        vacancy_repo=vacancy_repo,
        cover_letter_repo=cover_letter_repo,
        telegram_account_repo=telegram_account_repo,
    )
    bot = TelegramBot(
        settings=TelegramSettings(bot_token="test-token", polling_timeout=30),
        review_handler=handler,
    )

    response = bot.handle_update(_review_update(f"/review {match.id}", telegram_user_id=600))

    assert response is not None
    # The card contains the title and the action buttons.
    assert vacancy.title in response.text
    assert "/accept" in response.text


def test_dispatcher_review_command_without_args_returns_help(
    match_service: MatchService,
    vacancy_repo: InMemoryVacancyRepository,
    cover_letter_repo: InMemoryCoverLetterDraftRepository,
    telegram_account_repo: InMemoryTelegramAccountRepository,
) -> None:
    """``/review`` with no match_id is a usage error — the bot returns the help text."""
    handler = ReviewActionHandler(
        match_service=match_service,
        vacancy_repo=vacancy_repo,
        cover_letter_repo=cover_letter_repo,
        telegram_account_repo=telegram_account_repo,
    )
    bot = TelegramBot(
        settings=TelegramSettings(bot_token="test-token", polling_timeout=30),
        review_handler=handler,
    )

    response = bot.handle_update(_review_update("/review", telegram_user_id=600))

    assert response is not None
    text = response.text.lower()
    # The help text mentions the /review command syntax.
    assert "/review" in text or "usage" in text


def test_dispatcher_includes_review_in_help() -> None:
    """The /help text mentions the /review command."""
    bot = TelegramBot(
        settings=TelegramSettings(bot_token="test-token", polling_timeout=30),
    )
    response = bot.handle_update(_review_update("/help", telegram_user_id=600))
    assert response is not None
    assert "/review" in response.text
