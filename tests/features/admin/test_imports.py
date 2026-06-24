"""Boot-path regression test for issue #225.

Issue #225: ``uvicorn apply_pilot.app:create_app --factory`` (and any
``create_app()`` call) failed with::

    ImportError: cannot import name 'LLMClient' from partially
    initialized module 'apply_pilot.features.scoring.llm'

Root cause: :mod:`apply_pilot.features.admin.integrations` had a
top-level ``from apply_pilot.features.scoring.llm import HttpLLMClient``.
That eagerly loaded ``scoring.llm`` from the admin slice, which
re-triggers the chain

    scoring.llm → scoring.prompts → sources.models →
    apply_worker.runtime → apply_worker.service →
    apply_worker.notifications → telegram.repository → telegram.bot →
    messaging.actions.accept → messaging.actions.regenerate →
    cover_letter.service (top: from scoring.llm import LLMClient)

deadlock at startup. The fix is to make the ``HttpLLMClient`` import
lazy — it is only used as a type annotation on
``LlmChecker.__init__`` and ``from __future__ import annotations``
turns that into a string, so no runtime import is required.

These tests lock the boot path in two ways:

1. ``test_create_app_boots_cleanly`` — instantiates the FastAPI app via
   :func:`apply_pilot.app.create_app` (no DB / network), the exact
   boot sequence uvicorn performs.
2. ``test_integrations_module_does_not_eagerly_import_scoring_llm`` —
   asserts ``HttpLLMClient`` is **not** bound on
   :mod:`apply_pilot.features.admin.integrations` after the module is
   loaded, so a future regression that re-introduces the top-level
   import fails this test before it ever reaches ``create_app``.
"""

from __future__ import annotations

import importlib
import sys

import pytest
from fastapi import FastAPI


def test_create_app_boots_cleanly() -> None:
    """The factory entry point must construct without raising.

    Reproduces the exact boot path that uvicorn executes with
    ``--factory`` (and that ``docker compose up apply-pilot-api``
    exercises). On the broken main branch (issue #225) this raises
    ``ImportError: cannot import name 'LLMClient' from partially
    initialized module 'apply_pilot.features.scoring.llm'``.
    """
    # Import the factory lazily so the assertion is on the same code
    # path uvicorn uses (``from apply_pilot.app import create_app``).
    from apply_pilot.app import create_app

    app: FastAPI = create_app()
    assert isinstance(app, FastAPI)
    # Sanity: at least the admin router got wired in. The simplest
    # portable check is to ask the schema for an admin path — this
    # works regardless of how the routers are mounted (the routes
    # list contains _IncludedRouter objects without ``.path``/``.prefix``).
    schema_paths = set(app.openapi().get("paths", {}).keys())
    assert any(p.startswith("/admin") for p in schema_paths), (
        f"admin router did not get mounted; schema paths={sorted(schema_paths)[:20]}"
    )


def test_integrations_module_does_not_eagerly_import_scoring_llm() -> None:
    """``HttpLLMClient`` must NOT be a module-level binding.

    The import was the cycle's entry point. If a future change re-adds
    a top-level ``from apply_pilot.features.scoring.llm import
    HttpLLMClient`` (or ``import apply_pilot.features.scoring.llm``)
    this test fails before anyone has to reproduce the uvicorn
    ``ImportError``.
    """
    # Force a fresh import so we observe the module as it loads now,
    # not whatever the rest of the test session may have cached.
    sys.modules.pop("apply_pilot.features.admin.integrations", None)
    module = importlib.import_module("apply_pilot.features.admin.integrations")

    assert not hasattr(module, "HttpLLMClient"), (
        "admin.integrations must not bind HttpLLMClient at module "
        "scope — it re-introduces the cover_letter ↔ scoring.llm "
        "cycle that blocks uvicorn --factory (issue #225). Move the "
        "import inside the function that needs it, or under "
        "TYPE_CHECKING if the symbol is annotation-only."
    )


def test_llm_checker_class_is_still_importable() -> None:
    """The fix must not regress the public surface.

    :class:`apply_pilot.features.admin.integrations.LlmChecker` is part
    of ``apply_pilot.features.admin.__all__`` and used directly by
    tests and the integration worker wiring.
    """
    from apply_pilot.features.admin.integrations import LlmChecker

    assert LlmChecker.__init__ is not None
    # The annotation on __init__ must still mention HttpLLMClient (it
    # is a forward-reference string under ``from __future__ import
    # annotations``); this is what makes the lazy import safe.
    annotations = LlmChecker.__init__.__annotations__
    assert "client" in annotations
    assert "HttpLLMClient" in annotations["client"]


@pytest.mark.parametrize(
    "name",
    ["LlmChecker", "DatabaseChecker", "IntegrationStatusWorker"],
)
def test_admin_integration_classes_remain_public(name: str) -> None:
    """Sanity check: the lazy import did not break sibling exports."""
    module = importlib.import_module("apply_pilot.features.admin.integrations")
    assert hasattr(module, name)
