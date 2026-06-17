"""Shared cross-slice primitives.

Anything that lives in this module must be a **small**, **stable**, and
**genuinely cross-slice** type — used by two or more vertical slices.
Resist the temptation to grow this module; the cost of moving code out
of a vertical slice should be lower than the cost of crowding shared
abstractions.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import CHAR, TypeDecorator
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.sql.type_api import TypeEngine


class GUID(TypeDecorator):
    """Platform-independent UUID column.

    Uses the native ``UUID`` type on PostgreSQL and falls back to a
    fixed-width ``CHAR(36)`` on every other dialect. Stores and returns
    :class:`uuid.UUID` instances uniformly.
    """

    impl: type[CHAR] = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PG_UUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value: object, dialect: Dialect) -> str | uuid.UUID | None:
        if value is None:
            return None
        if not isinstance(value, uuid.UUID):
            value = uuid.UUID(str(value))
        if dialect.name == "postgresql":
            return value
        return str(value)

    def process_result_value(self, value: object, dialect: Dialect) -> uuid.UUID | None:
        if value is None:
            return None
        if isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(str(value))


__all__ = ["GUID"]
