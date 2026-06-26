"""hh.ru apply slice — implements apply-once workflow against the Android-emulated /negotiations endpoint.

Source-of-truth contract: docs/integrations/hh_apply.md (M11 T1 #242, merged at fbed762).
No Selenium / playwright / pywebview. No `hh-applicant-tool` dep. Read-only orientation to
/home/mikhail/projects/hh_apply, this slice is fully implemented in apply-pilot.
"""
from .models import (
    ApplyError,
    ApplyRequest,
    ApplyResult,
    ApplyStatus,
    HHApplyError,
)
from .client import HHApplyClient
from .config import HHApplySettings, TenantCredentials
from .service import RetryPolicy, apply_once

__all__ = [
    "apply_once",
    "ApplyRequest",
    "ApplyResult",
    "ApplyError",
    "ApplyStatus",
    "HHApplyError",
    "HHApplyClient",
    "RetryPolicy",
    "HHApplySettings",
    "TenantCredentials",
]
