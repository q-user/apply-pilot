"""admin — integration health view (M6, issue #57).

This vertical slice exposes a read-only ``GET /admin/integrations``
endpoint that returns the current health of every external integration
(hh.ru OAuth, the LLM provider, the database, ...) and a
``POST /admin/integrations/refresh`` endpoint that manually triggers a
one-shot refresh via the :class:`IntegrationStatusWorker`.

A long-running :class:`IntegrationStatusWorker`
(a :class:`~job_apply.runtime.process.BaseProcess` subclass) periodically
runs every :class:`IntegrationChecker` and updates the shared
:class:`InMemoryIntegrationStatusStore`.

Public surface
--------------

* :class:`IntegrationStatus` — the in-process value object.
* :class:`InMemoryIntegrationStatusStore` — the default store.
* :class:`IntegrationStatusWorker` — the long-running
  :class:`BaseProcess` that drives the refresh loop.
* :class:`HhOAuthChecker`, :class:`LlmChecker`, :class:`DatabaseChecker`
  — the three concrete :class:`IntegrationChecker` implementations.
* :data:`router` — FastAPI router (mounted at ``/admin/integrations``).
"""

from __future__ import annotations

from job_apply.features.admin.api import router
from job_apply.features.admin.integrations import (
    DatabaseChecker,
    HhOAuthChecker,
    InMemoryIntegrationStatusStore,
    IntegrationChecker,
    IntegrationStatus,
    IntegrationStatusStore,
    IntegrationStatusWorker,
    LlmChecker,
)

__all__ = [
    "DatabaseChecker",
    "HhOAuthChecker",
    "InMemoryIntegrationStatusStore",
    "IntegrationChecker",
    "IntegrationStatus",
    "IntegrationStatusStore",
    "IntegrationStatusWorker",
    "LlmChecker",
    "router",
]
