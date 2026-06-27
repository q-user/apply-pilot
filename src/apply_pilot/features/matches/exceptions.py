"""Domain exceptions for the matches slice (Fix #263)."""
from __future__ import annotations

from apply_pilot.shared.errors import NotFoundError


class MatchNotFoundError(NotFoundError):
    """The requested vacancy match does not exist."""

    code: str = "vacancy_match_not_found"


class MatchNotFoundOrForbiddenError(LookupError):
    """The match does not exist OR is owned by another user.

    Raised as a plain ``LookupError`` so the HTTP layer always returns
    404 regardless of error-code evolution, matching the convention used
    by :class:`MatchOwnershipError` (kept for backward compatibility).
    """

    code: str = "vacancy_match_not_found_or_forbidden"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message
