from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from job_apply.config import get_database_settings
from job_apply.db import Base
from job_apply.features.apply_worker import (
    models as _apply_worker_models,  # noqa: F401  (register ApplyJob, ApplyStatusHistory, ApplyRateLimitEvent)
)
from job_apply.features.audit import models as _audit_models  # noqa: F401  (register AuditLog)
from job_apply.features.cover_letter import (
    models as _cover_letter_models,  # noqa: F401  (register CoverLetterDraft)
)
from job_apply.features.cover_letter_style import (
    models as _cover_letter_style_models,  # noqa: F401  (register CoverLetterStyle)
)
from job_apply.features.hh import models as _hh_models  # noqa: F401  (register HHCredential)
from job_apply.features.matches import (
    models as _matches_models,  # noqa: F401  (register VacancyMatch)
)
from job_apply.features.orders import models as _orders_models  # noqa: F401  (register Order)
from job_apply.features.resumes import models as _resumes_models  # noqa: F401  (register Resume)
from job_apply.features.scoring import (
    models as _scoring_models,  # noqa: F401  (register PromptVersionRow)
)
from job_apply.features.scoring_ab import (
    models as _scoring_ab_models,  # noqa: F401  (register ScoringExperimentRow, ScoringVariantRow, ScoringExperimentOutcomeRow)
)
from job_apply.features.search_profiles import (
    models as _sp_models,  # noqa: F401  (register SearchProfile)
)
from job_apply.features.source_metrics import (
    models as _source_metrics_models,  # noqa: F401  (register SourceMetricEventORM)
)
from job_apply.features.sources import models as _sources_models  # noqa: F401  (register Vacancy)
from job_apply.features.users import models as _users_models  # noqa: F401  (register User)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

if database_url := os.getenv("DATABASE_URL"):
    config.set_main_option("sqlalchemy.url", database_url)
else:
    # Fall back to DatabaseSettings (env-driven via get_database_settings),
    # and only after that to whatever `alembic.ini` declares.
    config.set_main_option("sqlalchemy.url", get_database_settings().database_url)

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
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
