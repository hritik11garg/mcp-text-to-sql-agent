"""Alembic environment.

Autogenerate is deliberately not used. Several migrations here are GRANT and
REVOKE statements, which Alembic cannot infer from model metadata -- and the
security posture must be reproducible from a clean database, so it belongs in
migrations rather than in a README someone runs by hand.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# No declarative models: the schema is written as explicit SQL.
target_metadata = None


def _database_url() -> str:
    """Read the URL from the environment. Fail fast if it is absent."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Migrations run as the owner role, not the "
            "read-only role -- see docs/architecture/DATABASE.md section 7."
        )
    # psycopg3 is the driver; accept a bare postgresql:// URL for convenience.
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()

    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
