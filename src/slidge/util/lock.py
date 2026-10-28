import asyncio
import logging
from collections.abc import AsyncIterator, Hashable
from contextlib import asynccontextmanager


class NamedLockMixin:
    def __init__(self, *a: object, **k: object) -> None:
        super().__init__(*a, **k)
        self.__locks = dict[Hashable, asyncio.Lock]()

    @asynccontextmanager
    async def lock(self, id_: Hashable) -> AsyncIterator[None]:
        log.trace("getting %s", id_)  # type:ignore
        locks = self.__locks
        if not locks.get(id_):
            locks[id_] = asyncio.Lock()
        try:
            async with locks[id_]:
                log.trace("acquired %s", id_)  # type:ignore
                yield
        finally:
            log.trace("releasing %s", id_)  # type:ignore
            waiters = locks[id_]._waiters
            if not waiters:
                del locks[id_]
                log.trace("erasing %s", id_)  # type:ignore

    def get_lock(self, id_: Hashable) -> asyncio.Lock | None:
        return self.__locks.get(id_)


log = logging.getLogger(__name__)
