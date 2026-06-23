"""Pre-warm the import chain for the ``max`` test slice.

Mirrors :mod:`tests.features.telegram.conftest` so the MAX tests
collect in isolation without hitting the cross-slice circular import
``messaging.actions.accept`` → ``matches.service`` →
``apply_worker.runtime`` → ``telegram.repository`` → ``telegram.bot``
→ ``messaging.actions.accept``.

The MAX slice participates in the same cycle (its ``MaxBot`` imports
the same action handlers) so the pre-warm is the same set of modules.
A future fix that breaks the cycle at its source (e.g. by lazy-loading
:class:`MatchService` inside :mod:`apply_worker.runtime`) would let
us drop this conftest.
"""

from __future__ import annotations

# Pre-warm the modules that close the cross-slice import cycle. The
# order matches the telegram test conftest: each ``import`` here is a
# no-op once the module is already cached, so the cost is a single
# dict lookup per module in the full-suite run.
import apply_pilot.features.apply_worker.runtime  # noqa: F401  (closes the cycle)
import apply_pilot.features.matches.service  # noqa: F401  (cycle participant)
import apply_pilot.features.messaging.actions.accept  # noqa: F401  (cycle participant)
import apply_pilot.features.sources.models  # noqa: F401  (cycle participant)
