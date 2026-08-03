"""Suite-wide isolation for objects that outlive a single event loop.

The coordinator builds ONE Redis client at import time
(`coordinator/app/redis_client.py`), so the whole test session shares a
single `ConnectionPool` — and that pool holds an `asyncio.Lock`, taken on
every `get_connection()`.

A deployed coordinator has one event loop for the life of the process, so
this is correct in production and nothing here is a workaround for a
production defect. The suite is the odd case: `test_coordinator_integration.py`
and `test_operator_api.py` each run the app under their own `TestClient`,
and each `TestClient` brings its own event loop.

**Why it fails intermittently rather than every time.** `asyncio.Lock`
binds itself to a loop only when it is *contended* — `Lock.acquire()`
reaches `_get_loop()` on the slow path alone, and an uncontended acquire
returns without ever looking at the loop. Binding therefore requires two
coroutines to want a Redis connection at the same instant, which the
coordinator's background heartbeat sweep supplies at unpredictable
moments. Once bound the binding is permanent: neither
`ConnectionPool.reset()` nor `Redis.aclose()` replaces the lock. The next
loop to contend raises `RuntimeError: ... is bound to a different event
loop`.

Observed in CI on 2026-08-03: `test_a_near_miss_credential_is_rejected_like_a_wild_one`
failed exactly this way (run 30804364008) while the identical commit
passed in a second run (30804327884).

Handing each test module a fresh, unbound lock is the same remedy
`test_operator_api.py` already applies to `assignment._work_available`,
for the same reason.
"""

from __future__ import annotations

import asyncio

import pytest


def reset_redis_pool_lock() -> bool:
    """Give the shared Redis connection pool a fresh, unbound `asyncio.Lock`.

    Returns True if a lock was replaced, False if the coordinator package
    is not importable (a local run without `PYTHONPATH`) or redis-py no
    longer keeps the lock where we expect it.

    The pool is mutated in place rather than a name being rebound on
    purpose: `app.main`, `app.assignment` and `app.metrics` each did
    `from app.redis_client import redis_client`, so all three hold the
    same object and rebinding `app.redis_client.redis_client` would reach
    none of them.
    """
    try:
        from app.redis_client import redis_client
    except Exception:  # noqa: BLE001 — no coordinator on the path is not an error here
        return False

    pool = getattr(redis_client, "connection_pool", None)
    if not isinstance(getattr(pool, "_lock", None), asyncio.Lock):
        # redis-py moved or renamed the lock. Report it rather than
        # silently doing nothing — `test_event_loop_isolation.py` asserts
        # this returns True, so an upgrade that breaks the assumption
        # fails a test instead of quietly restoring the bug.
        return False

    pool._lock = asyncio.Lock()
    return True


@pytest.fixture(scope="module", autouse=True)
def _fresh_redis_pool_lock():
    """Run before every module's own fixtures, so the loop a `TestClient`
    is about to create finds an unbound lock waiting for it."""
    reset_redis_pool_lock()
    yield
