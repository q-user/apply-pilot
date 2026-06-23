"""Pre-warm the import chain for the ``scoring_ab`` test slice.

The full import chain ``messaging.actions.accept`` → ``matches.service``
→ ``apply_worker.runtime`` → ``telegram.repository`` → ``telegram.bot``
→ ``messaging.actions.accept`` has a pre-existing circular import
(introduced before this slice landed). The cycle resolves when the
project is imported in the same order the full test suite walks it, but
it bites when this directory is collected in isolation
(``pytest tests/features/scoring_ab/``).

The historical pre-warm was provided indirectly: the admin router used
to import :mod:`apply_pilot.features.hh.oauth`, which in turn
re-exported :mod:`apply_pilot.features.hh.apply`, which pre-loaded
:mod:`apply_pilot.features.apply_worker.models`. M10 (issue #204)
removed the HH OAuth / apply path, so the indirect pre-warm is gone
and we have to do it explicitly here.

This conftest loads the modules in a benign order so the cycle
resolves before pytest tries to import the action-handler test
modules. The pre-warm is a no-op when the modules are already loaded,
so the full suite pays no extra cost.

A future fix that breaks the cycle at its source (for example, by
lazy-loading :class:`MatchService` inside
:mod:`apply_worker.runtime`) would let us drop this conftest.
"""

from __future__ import annotations

# Pre-warm the modules that close the cross-slice import cycle. The
# order matches the other test conftests: each ``import`` here is a
# no-op once the module is already cached, so the cost is a single
# dict lookup per module in the full-suite run.
import apply_pilot.features.apply_worker.models  # noqa: F401  (pre-warm removed by M10)
import apply_pilot.features.apply_worker.runtime  # noqa: F401  (closes the cycle)
import apply_pilot.features.matches.service  # noqa: F401  (cycle participant)
import apply_pilot.features.messaging.actions.accept  # noqa: F401  (cycle participant)
import apply_pilot.features.sources.models  # noqa: F401  (cycle participant)
