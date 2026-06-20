"""Pre-warm the import chain for the ``max`` test slice.

The full import chain ``messaging.actions.accept`` → ``matches.service``
→ ``apply_worker.runtime`` → ``telegram.repository`` → ``telegram.bot``
→ ``messaging.actions.accept`` has a pre-existing circular import
(introduced before this slice landed, visible in
:class:`AcceptActionHandler` loading on top of an unfinished
:mod:`matches.service` module). The cycle resolves when the project is
imported in the same order the full test suite walks it, but it bites
when this directory is collected in isolation
(``pytest tests/features/max/``).

This conftest pre-loads the modules in a benign order so the cycle
resolves before pytest tries to import the action-handler test
modules. The pre-warm is a no-op when the modules are already loaded,
so the full suite pays no extra cost.

Mirrors ``tests/features/telegram/conftest.py`` because both slices
hit the same cycle.
"""

from __future__ import annotations

# Pre-warm the modules that close the cross-slice import cycle. The
# order matters: each ``import`` here is a no-op once the module is
# already cached, so the cost is a single dict lookup per module in
# the full-suite run.
import apply_pilot.features.apply_worker.runtime  # noqa: F401  (closes the cycle)
import apply_pilot.features.matches.service  # noqa: F401  (cycle participant)
import apply_pilot.features.messaging.actions.accept  # noqa: F401  (cycle participant)
import apply_pilot.features.sources.models  # noqa: F401  (cycle participant)
