"""Top-level pytest conftest.

Sets DATABASE_URL to an in-memory SQLite database before any application
imports. ``apply_pilot.db`` reads the env var at module-load time, so we
must set it before the chain
``apply_pilot.features.matches.models -> apply_pilot.db.Base -> engine
instantiation`` runs.
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
