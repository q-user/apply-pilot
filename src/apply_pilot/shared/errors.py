"""Domain error hierarchy.

These errors are the canonical way to signal business-rule failures from a
vertical slice. They are deliberately distinct from HTTP exceptions: the same
``NotFoundError`` should be usable from a FastAPI handler, a CLI command, or a
background worker, with the transport layer (HTTP/gRPC/CLI) choosing how to
translate ``code``/``message`` into a response.

Design rules:

* Always subclass :class:`DomainError`. Callers should be able to catch a
  single base class.
* ``code`` is a stable, machine-readable identifier (``snake_case``). It
  should not change once an error is exposed through a public API; treat it
  as part of the public contract.
* ``message`` is a human-readable string. It is safe to log, but should not
  be parsed.
"""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    """Base class for all domain errors raised by vertical slices.

    ``code`` defaults to ``"domain_error"`` so that ad-hoc subclasses can be
    written without overriding the class attribute. Subclasses that do
    override it provide a more specific machine-readable identifier.
    """

    code: str = "domain_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            # Allow callers to override the class-level default for one-off
            # cases (e.g. external service errors) without inventing a new
            # subclass.
            self.code = code

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class NotFoundError(DomainError):
    """The referenced entity does not exist."""

    code: str = "not_found"

    @classmethod
    def for_entity(cls, entity: str, identifier: Any) -> NotFoundError:
        """Build a ``NotFoundError`` for a missing entity.

        The default message format is stable and includes both the entity
        type and the identifier so logs and HTTP responses stay debuggable
        without leaking internal structure.
        """
        return cls(f"{entity} {identifier!r} not found")


class ValidationError(DomainError):
    """The input failed business-rule validation.

    Use this for semantic violations (e.g. "quantity must be positive")
    rather than schema-shape problems, which Pydantic already surfaces.
    """

    code: str = "validation_error"


class ConflictError(DomainError):
    """The operation cannot proceed because of the current resource state.

    Typical examples include uniqueness violations, optimistic-lock failures,
    and "the resource is being modified by another request" conditions.
    """

    code: str = "conflict"
