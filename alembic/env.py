from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from job_apply.config import get_settings
from job_apply.db import Base

# Importing the orders models here ensures they are registered on
# ``Base.metadata`` so future autogenerate-based revisions for the
# ``features.orders`` slice pick them up. The M0 baseline migration is
# intentionally schema-empty and does NOT include the ``orders`` table —
# feature tables arrive with their own slices (see PR body / issue #7).
from job_apply.features.orders import models as _orders_models  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Load the DSN from application settings so Alembic honours DB_DSN
# (and the legacy DATABASE_URL alias). When neither is set we keep
# whatever ``alembic.ini`` or the test harness has configured, which
# keeps ``cfg.set_main_option("sqlalchemy.url", ...)`` effective in
# programmatic upgrade/downgrade calls.
if "DB_DSN" in os.environ or "DATABASE_URL" in os.environ:
    _settings = get_settings()
    config.set_main_option("sqlalchemy.url", _settings.database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
