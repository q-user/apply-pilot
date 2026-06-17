"""Shared utilities reused by more than one vertical slice.

Anything that lives here must be **small**, **stable**, and **genuinely
cross-slice**. Resist the temptation to grow this package; the cost of moving
code out of a vertical slice should be lower than the cost of crowding
shared abstractions.
"""

from apply_pilot.shared.errors import (
    ConflictError,
    DomainError,
    NotFoundError,
    ValidationError,
)
from apply_pilot.shared.logging import configure_logging
from apply_pilot.shared.schemas import IdentifiedSchema, TimestampedSchema
from apply_pilot.shared.types import GUID

__all__ = [
    "ConflictError",
    "DomainError",
    "GUID",
    "IdentifiedSchema",
    "NotFoundError",
    "TimestampedSchema",
    "ValidationError",
    "configure_logging",
]
