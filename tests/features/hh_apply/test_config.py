"""HHApplySettings — env-var loading, per-tenant overrides, defaults from contract."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from apply_pilot.features.hh_apply.config import (
    HHApplySettings,
    TenantCredentials,
)


class TestHHApplySettingsDefaults:
    def test_defaults_match_contract_doc_section_5(self) -> None:
        s = HHApplySettings()
        # Header defaults
        assert s.request_delay_ms == 750
        assert s.max_retries == 3
        assert s.backoff_multiplier == 2.0
        assert s.jitter_ms == 200
        assert s.timeout_seconds == 30.0
        assert s.tenant_credentials is None  # OSS default

    def test_user_agent_default_includes_android_signature(self) -> None:
        s = HHApplySettings()
        # User-Agent default propagated from .client.DEFAULT_USER_AGENT; must
        # still contain the Android signature so default-UA never silently
        # degrades to a desktop fingerprint.
        assert "ru.hh.android" in s.user_agent

    def test_xsrf_init_url_default_is_hh_root(self) -> None:
        s = HHApplySettings()
        assert str(s.xsrf_init_url).rstrip("/") == "https://hh.ru"


class TestHHApplySettingsFromEnv:
    def test_env_var_overrides_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HH_APPLY_USER_AGENT", "ru.hh.android/test (Android; 14; TestEnv)")
        monkeypatch.setenv("HH_APPLY_MAX_RETRIES", "5")
        s = HHApplySettings()
        assert "test-env" in s.user_agent.lower() or s.user_agent.endswith("TestEnv)")
        assert s.max_retries == 5


class TestTenantCredentials:
    def test_minimal(self) -> None:
        c = TenantCredentials(resume_id="r1")
        assert c.resume_id == "r1"
        assert c.user_agent is None

    def test_frozen(self) -> None:
        c = TenantCredentials(resume_id="r1")
        with pytest.raises(ValidationError):
            c.resume_id = "r2"  # type: ignore[misc]

    def test_user_agent_override(self) -> None:
        c = TenantCredentials(
            resume_id="r1",
            user_agent="ru.hh.android/2.0 (Android; 14; Tenant)",
        )
        assert c.user_agent is not None
        assert "Tenant" in c.user_agent


class TestHHApplySettingsPerTenant:
    def test_tenant_credentials_dict_round_trip(self) -> None:
        s = HHApplySettings(
            tenant_credentials={
                "tenant-a": TenantCredentials(
                    resume_id="res-a",
                    user_agent="ru.hh.android/1 (Android; 14; TenantA)",
                ),
                "tenant-b": TenantCredentials(resume_id="res-b"),
            },
        )
        assert s.tenant_credentials is not None
        assert set(s.tenant_credentials.keys()) == {"tenant-a", "tenant-b"}
        assert s.tenant_credentials["tenant-a"].resume_id == "res-a"
