"""Shared utilities reused by more than one vertical slice.

Anything that lives here must be **small**, **stable**, and **genuinely
cross-slice**. Resist the temptation to grow this package; the cost of moving
code out of a vertical slice should be lower than the cost of crowding
shared abstractions.
"""

from job_apply.shared.errors import (
    ConflictError,
    DomainError,
    NotFoundError,
    ValidationError,
)
from job_apply.shared.logging import configure_logging
from job_apply.shared.schemas import IdentifiedSchema, TimestampedSchema

__all__ = [
    "ConflictError",
    "DomainError",
    "IdentifiedSchema",
    "NotFoundError",
    "TimestampedSchema",
    "ValidationError",
    "configure_logging",
]
