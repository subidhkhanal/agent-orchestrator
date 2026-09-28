"""Alembic environment. Reads DATABASE_URL; migrations are plain SQL (no ORM models)."""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from orchestrator.config import Settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

if os.name == "nt" and (libpq_dir := os.environ.get("LIBPQ_DIR")):
    os.environ["PATH"] = libpq_dir + os.pathsep + os.environ["PATH"]


def database_url() -> str:
    url = os.environ.get("DATABASE_URL") or Settings().database_url
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    # SQLAlchemy needs the driver named explicitly to use psycopg 3.
    return url.replace("postgresql://", "postgresql+psycopg://", 1)


def run_migrations_offline() -> None:
    context.configure(url=database_url(), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
