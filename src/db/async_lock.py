"""
Serializes the fire-and-forget background tasks spawned via
asyncio.create_task() throughout src/answer/*, all of which share one
thread-local synchronous SQLite connection (src/db/store.py).

Why this exists: sqlite3.connect(..., timeout=N) makes a lock-contended
write BLOCK for up to N seconds at the C level. Since asyncio is
single-threaded, that block freezes the *entire* event loop -- not just the
task doing the write -- so every other coroutine (including the one
sending the login response) stalls for the same duration. With many
concurrent background tasks all touching the same connection during one
login sequence, this reproduces as an apparently-random, data-dependent
hang with no server-side error, because a contended write that resolves
within the timeout never raises anything.

`guarded()` wraps a background task's body so only one such task is
actually inside a blocking DB call at a time -- serialized cooperatively via
asyncio.Lock (which yields to the event loop while waiting, unlike a
blocked sqlite3 call), instead of piling up as concurrent OS-level blocking
calls that starve the loop.

Usage (inside a handler's fire-and-forget `_do()`):

    from src.db.async_lock import guarded

    async def _do():
        async with guarded():
            ... existing body, including any store.execute()/fetch() calls ...

This does not make SQLite itself faster or allow real concurrent writes
(SQLite still only allows one writer at a time regardless) -- it only
ensures that waiting for that writer never blocks anything else.
"""
import asyncio

_db_task_lock = asyncio.Lock()


def guarded():
    """Async context manager: `async with guarded(): ...`"""
    return _db_task_lock


def create_locked_task(coro):
    """Drop-in replacement for asyncio.create_task(coro) that serializes
    the coroutine's execution against every other task also started this
    way, so their (blocking, synchronous) DB calls can never collide.

    Safe to use as a direct substitute: `asyncio.create_task(_do())` ->
    `create_locked_task(_do())`. The task is still scheduled immediately
    and runs independently (fire-and-forget); it just waits its turn to
    actually execute if another guarded task is currently running.
    """
    async def _wrapped():
        async with _db_task_lock:
            return await coro
    return asyncio.create_task(_wrapped())
