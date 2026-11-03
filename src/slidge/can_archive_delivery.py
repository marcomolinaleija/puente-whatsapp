"""Keep native callback completion behind its own deferred archive deliveries."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from functools import wraps

_pending: ContextVar[list | None] = ContextVar("can_archive_deliveries", default=None)


def track(task):
    pending = _pending.get()
    if pending is not None:
        pending.append(task)


def delivery_scope(method):
    @wraps(method)
    async def wrapped(*args, **kwargs):
        pending = []
        token = _pending.set(pending)
        try:
            return await method(*args, **kwargs)
        finally:
            _pending.reset(token)
            if pending:
                waiter = asyncio.gather(*pending)
                try:
                    await asyncio.shield(waiter)
                except asyncio.CancelledError:
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    raise

    return wrapped
