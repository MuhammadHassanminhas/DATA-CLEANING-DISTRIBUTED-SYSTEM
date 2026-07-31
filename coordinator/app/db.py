"""Async SQLAlchemy engine and session factory.

No authoritative state is held in coordinator process memory (CLAUDE.md
architectural invariant #9) — everything durable lives in Postgres,
reachable identically from any coordinator replica.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import database_url, db_max_overflow, db_pool_size

# Phase 2.7. The pool size is explicit because the default was **measured
# to be the bottleneck**, not because a bigger number felt safer.
#
# With 100 workers connected and a 1,000-task batch draining, an operator
# page took **0.83s to 48.8s** through `GET /tasks` while the SQL behind it
# ran in **0.198 ms** — a five-order-of-magnitude gap. `pg_stat_activity`
# showed the coordinator holding exactly **15** connections, which is
# SQLAlchemy's default `pool_size=5` plus `max_overflow=10`. Every worker
# message that writes (`task_started`, `task_result`) takes a session, so a
# busy fleet holds the pool and an operator read waits behind it.
#
# Sized against Postgres's connection budget rather than picked: this is
# per **replica**, and staging runs three, so the ceiling below is
# `3 × (15 + 5) = 60` against a default `max_connections` of 100 — leaving
# room for migrations, psql and the observability scrapes. A deployment
# with more replicas must raise `max_connections` or lower these, which is
# why both are environment variables rather than constants.
engine = create_async_engine(
    database_url(),
    pool_pre_ping=True,
    pool_size=db_pool_size(),
    max_overflow=db_max_overflow(),
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session
