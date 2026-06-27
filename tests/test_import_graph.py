"""Static import-graph guard against slice import cycles (issue #221).

This module locks down the import relationships between four feature
slices that historically formed a circular import:

* ``apply_pilot.features.apply_worker.runtime`` — the per-iteration
  loop body that flips a match to ``applied`` once a submission
  succeeds. Uses :class:`MatchService` lazily inside
  :meth:`ApplyWorker._handle_success`.
* ``apply_pilot.features.matches.service`` — owns :class:`MatchService`,
  which is injected into :class:`~apply_pilot.features.apply_worker.runtime.ApplyWorker`.
* ``apply_pilot.features.messaging.actions.accept`` — the ``/accept``
  command handler, which imports :class:`MatchService` at module level
  to mark a match accepted and then enqueues an :class:`ApplyJob` for
  the worker.
* ``apply_pilot.features.max.process`` — the MAX bot polling loop,
  whose console-script entry point wires the ``/accept`` handler.

Why this guard exists
---------------------

PR #229 fixed the ``apply_worker`` ↔ ``matches`` import cycle by
moving ``from apply_pilot.features.matches.service import MatchService``
from the top of :mod:`apply_pilot.features.apply_worker.runtime` into
:meth:`ApplyWorker._handle_success`. Without a guard, a future refactor
that re-adds a top-level import would silently regress the cycle: the
import happens to resolve through a different path, so static analysis
stays silent, and the failure only surfaces at the first
``apply-pilot-max-bot`` container boot.

Cycles covered
--------------

1. ``apply_worker.runtime`` ↔ ``matches.service``. PR #229 made the
   runtime side lazy; PR #287 completed the fix by moving the
   ``messaging.actions.*`` handler imports to text-anchored lazy
   imports inside ``handle()``, fully breaking the cycle. This test
   asserts :class:`MatchService` is **not** present at module level in
   the runtime (see
   :func:`test_apply_worker_runtime_does_not_eagerly_import_match_service`).

2. ``messaging.actions.accept`` ↔ ``max.process`` — the accept handler
   transitively pulls in the messaging DTO/protocols used by the MAX
   bot. The reverse direction (max importing accept) is the real boot
   path of the ``apply-pilot-max-bot`` console script. Both directions
   are exercised in :func:`test_pair_imports_in_both_orders`.

Lazy-import contract locked
---------------------------

The contract from PR #229 is:

* :class:`MatchService` is reachable from
  ``apply_pilot.features.matches.service`` (the canonical place);
* :class:`MatchService` is **not** present as a module-level attribute
  of ``apply_pilot.features.apply_worker.runtime`` — it is only
  resolved at call time, after the rest of the import graph has
  settled.

Generic ``import`` smoke tests will not catch a regression that
re-introduces a top-level import of a symbol that happens to be
resolvable through a *different* import path; the dedicated
``test_apply_worker_runtime_does_not_eagerly_import_match_service``
test exists for that reason.

Cycle resolved (PR #287)
------------------------

PR #287 broke the ``apply_worker.runtime`` ↔ ``matches.service`` cycle
by moving the ``messaging.actions.*`` handler imports of
``MatchNotFoundError`` / ``MatchOwnershipError`` into text-anchored
lazy imports inside each ``handle()`` method. Both import orders now
succeed from a fresh interpreter, and
:func:`test_pair_imports_in_both_orders` exercises both directions
without special-casing.

The lazy-import contract
(:func:`test_apply_worker_runtime_does_not_eagerly_import_match_service`)
remains in force: :class:`MatchService` must not appear as a
module-level attribute of ``apply_worker.runtime``.

Extending the guard
-------------------

If a new cycle appears (e.g. between ``matches`` and a new feature):

1. Add the two module names to :data:`_CYCLE_PAIRS` in both orders;
   the reverse-order entry will surface a real cycle as a test
   failure.
2. If the new cycle relies on a lazy import in one of the slices, add
   a dedicated ``test_<slice>_does_not_eagerly_import_<symbol>``
   assertion modelled on the existing one — generic ``import`` smoke
   tests will not catch a regression that re-introduces a top-level
   import of a symbol that happens to be resolvable through a
   *different* path.
3. Update this docstring with the new cycle so the next contributor
   finds the rationale here.
"""

from __future__ import annotations

import importlib
import sys

import pytest

# ---------------------------------------------------------------------------
# Module under test
# ---------------------------------------------------------------------------


def _purge(*module_names: str) -> None:
    """Remove ``module_names`` and every descendant from ``sys.modules``.

    The import system caches modules in ``sys.modules``; a previously
    imported module stays importable even if the source file on disk
    changes. The tests in this module need each import to walk the
    real import graph from scratch, so we drop every entry whose name
    starts with one of the requested prefixes before importing.
    """
    exact: set[str] = set(module_names)
    prefixes: tuple[str, ...] = tuple(f"{name}." for name in module_names)
    # ``sys.modules`` may be mutated while we iterate; collect first.
    to_drop: list[str] = [
        cached
        for cached in list(sys.modules)
        if cached in exact or any(cached.startswith(prefix) for prefix in prefixes)
    ]
    for cached in to_drop:
        del sys.modules[cached]


