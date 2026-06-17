"""TDD unit tests for AuditService with the in-memory repository.

These tests describe the audit service contract before any HTTP
integration is added. They use the ``InMemoryAuditLogRepository``
so no database is required.
"""

from __future__ import annotations

import uuid

import pytest

from apply_pilot.features.audit.models import AuditEventType
from apply_pilot.features.audit.repository import InMemoryAuditLogRepository
from apply_pilot.features.audit.service import AuditService


@pytest.fixture
def repo() -> InMemoryAuditLogRepository:
    """Fresh in-memory repo per test."""
    return InMemoryAuditLogRepository()


@pytest.fixture
def service(repo: InMemoryAuditLogRepository) -> AuditService:
    """AuditService wired to the in-memory repo."""
    return AuditService(audit_repo=repo)


def test_log_event_inserts_record(service: AuditService, repo: InMemoryAuditLogRepository) -> None:
    """log_event must insert a record into the repository."""
    user_id = uuid.uuid4()
    service.log_event(AuditEventType.REGISTER, user_id=user_id, details={"email": "a@b.com"})

    logs = repo.list_by_user(user_id)
    assert len(logs) == 1
    assert logs[0].event_type == AuditEventType.REGISTER
    assert logs[0].user_id == user_id


def test_log_event_details_survive_roundtrip(
    service: AuditService, repo: InMemoryAuditLogRepository
) -> None:
    """Details dict must be serialised and stored as JSON text."""
    import json

    user_id = uuid.uuid4()
    service.log_event(
        AuditEventType.LOGIN,
        user_id=user_id,
        details={"ip": "127.0.0.1", "user_agent": "pytest"},
    )

    logs = repo.list_by_user(user_id)
    assert len(logs) == 1
    assert logs[0].details is not None
    parsed = json.loads(logs[0].details)
    assert parsed["ip"] == "127.0.0.1"


def test_log_event_anonymous_has_null_user_id(
    service: AuditService, repo: InMemoryAuditLogRepository
) -> None:
    """Omitting user_id must store None (anonymous event)."""
    service.log_event(AuditEventType.LOGIN, details={"reason": "attempt"})

    all_logs = repo.list_recent(10)
    assert len(all_logs) == 1
    assert all_logs[0].user_id is None


def test_list_by_user_filters_correctly(repo: InMemoryAuditLogRepository) -> None:
    """list_by_user must return only logs for the given user."""
    svc = AuditService(audit_repo=repo)
    alice = uuid.uuid4()
    bob = uuid.uuid4()

    svc.log_event(AuditEventType.REGISTER, user_id=alice)
    svc.log_event(AuditEventType.REGISTER, user_id=bob)
    svc.log_event(AuditEventType.LOGIN, user_id=alice)

    assert len(repo.list_by_user(alice)) == 2
    assert len(repo.list_by_user(bob)) == 1


def test_list_by_event_type_filters_correctly(repo: InMemoryAuditLogRepository) -> None:
    """list_by_event_type must return only logs for the given type."""
    svc = AuditService(audit_repo=repo)
    alice = uuid.uuid4()

    svc.log_event(AuditEventType.REGISTER, user_id=alice)
    svc.log_event(AuditEventType.LOGIN, user_id=alice)
    svc.log_event(AuditEventType.LOGIN, user_id=alice)

    assert len(repo.list_by_event_type(AuditEventType.REGISTER)) == 1
    assert len(repo.list_by_event_type(AuditEventType.LOGIN)) == 2
    assert len(repo.list_by_event_type(AuditEventType.RESUME_UPLOAD)) == 0


def test_list_recent_respects_limit(repo: InMemoryAuditLogRepository) -> None:
    """list_recent must return at most ``limit`` entries, newest first."""
    svc = AuditService(audit_repo=repo)
    alice = uuid.uuid4()

    for _i in range(5):
        svc.log_event(AuditEventType.LOGIN, user_id=alice)

    recent = repo.list_recent(3)
    assert len(recent) == 3
    # newest first
    assert recent[0].created_at >= recent[-1].created_at


def test_audit_event_type_enum_has_all_required_values() -> None:
    """Every event type described in the issue spec must exist."""
    assert AuditEventType.REGISTER == "register"
    assert AuditEventType.LOGIN == "login"
    assert AuditEventType.TELEGRAM_LINK == "telegram_link"
    assert AuditEventType.RESUME_UPLOAD == "resume_upload"
    assert AuditEventType.PROFILE_UPDATE == "profile_update"
