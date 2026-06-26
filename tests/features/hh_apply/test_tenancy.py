"""EnvTenantCredentialProvider — OSS default + per-tenant resolution. MultiTenantProvider stub."""
from __future__ import annotations

import pytest

from apply_pilot.features.hh_apply.config import HHApplySettings, TenantCredentials
from apply_pilot.features.hh_apply.tenancy import (
    EnvTenantCredentialProvider,
    MultiTenantProvider,
)


class TestEnvTenantCredentialProviderOSS:
    def test_oss_default_returns_single_resolution(self) -> None:
        s = HHApplySettings()  # tenant_credentials=None by default
        provider = EnvTenantCredentialProvider(s)
        r = provider.resolve(tenant_id=None)
        assert r.tenant_id is None
        assert r.credentials is None
        assert r.resume_id == "oss-default-resume"
        # Client created with default UA, retry policy populated from settings.
        assert "ru.hh.android" in r.client.headers["User-Agent"]
        assert r.retry_policy.max_retries == s.max_retries


class TestEnvTenantCredentialProviderDict:
    def test_oss_default_with_default_tenant_key(self) -> None:
        s = HHApplySettings(
            tenant_credentials={
                "__default__": TenantCredentials(resume_id="res-default"),
            },
        )
        provider = EnvTenantCredentialProvider(s)
        r = provider.resolve(tenant_id=None)
        assert r.tenant_id is None
        assert r.resume_id == "res-default"
        assert r.credentials is not None

    def test_per_tenant_lookup(self) -> None:
        s = HHApplySettings(
            tenant_credentials={
                "tenant-a": TenantCredentials(
                    resume_id="res-a",
                    user_agent="ru.hh.android/1 (Android; 14; TenantA)",
                ),
            },
        )
        provider = EnvTenantCredentialProvider(s)
        r = provider.resolve(tenant_id="tenant-a")
        assert r.tenant_id == "tenant-a"
        assert r.resume_id == "res-a"
        assert r.client.headers["User-Agent"].endswith("TenantA)")

    def test_missing_tenant_raises_value_error(self) -> None:
        s = HHApplySettings(
            tenant_credentials={
                "tenant-a": TenantCredentials(resume_id="res-a"),
            },
        )
        provider = EnvTenantCredentialProvider(s)
        with pytest.raises(ValueError, match="tenant-b"):
            provider.resolve(tenant_id="tenant-b")


class TestMultiTenantProviderStub:
    def test_resolve_raises_not_implemented(self) -> None:
        s = HHApplySettings()
        provider = MultiTenantProvider()
        with pytest.raises(NotImplementedError, match="MultiTenantProvider"):
            provider.resolve(tenant_id="anything", settings=s)

    def test_error_message_mentions_saas_epic(self) -> None:
        s = HHApplySettings()
        provider = MultiTenantProvider()
        with pytest.raises(NotImplementedError) as exc_info:
            provider.resolve(tenant_id=None, settings=s)
        assert "SaaS epic" in str(exc_info.value)
