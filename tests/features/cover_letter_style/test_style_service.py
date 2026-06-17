"""TDD tests for the ``CoverLetterStyleService`` use cases.

The service exposes a small surface tuned for the one-style-per-user
contract: ``get_or_default``, ``upsert``, and ``delete``. We exercise it
through the in-memory repository so the slice is fast and deterministic.
"""

from __future__ import annotations

import uuid

import pytest
import pytest as _pytest  # noqa: F401  (kept for symmetry with siblings)

from apply_pilot.features.cover_letter_style.models import CoverLetterStyle
from apply_pilot.features.cover_letter_style.repository import (
    InMemoryCoverLetterStyleRepository,
)
from apply_pilot.features.cover_letter_style.schemas import (
    CoverLetterStyleUpdate,
)
from apply_pilot.features.cover_letter_style.service import CoverLetterStyleService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def repo() -> InMemoryCoverLetterStyleRepository:
    return InMemoryCoverLetterStyleRepository()


@pytest.fixture
def service(repo: InMemoryCoverLetterStyleRepository) -> CoverLetterStyleService:
    return CoverLetterStyleService(repo)


@pytest.fixture
def user_id() -> uuid.UUID:
    return uuid.uuid4()


# ---------------------------------------------------------------------------
# get_or_default
# ---------------------------------------------------------------------------


def test_get_or_default_returns_in_memory_default_when_none_exists(
    service: CoverLetterStyleService, user_id: uuid.UUID
) -> None:
    """When the user has no style, return a default in memory (not persisted)."""
    style = service.get_or_default(user_id)

    assert style.tone == "professional"
    assert style.length == "medium"
    assert style.focus_areas == []
    assert style.avoid_phrases == []
    assert style.extra_instructions is None
    assert style.user_id == user_id
    # Default must NOT be persisted.
    assert service.repo.get_by_user(user_id) is None


def test_get_or_default_returns_existing_style(
    service: CoverLetterStyleService, user_id: uuid.UUID
) -> None:
    """If a style exists, ``get_or_default`` must return it as-is."""
    payload = CoverLetterStyleUpdate(
        tone="friendly",
        length="short",
        focus_areas=["teamwork"],
        avoid_phrases=["ninja"],
        extra_instructions="Be warm.",
    )
    upserted = service.upsert(user_id, payload)

    fetched = service.get_or_default(user_id)

    assert fetched.id == upserted.id
    assert fetched.tone == "friendly"
    assert fetched.length == "short"
    assert fetched.focus_areas == ["teamwork"]
    assert fetched.avoid_phrases == ["ninja"]
    assert fetched.extra_instructions == "Be warm."


# ---------------------------------------------------------------------------
# upsert
# ---------------------------------------------------------------------------


def test_upsert_creates_when_no_style_exists(
    service: CoverLetterStyleService, user_id: uuid.UUID
) -> None:
    """First ``upsert`` for a user must create a new style row."""
    payload = CoverLetterStyleUpdate(
        tone="concise",
        length="medium",
        focus_areas=["technical_skills"],
    )

    result = service.upsert(user_id, payload)

    assert result.id is not None
    assert result.user_id == user_id
    assert result.tone == "concise"
    assert result.length == "medium"
    assert result.focus_areas == ["technical_skills"]
    assert result.avoid_phrases == []  # default

    persisted = service.repo.get_by_user(user_id)
    assert persisted is not None
    assert persisted.id == result.id


def test_upsert_updates_existing_style(
    service: CoverLetterStyleService, user_id: uuid.UUID
) -> None:
    """Second ``upsert`` for the same user must update, not insert."""
    first = service.upsert(
        user_id,
        CoverLetterStyleUpdate(tone="friendly", focus_areas=["teamwork"]),
    )

    second = service.upsert(
        user_id,
        CoverLetterStyleUpdate(
            tone="formal",
            length="long",
            focus_areas=["leadership", "results"],
            avoid_phrases=["rockstar"],
            extra_instructions="Quantify impact.",
        ),
    )

    assert second.id == first.id
    assert second.tone == "formal"
    assert second.length == "long"
    assert second.focus_areas == ["leadership", "results"]
    assert second.avoid_phrases == ["rockstar"]
    assert second.extra_instructions == "Quantify impact."


def test_upsert_with_minimal_payload_applies_defaults(
    service: CoverLetterStyleService, user_id: uuid.UUID
) -> None:
    """An empty-ish payload must still create a usable style (with defaults)."""
    result = service.upsert(user_id, CoverLetterStyleUpdate())

    assert result.tone == "professional"
    assert result.length == "medium"
    assert result.focus_areas == []
    assert result.avoid_phrases == []
    assert result.extra_instructions is None


def test_upsert_rejects_invalid_tone(service: CoverLetterStyleService, user_id: uuid.UUID) -> None:
    """Pydantic validation must reject unknown tone values."""
    with pytest.raises(ValueError):
        CoverLetterStyleUpdate(tone="casual-slang")  # type: ignore[arg-type]


def test_upsert_rejects_invalid_length(
    service: CoverLetterStyleService, user_id: uuid.UUID
) -> None:
    """Pydantic validation must reject unknown length values."""
    with pytest.raises(ValueError):
        CoverLetterStyleUpdate(length="epic")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


def test_delete_returns_true_when_style_existed(
    service: CoverLetterStyleService, user_id: uuid.UUID
) -> None:
    """Deleting an existing style must return True."""
    service.upsert(user_id, CoverLetterStyleUpdate(tone="friendly"))

    deleted = service.delete(user_id)

    assert deleted is True
    assert service.repo.get_by_user(user_id) is None


def test_delete_returns_false_when_no_style_existed(
    service: CoverLetterStyleService, user_id: uuid.UUID
) -> None:
    """Deleting a missing style must return False (idempotent semantics)."""
    assert service.delete(user_id) is False


def test_delete_after_get_or_default_does_not_persist_default(
    service: CoverLetterStyleService, user_id: uuid.UUID
) -> None:
    """Calling ``get_or_default`` does not materialise a row, so ``delete``
    should still report no-op."""
    service.get_or_default(user_id)

    assert service.delete(user_id) is False


# ---------------------------------------------------------------------------
# Repository interaction smoke test
# ---------------------------------------------------------------------------


def test_upsert_round_trip_uses_repository(
    service: CoverLetterStyleService,
    repo: InMemoryCoverLetterStyleRepository,
    user_id: uuid.UUID,
) -> None:
    """The service must persist through the repository contract."""
    service.upsert(
        user_id,
        CoverLetterStyleUpdate(tone="formal", focus_areas=["results"]),
    )

    stored = repo.get_by_user(user_id)
    assert stored is not None
    assert stored.tone == "formal"
    assert stored.focus_areas == ["results"]
    assert isinstance(stored, CoverLetterStyle)
