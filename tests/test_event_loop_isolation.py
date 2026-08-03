"""The shared Redis pool's lock must not survive from one event loop to the next.

This reproduces the CI failure of 2026-08-03 deterministically. It never
opens a socket — the whole defect lives in an `asyncio.Lock`, so the pool
is exercised without a Redis server being anywhere near it.

See `conftest.py` for why the lock binds at all, and why it only bites
when two coroutines contend.
"""

from __future__ import annotations

import asyncio

import pytest

from conftest import reset_redis_pool_lock

pytest.importorskip("redis", reason="coordinator requirements not installed")

try:
    from app.redis_client import redis_client
except Exception:  # noqa: BLE001 — mirrors conftest; a local run may lack PYTHONPATH
    pytest.skip("coordinator package not importable (set PYTHONPATH)", allow_module_level=True)


async def _contend(lock: asyncio.Lock) -> None:
    """Force the contended path, which is the only one that binds a loop.

    An uncontended `acquire()` returns on the fast path without touching
    `_get_loop()`, so simply taking the lock proves nothing.
    """

    async def second() -> None:
        async with lock:
            pass

    async with lock:
        waiter = asyncio.ensure_future(second())
        for _ in range(3):
            await asyncio.sleep(0)  # let `second` reach the slow path
    await waiter


def test_a_pool_lock_bound_by_a_dead_loop_is_reset_for_the_next_one():
    pool = redis_client.connection_pool

    # Start from the state conftest guarantees at module setup.
    assert reset_redis_pool_lock() is True
    assert pool._lock._loop is None

    asyncio.run(_contend(pool._lock))
    assert pool._lock._loop is not None, "contention should have bound the lock"

    # This is the CI failure, reproduced: a second loop touching the same
    # lock. `asyncio.run` has already closed the loop that claimed it.
    with pytest.raises(RuntimeError, match="bound to a different event loop"):
        asyncio.run(_contend(pool._lock))

    # And this is the fix.
    assert reset_redis_pool_lock() is True
    assert pool._lock._loop is None
    asyncio.run(_contend(pool._lock))  # no longer raises

    reset_redis_pool_lock()  # leave the pool as we found it


def test_the_reset_mutates_the_shared_client_rather_than_rebinding_a_name():
    """`app.main`, `app.assignment` and `app.metrics` each did
    `from app.redis_client import redis_client`, so they hold the object, not
    the module attribute. A reset that replaced the client would reach none of
    them and would restore the bug silently — so assert it does not."""
    from app import redis_client as module

    client_before = module.redis_client
    pool_before = client_before.connection_pool

    assert reset_redis_pool_lock() is True

    assert module.redis_client is client_before, "the client object was replaced"
    assert module.redis_client.connection_pool is pool_before, "the pool was replaced"
