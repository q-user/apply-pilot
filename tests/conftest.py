"""Root conftest — populate DATABASE_URL before any nested conftest pre-warms imports.

Several nested conftest.py files under tests/features/* import apply_pilot.config
or apply_pilot.db at module level to break circular-import edges. Those imports
fire get_database_settings(), which is now strict (raises RuntimeError on unset
DATABASE_URL). Setting it here, before pytest collects any test module, lets
test collection succeed without re-introducing a silent SQLite fallback in
production code.

`setdefault` (not `os.environ[...] = ...`) so explicit overrides from CI / CLI
keep winning. Default value is `:memory:` SQLite, which is fast and isolated
per pytest-xdist worker (workers fork the parent process and inherit env).
The driver prefix `sqlite+pysqlite` matches .env.example and Alembic.
"""

import os

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
