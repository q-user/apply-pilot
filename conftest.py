"""Top-level pytest conftest: bootstrap env before any application imports.

``apply_pilot.db`` reads ``DATABASE_URL`` at module-load time; if it is
unset the engine raises RuntimeError and test collection crashes. We
default to an in-memory SQLite so the test bootstrap is self-contained
even on CI runners that don't inject a private ``DATABASE_URL``.
"""

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
