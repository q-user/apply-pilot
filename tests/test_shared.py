"""Tests for the shared utilities (errors, logging, schemas).

These tests follow the VSA skill's preference for fakes/in-memory implementations
over heavy mocking: the shared utilities are deliberately dependency-free, so
each test only needs the standard library plus ``caplog``.
"""

from __future__ import annotations

import io
import json
import logging

import pytest

from job_apply.shared.errors import (
    ConflictError,
    DomainError,
    NotFoundError,
    ValidationError,
)
from job_apply.shared.logging import configure_logging
from job_apply.shared.schemas import IdentifiedSchema, TimestampedSchema

# ---------- errors ----------


def test_domain_error_str() -> None:
    """``str(DomainError(...))`` should be ``"<code>: <message>"``."""
    err = DomainError("Order 42 not found", code="not_found")

    assert str(err) == "not_found: Order 42 not found"
    # And the base class should remain raise/except-able as a real Exception.
    with pytest.raises(DomainError):
        raise err


def test_not_found_error_subclass() -> None:
    """``NotFoundError`` is a ``DomainError`` subclass with a stable code."""
    err = NotFoundError.for_entity("Order", 42)

    assert isinstance(err, DomainError)
    assert err.code == "not_found"
    assert "Order" in err.message
    assert "42" in err.message


def test_conflict_and_validation_subclass() -> None:
    """``ConflictError`` and ``ValidationError`` carry their own codes."""
    assert issubclass(ConflictError, DomainError)
    assert ConflictError("dup").code == "conflict"
    assert issubclass(ValidationError, DomainError)
    assert ValidationError("bad").code == "validation_error"


# ---------- logging ----------


@pytest.fixture
def restore_root_logger() -> None:  # type: ignore[return-value]
    """Snapshot and restore root logger handlers/level around a test.

    ``configure_logging`` deliberately replaces the root logger's handlers
    so that repeated calls don't stack formatters. Tests that exercise it
    must therefore restore the previous state to avoid leaking into siblings.
    """
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    try:
        yield
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)


def test_configure_logging_is_idempotent(restore_root_logger: None) -> None:
    """Calling ``configure_logging`` repeatedly must not stack handlers."""
    configure_logging(level="INFO", json=False)
    configure_logging(level="INFO", json=False)
    configure_logging(level="INFO", json=False)

    assert len(logging.getLogger().handlers) == 1


def test_configure_logging_respects_level(restore_root_logger: None) -> None:
    """``configure_logging`` should set the requested level on the root logger."""
    configure_logging(level="WARNING", json=False)
    assert logging.getLogger().level == logging.WARNING

    configure_logging(level="DEBUG", json=False)
    assert logging.getLogger().level == logging.DEBUG


def test_configure_logging_json_emits_json(restore_root_logger: None) -> None:
    """When ``json=True`` every record should serialize to a JSON line."""
    buffer = io.StringIO()
    configure_logging(level="INFO", json=True, stream=buffer)

    logging.getLogger("job_apply.shared").info("hello %s", "world")

    payload = json.loads(buffer.getvalue().splitlines()[-1])
    assert payload["message"] == "hello world"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "job_apply.shared"


# ---------- schemas ----------


def test_identified_schema_roundtrip() -> None:
    """``IdentifiedSchema`` should round-trip a positive id through dict()."""
    schema = IdentifiedSchema(id=1)

    payload = schema.model_dump()
    restored = IdentifiedSchema.model_validate(payload)

    assert restored.id == 1
    assert payload == {"id": 1}


def test_timestamped_schema_defaults() -> None:
    """``TimestampedSchema`` should expose ``created_at`` and optional ``updated_at``."""
    schema = TimestampedSchema()

    assert schema.created_at is not None
    assert schema.updated_at is None
