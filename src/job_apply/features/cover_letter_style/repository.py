"""Persistence gateway for the ``cover_letter_style`` slice.

Three implementations live here:

* :class:`CoverLetterStyleRepository` — Protocol defining the contract
  the service layer depends on.
* :class:`InMemoryCoverLetterStyleRepository` — dict-backed fake for
  tests, with a ``_by_user`` index that mirrors the ``UNIQUE(user_id)``
  constraint.
* :class:`SqlCoverLetterStyleRepository` — production implementation
  backed by a SQLAlchemy ``Session``.

Design notes
------------

* The model's ``focus_areas`` and ``avoid_phrases`` columns are
  ``Text``-typed on the SQL side (portable across sqlite and
  PostgreSQL without an ``ARRAY`` type).
* The :class:`InMemoryCoverLetterStyleRepository` keeps those columns
  as plain Python lists so tests can construct models with
  ``focus_areas=["a", "b"]`` and see the list back unchanged.
* The :class:`SqlCoverLetterStyleRepository` is the boundary that
  JSON-encodes the lists on write and JSON-decodes them on read so
  callers (service, schemas) never have to think about it.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from job_apply.features.cover_letter_style.models import CoverLetterStyle

# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------


def _encode_list(values: list[str] | None) -> str:
    """Encode a list of strings as a JSON string for storage.

    ``None`` is normalised to ``"[]"`` so the column never holds
    ``"null"`` (which would force the read side to handle two formats).
    """
    return json.dumps(list(values) if values else [], ensure_ascii=False)


def _decode_list(raw: Any) -> list[str]:
    """Decode a JSON-encoded list of strings; tolerate any input shape.

    Accepts the column as returned by SQLAlchemy (``str | None``) and
    also tolerates Python lists (so the in-memory implementation can
    reuse the helper for normalisation).
    """
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        return [str(item) for item in raw]
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError):
            return []
        if not isinstance(decoded, list):
            return []
        return [str(item) for item in decoded]
    return []


class CoverLetterStyleRepository(Protocol):
    """Minimal interface the ``CoverLetterStyleService`` relies on.

    The service exchanges ORM rows whose ``focus_areas`` /
    ``avoid_phrases`` attributes are always Python ``list[str]``; the
    SQL implementation owns the JSON encoding.
    """

    def get_by_user(self, user_id: uuid.UUID) -> CoverLetterStyle | None: ...
    def create(self, style: CoverLetterStyle) -> CoverLetterStyle: ...
    def update(self, style: CoverLetterStyle) -> CoverLetterStyle: ...
    def delete_by_user(self, user_id: uuid.UUID) -> bool: ...


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------


class InMemoryCoverLetterStyleRepository:
    """Dict-backed repository for tests.

    Stores the list columns as Python lists so the public contract
    (list in, list out) holds even for the fake implementation.
    """

    def __init__(self) -> None:
        self._by_id: dict[uuid.UUID, CoverLetterStyle] = {}
        self._by_user: dict[uuid.UUID, uuid.UUID] = {}

    def get_by_user(self, user_id: uuid.UUID) -> CoverLetterStyle | None:
        style_id = self._by_user.get(user_id)
        if style_id is None:
            return None
        return self._by_id.get(style_id)

    def create(self, style: CoverLetterStyle) -> CoverLetterStyle:
        if style.user_id in self._by_user:
            raise ValueError(f"cover letter style for user {style.user_id!s} already exists")
        if style.id is None:
            style.id = uuid.uuid4()
        # Defaults: tone / length / lists.
        if not style.tone:
            style.tone = "professional"
        if not style.length:
            style.length = "medium"
        # Normalise list columns to Python lists (decode any string the
        # caller happened to pass, e.g. the model's TEXT default).
        # The model declares ``focus_areas``/``avoid_phrases`` as
        # ``Mapped[str]`` (a JSON-encoded string on disk), but the
        # in-memory repository stores them as ``list[str]`` by design
        # (see module docstring). The ``ty: ignore`` suppresses the
        # ``invalid-assignment`` diagnostic that arises from that
        # intentional type mismatch at the descriptor boundary.
        style.focus_areas = _decode_list(style.focus_areas)  # ty: ignore[invalid-assignment]
        style.avoid_phrases = _decode_list(style.avoid_phrases)  # ty: ignore[invalid-assignment]
        style.created_at = datetime.now(UTC)
        self._by_id[style.id] = style
        self._by_user[style.user_id] = style.id
        return style

    def update(self, style: CoverLetterStyle) -> CoverLetterStyle:
        existing = self._by_id.get(style.id)
        if existing is None:
            # Mirror the SQL behaviour: an update without a matching row
            # is treated as "create the row" so the service can use
            # upsert semantics without branching.
            return self.create(style)
        # Preserve created_at when the caller forgot to pass it through.
        style.created_at = existing.created_at
        style.updated_at = datetime.now(UTC)
        # Keep lists as lists in the in-memory store. See ``create`` for
        # why ``ty: ignore`` is used here.
        style.focus_areas = _decode_list(style.focus_areas)  # ty: ignore[invalid-assignment]
        style.avoid_phrases = _decode_list(style.avoid_phrases)  # ty: ignore[invalid-assignment]
        self._by_id[style.id] = style
        self._by_user[style.user_id] = style.id
        return style

    def delete_by_user(self, user_id: uuid.UUID) -> bool:
        style_id = self._by_user.pop(user_id, None)
        if style_id is None:
            return False
        self._by_id.pop(style_id, None)
        return True


# ---------------------------------------------------------------------------
# SQLAlchemy implementation
# ---------------------------------------------------------------------------


class SqlCoverLetterStyleRepository:
    """SQLAlchemy-backed repository.

    Construct with either a fixed ``Session`` (caller-managed lifetime)
    or a ``session_factory`` callable (the FastAPI ``get_db`` pattern).
    """

    def __init__(
        self,
        *,
        session: Session | None = None,
        session_factory: Callable[[], Session] | None = None,
    ) -> None:
        if session is None and session_factory is None:
            raise RuntimeError(
                "SqlCoverLetterStyleRepository requires a Session or session_factory"
            )
        self._session = session
        self._session_factory = session_factory

    def _scope(self) -> Session:
        if self._session is not None:
            return self._session
        if self._session_factory is None:
            raise RuntimeError("SqlCoverLetterStyleRepository is not bound to a session")
        return self._session_factory()

    def _ensure_list_columns(self, style: CoverLetterStyle) -> None:
        """Re-encode the list columns to JSON text before persistence.

        The result of ``_encode_list`` is a JSON string, so the
        ``Mapped[str]`` type matches and no suppression is needed.
        """
        style.focus_areas = _encode_list(_decode_list(style.focus_areas))
        style.avoid_phrases = _encode_list(_decode_list(style.avoid_phrases))

    def _decode_list_columns(self, style: CoverLetterStyle) -> CoverLetterStyle:
        """Decode the JSON list columns back to Python lists.

        See :meth:`_ensure_list_columns` for the rationale behind the
        ``ty: ignore`` markers.
        """
        style.focus_areas = _decode_list(style.focus_areas)  # ty: ignore[invalid-assignment]
        style.avoid_phrases = _decode_list(style.avoid_phrases)  # ty: ignore[invalid-assignment]
        return style

    def get_by_user(self, user_id: uuid.UUID) -> CoverLetterStyle | None:
        session = self._scope()
        try:
            statement = select(CoverLetterStyle).where(CoverLetterStyle.user_id == user_id)
            style = session.execute(statement).scalar_one_or_none()
            if style is not None:
                self._decode_list_columns(style)
            return style
        finally:
            if self._session_factory is not None:
                session.close()

    def create(self, style: CoverLetterStyle) -> CoverLetterStyle:
        session = self._scope()
        try:
            self._ensure_list_columns(style)
            session.add(style)
            session.commit()
            session.refresh(style)
            # The DB now holds the JSON text; the service layer exchanges
            # lists, so we decode on the way out for consistency.
            self._decode_list_columns(style)
            return style
        except Exception:
            session.rollback()
            raise
        finally:
            if self._session_factory is not None:
                session.close()

    def update(self, style: CoverLetterStyle) -> CoverLetterStyle:
        session = self._scope()
        try:
            existing = session.get(CoverLetterStyle, style.id)
            if existing is None:
                # No matching row → treat as create. The service layer
                # drives the upsert, so callers should be aware they are
                # inserting a brand-new row.
                return self.create(style)
            existing.tone = style.tone
            existing.length = style.length
            existing.extra_instructions = style.extra_instructions
            self._ensure_list_columns(style)
            existing.focus_areas = style.focus_areas
            existing.avoid_phrases = style.avoid_phrases
            session.commit()
            session.refresh(existing)
            return self._decode_list_columns(existing)
        except Exception:
            session.rollback()
            raise
        finally:
            if self._session_factory is not None:
                session.close()

    def delete_by_user(self, user_id: uuid.UUID) -> bool:
        session = self._scope()
        try:
            existing = self.get_by_user(user_id)
            if existing is None:
                return False
            session.delete(existing)
            session.commit()
            return True
        except Exception:
            session.rollback()
            raise
        finally:
            if self._session_factory is not None:
                session.close()


__all__ = [
    "CoverLetterStyleRepository",
    "InMemoryCoverLetterStyleRepository",
    "SqlCoverLetterStyleRepository",
]