# Entry points that must import cleanly from a fresh ``sys.modules``.
# These are the working boot paths in production: the
# ``apply-pilot-max-bot`` console script loads :mod:`max.process`, the
# messaging dispatcher loads the accept handler, and the worker
# process loads :mod:`apply_worker.runtime` directly.
#
# ``matches.service`` is covered by the pair test below rather than
# this smoke list; both import orders now succeed after #287 broke
# the cycle.
_SMOKE_MODULES: tuple[str, ...] = (
    "apply_pilot.features.max.process",
    "apply_pilot.features.messaging.actions.accept",
    "apply_pilot.features.apply_worker.runtime",
)


# Pairs of modules whose import must not raise in either order.
# PR #287 broke the ``matches.service`` ↔ ``apply_worker.runtime``
# cycle, so both directions now succeed from a fresh interpreter.
_CYCLE_PAIRS: tuple[tuple[str, str], ...] = (
    (
        "apply_pilot.features.messaging.actions.accept",
        "apply_pilot.features.max.process",
    ),
    (
        "apply_pilot.features.apply_worker.runtime",
        "apply_pilot.features.matches.service",
    ),
)


# ---------------------------------------------------------------------------
# Smoke imports
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module_name", _SMOKE_MODULES)
def test_smoke_imports_clean(module_name: str) -> None:
    """Each cycle entry point imports cleanly from a fresh ``sys.modules``."""
    _purge("apply_pilot")
    importlib.import_module(module_name)


# ---------------------------------------------------------------------------
# Pair imports in both orders
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("first", "second"),
    [
        pytest.param(
            pair[0],
            pair[1],
            id=f"{pair[0].rsplit('.', 1)[-1]}-then-{pair[1].rsplit('.', 1)[-1]}",
        )
        for pair in _CYCLE_PAIRS
    ]
    + [
        pytest.param(
            pair[1],
            pair[0],
            id=f"{pair[1].rsplit('.', 1)[-1]}-then-{pair[0].rsplit('.', 1)[-1]}",
        )
        for pair in _CYCLE_PAIRS
    ],
)
def test_pair_imports_in_both_orders(first: str, second: str) -> None:
    """Importing each cycle pair in either order must not raise.

    PR #287 broke the ``apply_worker.runtime`` ↔ ``matches.service``
    cycle by moving the ``messaging.actions.*`` handler imports to
    text-anchored lazy imports inside each ``handle()`` method. Both
    directions now import cleanly from a fresh interpreter.
    """
    _purge("apply_pilot")
    importlib.import_module(first)
    importlib.import_module(second)


# ---------------------------------------------------------------------------
# Lazy-import contract
# ---------------------------------------------------------------------------


def test_apply_worker_runtime_does_not_eagerly_import_match_service() -> None:
    """``MatchService`` is a lazy import in ``apply_worker.runtime``.

    Locks the contract from PR #229: if a future change re-adds a
    top-level ``from apply_pilot.features.matches.service import
    MatchService`` to :mod:`apply_pilot.features.apply_worker.runtime`,
    the import will succeed (because ``matches.service`` is otherwise
    importable on its own) but this test will fail.
    """
    _purge("apply_pilot")
    rt = importlib.import_module("apply_pilot.features.apply_worker.runtime")
    assert "MatchService" not in rt.__dict__, (
        "apply_pilot.features.apply_worker.runtime must not import "
        "MatchService at module level — the apply_worker ↔ matches "
        "cycle (see PR #229) only stays broken while the import is "
        "lazy. Move the import inside the method that needs it "
        "(e.g. ApplyWorker._handle_success)."
    )


def test_match_service_available_via_matches_service() -> None:
    """``MatchService`` is reachable from the canonical ``matches.service``.

    The lazy-import contract above is meaningless if the symbol is
    not reachable at all. ``matches.service`` is imported directly —
    PR #287 broke the cross-slice cycle, so no pre-warm is needed.
    """
    from apply_pilot.features.matches.service import MatchService

    assert isinstance(MatchService, type)
    # The class is the one the rest of the slice expects; a future
    # rename would break the apply worker as well, so guard the
    # contract here.
    assert MatchService.__module__ == "apply_pilot.features.matches.service"


# ---------------------------------------------------------------------------
# ``_purge`` behaviour
# ---------------------------------------------------------------------------


def test_purge_removes_named_modules() -> None:
    """``_purge`` actually removes the requested modules from ``sys.modules``."""
    importlib.import_module("apply_pilot.features.apply_worker.runtime")
    assert "apply_pilot.features.apply_worker.runtime" in sys.modules
    _purge(
        "apply_pilot.features.apply_worker.runtime",
        "apply_pilot.features.matches.service",
    )
    assert "apply_pilot.features.apply_worker.runtime" not in sys.modules
    assert "apply_pilot.features.matches.service" not in sys.modules


def test_purge_removes_children() -> None:
    """``_purge`` removes descendants of the named packages too."""
    importlib.import_module("apply_pilot.features.apply_worker.runtime")
    child = "apply_pilot.features.apply_worker.models"
    assert child in sys.modules
    _purge("apply_pilot")
    survivors = [
        name for name in list(sys.modules) if name == child or name.startswith(f"{child}.")
    ]
    assert not survivors
