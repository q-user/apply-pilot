"""Pre-warm the import chain for the ``writing_style_memory`` slice.

The full import chain ``telegram.actions.accept`` → ``matches.service``
→ ``apply_worker.runtime`` → ``telegram.actions.accept`` has a
pre-existing circular import (introduced before this slice landed,
visible in :class:`AcceptActionHandler` loading on top of an
unfinished :mod:`matches.service` module). The cycle resolves when the
project is imported in the same order the full test suite walks it,
but it bites when this directory is collected in isolation
(``pytest tests/features/writing_style_memory/``).

This conftest pre-loads the modules in a benign order so the cycle
resolves before pytest tries to import
:mod:`tests.features.writing_style_memory.test_style_memory_accept_integration`
— which depends on :mod:`telegram.actions.accept`. The pre-warm is a
no-op when the modules are already loaded, so the full suite pays no
extra cost.

A future fix that breaks the cycle at its source (for example, by
lazy-loading :class:`MatchService` inside
:mod:`apply_worker.runtime`) would let us drop this conftest.
"""

from __future__ import annotations

# Pre-warm the modules that close the cross-slice import cycle. The
# order matters: each ``import`` here is a no-op once the module is
# already cached, so the cost is a single dict lookup per module in
# the full-suite run.
import job_apply.features.apply_worker.runtime  # noqa: F401  (closes the cycle)
import job_apply.features.matches.service  # noqa: F401  (cycle participant)
import job_apply.features.sources.models  # noqa: F401  (cycle participant)
import job_apply.features.telegram.actions.accept  # noqa: F401  (cycle participant)
