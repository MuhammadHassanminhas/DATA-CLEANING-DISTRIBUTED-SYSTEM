"""Alembic environment.

Runs migrations against the same async engine/URL the app itself uses
(app.config.database_url) so there's exactly one place that knows how
to build the connection string.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import database_url
from app.db import Base
from app.models import Worker  # noqa: F401  — registers the table with Base.metadata

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers defaults to True, which would silently set
    # .disabled on every already-created logger not listed in this ini —
    # including the app's own "coordinator" logger. That's a process-wide,
    # permanent side effect having nothing to do with running a migration.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(database_url())
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
