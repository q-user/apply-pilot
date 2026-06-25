"""Pydantic Settings layer for hh_apply (T3 #244 of M11).

Loads from env vars with prefix HH_APPLY_ (e.g. HH_APPLY_USER_AGENT).
Constructed once at apply_worker startup (T5 #246) and injected into
HHApplyClient + RetryPolicy.

Source-of-truth contract: docs/integrations/hh_apply.md §3 + §5.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

try:  # Pydantic v2 split: BaseSettings is its own package.
    from pydantic_settings import BaseSettings, SettingsConfigDict
except ImportError as _exc:  # pragma: no cover — surfaced in PR body
    raise ImportError(
        'pydantic-settings is required for src/apply_pilot/features/hh_apply/config.py. '
        'Add pydantic-settings>=2.6.0 to pyproject.toml [project.dependencies].'
    ) from _exc

from .client import DEFAULT_BASE_URL, DEFAULT_USER_AGENT


class TenantCredentials(BaseModel):
    """Per-tenant credential isolation hook — fully developed in T6 (#247).

    Holds the per-tenant resume_id (HH.ru resume token) and an optional
    User-Agent override. In OSS single-user mode this is not used (None).
    """

    model_config = ConfigDict(frozen=True)

    resume_id: str
    user_agent: Optional[str] = None  # if None, falls back to HHApplySettings.user_agent


class HHApplySettings(BaseSettings):
    """Settings layer — injected into HHApplyClient + RetryPolicy at T5 startup.

    Environment variable prefix HH_APPLY_. File-based loading from .env
    supported for local dev convenience (uv-runners override at runtime).
    """

    model_config = SettingsConfigDict(
        env_prefix="HH_APPLY_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Network / discrete transport ---
    user_agent: str = Field(
        default=DEFAULT_USER_AGENT,
        description=(
            "Android emulation UA for hh.ru mobile apply flow — see "
            "docs/integrations/hh_apply.md section 3. CONTROLLED at apply_worker startup; "
            "T3 lock-in: T2 finalizes the literal value before PR merge."
        ),
    )
    xsrf_init_url: HttpUrl = Field(
        default=DEFAULT_BASE_URL + "/",
        description="URL the XSRF bootstrap GET hits — must be on a hh.* domain.",
    )
    timeout_seconds: float = Field(default=30.0, ge=1.0, le=600.0)

    # --- RetryPolicy defaults (consumed by T5 to construct RetryPolicy) ---
    request_delay_ms: int = Field(default=750, ge=10, le=60_000)
    max_retries: int = Field(default=3, ge=1, le=20)
    backoff_multiplier: float = Field(default=2.0, ge=1.0, le=10.0)
    jitter_ms: int = Field(default=200, ge=0, le=10_000)

    # --- Multi-tenant hook (T6 #247 fully develops this) ---
    # In OSS single-user mode this is None. In SaaS-multi-tenant mode (post-M11),
    # T6 surfaces a TenantCredentialProvider abstraction; this dict remains the
    # static env-var-driven fallback that the SaaS provider replaces.
    tenant_credentials: Optional[dict[str, TenantCredentials]] = Field(
        default=None,
        description=(
            "Per-tenant credential override map — key is tenant_id, value is "
            "TenantCredentials. None in OSS single-user mode. Read by T5 worker "
            "to construct the per-tenant HHApplyClient + RetryPolicy pair."
        ),
    )
