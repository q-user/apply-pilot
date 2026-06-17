"""TDD tests for the :class:`StyleMemoryService` use cases.

The service is the bridge between the ``/accept`` Telegram action and the
persistence gateway: it ingests an accepted cover letter, derives a
deterministic ``style_summary`` from the letter's text, and exposes a
read-side that returns the user's aggregated summary for the API.

We exercise the slice through the in-memory repository; the deterministic
summariser is unit-tested separately in ``test_summariser.py``.
"""

from __future__ import annotations

import uuid

import pytest

from apply_pilot.features.writing_style_memory.repository import InMemoryStyleMemoryRepository
from apply_pilot.features.writing_style_memory.service import StyleMemoryService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def user_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def style_repo() -> InMemoryStyleMemoryRepository:
    return InMemoryStyleMemoryRepository()


@pytest.fixture
def service(style_repo: InMemoryStyleMemoryRepository) -> StyleMemoryService:
    return StyleMemoryService(repository=style_repo)


# ---------------------------------------------------------------------------
# record_accepted_letter
# ---------------------------------------------------------------------------


def test_record_accepted_letter_persists_an_entry(
    service: StyleMemoryService,
    style_repo: InMemoryStyleMemoryRepository,
    user_id: uuid.UUID,
) -> None:
    """Recording an accepted letter must persist a :class:`StyleMemoryEntry`."""
    cover_letter_id = uuid.uuid4()
    letter_text = (
        "Hello, I am writing to apply for the role. "
        "I bring ten years of Python and FastAPI experience."
    )

    entry = service.record_accepted_letter(
        user_id=user_id,
        cover_letter_id=cover_letter_id,
        letter_text=letter_text,
    )

    assert entry is not None
    assert entry.user_id == user_id
    assert entry.cover_letter_id == cover_letter_id
    assert entry.letter_text == letter_text
    # The deterministic summary must be derived from the letter's first sentence.
    assert "first-sentence:" in entry.style_summary
    # The entry must be retrievable through the repository.
    listed = style_repo.list_for_user(user_id)
    assert [e.id for e in listed] == [entry.id]


def test_record_accepted_letter_ignores_empty_letter(
    service: StyleMemoryService,
    style_repo: InMemoryStyleMemoryRepository,
    user_id: uuid.UUID,
) -> None:
    """An empty / whitespace-only letter must not produce an entry."""
    result = service.record_accepted_letter(
        user_id=user_id,
        cover_letter_id=uuid.uuid4(),
        letter_text="   ",
    )
    assert result is None
    assert style_repo.list_for_user(user_id) == []


def test_record_accepted_letter_uses_supplied_cover_letter_id(
    service: StyleMemoryService,
    user_id: uuid.UUID,
) -> None:
    """The service must respect the caller-supplied ``cover_letter_id``."""
    cover_letter_id = uuid.uuid4()
    entry = service.record_accepted_letter(
        user_id=user_id,
        cover_letter_id=cover_letter_id,
        letter_text="A real letter.",
    )
    assert entry is not None
    assert entry.cover_letter_id == cover_letter_id


# ---------------------------------------------------------------------------
# get_aggregated_summary
# ---------------------------------------------------------------------------


def test_get_aggregated_summary_returns_none_when_empty(
    service: StyleMemoryService,
    user_id: uuid.UUID,
) -> None:
    """An empty style memory must surface as ``None`` to the API."""
    assert service.get_aggregated_summary(user_id) is None


def test_get_aggregated_summary_joins_recent_summaries(
    service: StyleMemoryService,
    user_id: uuid.UUID,
) -> None:
    """``get_aggregated_summary`` must return the recent entries concatenated."""
    import time

    service.record_accepted_letter(
        user_id=user_id,
        cover_letter_id=uuid.uuid4(),
        letter_text="Older letter body.",
    )
    time.sleep(0.01)
    service.record_accepted_letter(
        user_id=user_id,
        cover_letter_id=uuid.uuid4(),
        letter_text="Newer letter body.",
    )

    aggregated = service.get_aggregated_summary(user_id)
    assert aggregated is not None
    # The most recent entry's summary should appear first.
    assert aggregated.index("Newer") < aggregated.index("Older")


def test_get_aggregated_summary_isolates_users(
    service: StyleMemoryService,
    user_id: uuid.UUID,
) -> None:
    """Two users' aggregated summaries must not leak into each other."""
    other_user = uuid.uuid4()
    service.record_accepted_letter(
        user_id=user_id,
        cover_letter_id=uuid.uuid4(),
        letter_text="Mine.",
    )
    service.record_accepted_letter(
        user_id=other_user,
        cover_letter_id=uuid.uuid4(),
        letter_text="Theirs.",
    )

    mine = service.get_aggregated_summary(user_id)
    theirs = service.get_aggregated_summary(other_user)
    assert mine is not None and theirs is not None
    assert "Mine" in mine
    assert "Theirs" not in mine
    assert "Theirs" in theirs
    assert "Mine" not in theirs
