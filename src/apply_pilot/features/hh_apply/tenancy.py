"""hh_apply tenancy — per-tenant credential isolation + RetryPolicy hook (T6 #247).

SaaS-multi-tenant is **out of scope for M11** (separately tracked as the
post-M11 SaaS epic). This module exposes:
  - TenantCredentialProvider Protocol
  - EnvTenantCredentialProvider — read-only env-driven resolution (OSS default)
  - MultiTenantProvider — stub; raises NotImplementedError to enforce boundary

Source-of-truth contract: docs/integrations/hh_apply.md section 6 + T6 #247.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from .client import HHApplyClient
from .config import HHApplySettings, TenantCredentials
from .service import RetryPolicy


class TenantResolution(BaseModel):
    """Per-tenant resolution result. `arbitrary_types_allowed` because RetryPolicy is
    a frozen dataclass and not a Pydantic BaseModel."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    tenant_id: str | None = None
    credentials: TenantCredentials | None = None
    resume_id: str = "oss-default"
    client: HHApplyClient  # ready-to-use; UA configured
    retry_policy: RetryPolicy


class TenantCredentialProvider:
    """Protocol surface (duck-typed). T5 (#246) calls
    `provider.resolve(tenant_id) -> TenantResolution` once per dispatch.

    Implementations MUST be idempotent and side-effect-free on `resolve` —
    they only READ the settings captured at construction time to materialize
    a TenantResolution.
    """

    def resolve(self, tenant_id: str | None) -> TenantResolution:  # pragma: no cover
        raise NotImplementedError


class EnvTenantCredentialProvider(TenantCredentialProvider):
    """Default provider — OSS single-user mode + env-driven multi-tenant fallback.

    Resolution rules (governed by `settings.tenant_credentials`):
      - `tenant_credentials is None` → returns OSS single-user resolution keyed off
        `settings.user_agent` with placeholder `resume_id` '`oss-default-resume`'.
      - `tenant_credentials is set` AND `tenant_id is None` → looks up sentinel
        `DEFAULT_TENANT_KEY` ('`__default__`'); if missing, falls back to OSS.
      - `tenant_credentials is set` AND `tenant_id is provided` → looks up matching
        TenantCredentials. Raises ValueError on missing tenant (forces operator
        misconfiguration to surface immediately rather than silently de-multi-tenant).
    """

    DEFAULT_TENANT_KEY: ClassVar[str] = "__default__"

    def __init__(self, settings: HHApplySettings) -> None:
        self._settings = settings

    def resolve(self, tenant_id: str | None) -> TenantResolution:
        settings = self._settings
        ua: str = settings.user_agent
        credentials: TenantCredentials | None = None
        resume_id: str = "oss-default-resume"

        creds_map = settings.tenant_credentials
        if creds_map is not None:
            if tenant_id is None:
                credentials = creds_map.get(self.DEFAULT_TENANT_KEY)
                if credentials is not None:
                    resume_id = credentials.resume_id
                    if credentials.user_agent is not None:
                        ua = credentials.user_agent
            else:
                credentials = creds_map.get(tenant_id)
                if credentials is None:
                    raise ValueError(
                        f"Tenant {tenant_id!r} not present in HHApplySettings.tenant_credentials. "
                        f"Configured tenants: {sorted(creds_map.keys())}"
                    )
                resume_id = credentials.resume_id
                if credentials.user_agent is not None:
                    ua = credentials.user_agent

        client = HHApplyClient(user_agent=ua)
        retry_policy = RetryPolicy(
            max_retries=settings.max_retries,
            request_delay_ms=settings.request_delay_ms,
            backoff_multiplier=settings.backoff_multiplier,
            jitter_ms=settings.jitter_ms,
        )
        return TenantResolution(
            tenant_id=tenant_id,
            credentials=credentials,
            resume_id=resume_id,
            client=client,
            retry_policy=retry_policy,
        )


class MultiTenantProvider:
    """SaaS-multi-tenant provider stub — raises NotImplementedError.

    Per T6 #247 acceptance: explicit guardrail, NOT silent fallback to single-tenant.
    Implementation belongs to the post-M11 SaaS epic (billing + quota + tenant-secret-vault).
    """

    def resolve(self, tenant_id: str | None, settings: HHApplySettings) -> TenantResolution:
        raise NotImplementedError(
            "MultiTenantProvider is reserved for the post-M11 SaaS epic. "
            "For OSS single-user mode or env-driven multi-tenant, use "
            "EnvTenantCredentialProvider. SaaS-billing + tenant-secret-vault go in a "
            "separate epic and MAY NOT silently fall back to single-tenant mode."
        )
