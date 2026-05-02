"""Alembic environment.

Pulls the SQLAlchemy URL and the model metadata from the live app config
so migrations always target the same DB the running server uses.
Override the location with ``TBDTASK_DATA_DIR`` (or pass ``-x url=...``).
"""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make the project importable when alembic is invoked from anywhere.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.db import DB_URL, Base  # noqa: E402
from app import models  # noqa: F401, E402  (registers tables)


config = context.config

# Honour the URL from the running app unless overridden via -x url=...
override_url = context.get_x_argument(as_dictionary=True).get("url")
config.set_main_option("sqlalchemy.url", override_url or DB_URL)

if config.config_file_name:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _is_sqlite_url(url: str) -> bool:
    return url.startswith("sqlite")


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # Batch mode rewrites tables and only applies to SQLite. Postgres
        # supports proper ALTER and breaks if batch mode is on.
        render_as_batch=_is_sqlite_url(url),
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
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
