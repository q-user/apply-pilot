"""ApplyStatus enum + ApplyRequest (frozen) + ApplyResult + ApplyError + HHApplyError."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from apply_pilot.features.hh_apply.models import (
    ApplyError,
    ApplyRequest,
    ApplyResult,
    ApplyStatus,
    HHApplyError,
)


class TestApplyStatus:
    def test_has_exactly_these_six_values(self) -> None:
        assert {s.value for s in ApplyStatus} == {
            "success",
            "idle_already_applied",
            "validation_error",
            "auth_required",
            "rate_limited",
            "upstream_error",
        }


class TestApplyRequest:
    def test_basic_construction(self) -> None:
        r = ApplyRequest(vacancy_id="v1", resume_id="r1", message="hello")
        assert r.vacancy_id == "v1"
        assert r.resume_id == "r1"
        assert r.message == "hello"
        assert r.lux is False
        assert r.force is False

    def test_frozen_per_pydantic_v2_config(self) -> None:
        r = ApplyRequest(vacancy_id="v1", resume_id="r1", message="hello")
        with pytest.raises(ValidationError):
            r.vacancy_id = "v2"  # type: ignore[misc]

    def test_lux_and_force_can_be_set(self) -> None:
        r = ApplyRequest(
            vacancy_id="v1", resume_id="r1", message="hello",
            lux=True, force=True,
        )
        assert r.lux is True
        assert r.force is True


class TestApplyHarvestResultFields:
    def test_minimal_apply_result(self) -> None:
        r = ApplyResult(status=ApplyStatus.success, http_status=201)
        assert r.status == ApplyStatus.success
        assert r.http_status == 201
        assert r.attempt_count == 1
        assert r.negotiation_id is None
        assert r.error is None


class TestApplyError:
    def test_construction_carries_required_fields(self) -> None:
        e = ApplyError(code="validation_error", message="bad payload", http_status=400)
        assert e.code == "validation_error"
        assert e.message == "bad payload"
        assert e.http_status == 400


class TestHHApplyError:
    def test_subclass_of_exception(self) -> None:
        assert isinstance(HHApplyError("x"), Exception)
