"""FastAPI router for the admin/integrations slice (M6, issue #57).

Two endpoints:

* ``GET /admin/integrations`` — read-only view of the
  :class:`IntegrationStatusStore`. The endpoint is a thin wrapper
  around :meth:`store.get_all` and never blocks on the network.
* ``POST /admin/integrations/refresh`` — runs every registered
  :class:`IntegrationChecker` exactly once and returns the freshly
  refreshed list. Useful for operator-driven ``curl`` checks.

Both endpoints are intentionally unauthenticated for now — the M6
contract is "admin worker + integration status view", and the
authorization story is tracked separately. The router is mounted
under the ``admin`` tag so the OpenAPI spec stays browsable.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from job_apply.features.admin.integrations import (
    InMemoryIntegrationStatusStore,
    IntegrationStatusStore,
    IntegrationStatusWorker,
)
from job_apply.features.admin.schemas import (
    IntegrationStatusRead,
    integration_status_to_read,
)

_LOGGER = logging.getLogger("job_apply.features.admin.api")

router = APIRouter(prefix="/admin/integrations", tags=["admin"])


# ---------------------------------------------------------------------------
# Module-level singletons
# ---------------------------------------------------------------------------
#
# The slice is a single-process MVP. The store and the worker are
# module-level so the API dependency, the long-running worker process,
# and the manual ``POST /refresh`` endpoint all see the same state.
# Tests override the dependencies to inject fakes.
_default_store: InMemoryIntegrationStatusStore = InMemoryIntegrationStatusStore()
_default_worker: IntegrationStatusWorker | None = None


def configure_default_worker(worker: IntegrationStatusWorker) -> None:
    """Register the long-running worker used by ``POST /refresh``.

    Production wiring calls this once at app startup. The dependency
    factory :func:`get_integration_status_worker` returns the
    configured instance, or ``None`` if no worker has been registered
    yet (e.g. during early startup or in tests).
    """
    global _default_worker
    _default_worker = worker
    _LOGGER.info(
        "admin.integrations.worker_configured",
        extra={
            "event": "admin.integrations.worker_configured",
            "checkers": [getattr(c, "name", "?") for c in worker.checkers],
        },
    )


# ---------------------------------------------------------------------------
# Dependency factories
# ---------------------------------------------------------------------------


def get_integration_status_store() -> IntegrationStatusStore:
    """Return the :class:`IntegrationStatusStore` shared with the worker.

    The default is the module-level :data:`_default_store`. Tests
    override this dependency to inject a fresh store.
    """
    return _default_store


def get_integration_status_worker() -> IntegrationStatusWorker | None:
    """Return the configured worker, or ``None`` if not yet registered.

    The ``POST /refresh`` endpoint uses this to trigger a manual
    refresh; the ``GET`` endpoint does not depend on it. Returning
    ``None`` instead of raising keeps the GET endpoint functional
    even before the worker is up.
    """
    return _default_worker


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=list[IntegrationStatusRead],
    responses={
        200: {"description": "Current status of every known integration."},
    },
    summary="List integration health snapshots",
)
def list_integrations(
    store: IntegrationStatusStore = Depends(get_integration_status_store),  # noqa: B008
) -> list[IntegrationStatusRead]:
    """Return the cached :class:`IntegrationStatus` for every integration.

    The endpoint reads the in-process store populated by
    :class:`IntegrationStatusWorker`. When the worker has not run yet
    the response is an empty list — the slice contract deliberately
    avoids inventing fake "unknown" entries so operators can tell at
    a glance whether the worker is alive.
    """
    return [integration_status_to_read(status) for status in store.get_all()]


@router.post(
    "/refresh",
    response_model=list[IntegrationStatusRead],
    responses={
        200: {"description": "Statuses after a one-shot refresh."},
        503: {"description": "No worker is registered; the refresh cannot run."},
    },
    summary="Trigger a one-shot integration refresh",
)
async def refresh_integrations(
    worker: IntegrationStatusWorker | None = Depends(get_integration_status_worker),  # noqa: B008
) -> list[IntegrationStatusRead]:
    """Run every checker once and return the freshest statuses.

    A missing worker is reported as ``503`` so the operator knows
    the refresh did not actually run (rather than silently returning
    the stale cache).
    """
    if worker is None:
        from fastapi import HTTPException
        from fastapi import status as _status

        raise HTTPException(
            status_code=_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "integration_worker_not_configured",
                "message": "no integration status worker is registered for this process",
            },
        )
    results = await worker.run_once()
    return [integration_status_to_read(status) for status in results]


__all__ = [
    "configure_default_worker",
    "get_integration_status_store",
    "get_integration_status_worker",
    "list_integrations",
    "refresh_integrations",
    "router",
]
