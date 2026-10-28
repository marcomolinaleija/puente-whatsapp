"""Exercise the installed Slidge history-on-join implementation without network or real data."""
from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

from slidge.group.archive import MessageArchive
from slidge.group.room import LegacyMUC
from slixmpp import JID, Message
from slixmpp.plugins.xep_0203.stanza import Delay
from slixmpp.xmlstream import register_stanza_plugin
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import QueuePool

register_stanza_plugin(Message, Delay)


async def check_database_lifetime(deliver, jid):
    # Real QueuePool + ORM sessions + installed archive generator, isolated from
    # production storage. A generator left open across a yield used to exhaust
    # the 5+10 pool when more than fifteen rooms joined concurrently.
    engine = create_engine("sqlite://", poolclass=QueuePool, pool_size=5,
                           max_overflow=10, pool_timeout=0.001)
    sent = 0
    peak = 0

    class Store:
        @contextmanager
        def session(self):
            nonlocal peak
            with Session(engine) as session:
                session.execute(text("SELECT 1"))
                peak = max(peak, engine.pool.checkedout())
                yield session

        def get_messages(self, session, room_id, **kwargs):
            for _ in range(kwargs["last_page_n"]):
                yield SimpleNamespace(stanza_component_ns=Message(), when=datetime.now(UTC))

    def send(message, unused):
        nonlocal sent
        assert engine.pool.checkedout() == 0, "join history retained its database connection"
        sent += 1

    try:
        rooms = []
        for index in range(20):
            room = SimpleNamespace(id=index, jid=JID(f"room{index}@example.invalid"),
                                   xmpp=SimpleNamespace(send=send))
            room.archive = MessageArchive(room, SimpleNamespace(rooms=None, mam=Store()))
            rooms.append(room)
        await asyncio.gather(*(deliver(room, jid) for room in rooms))
        assert sent == 1000 and peak == 1 and engine.pool.checkedout() == 0
        return {"concurrent_rooms": 20, "messages": sent, "peak_checkouts": peak,
                "checkouts_during_send": 0, "remaining_checkouts": 0}
    finally:
        engine.dispose()


async def run():
    delivered, heartbeat = [], []

    def get_all(**kwargs):
        for _ in range(min(kwargs["last_page_n"], 100)):
            yield SimpleNamespace(stanza_component_ns=Message(), when=datetime.now(UTC))

    def send(message, unused):
        delivered.append(message)
        if len(delivered) == 5:
            asyncio.get_running_loop().call_soon(lambda: heartbeat.append(len(delivered)))

    archive = SimpleNamespace(get_all=Mock(side_effect=get_all))
    room = SimpleNamespace(archive=archive, jid=JID("room@example.invalid"),
                           xmpp=SimpleNamespace(send=send))
    deliver = LegacyMUC._LegacyMUC__old_school_history
    jid = JID("fixture@example.invalid/can")
    await deliver(room, jid, maxstanzas=0)
    await deliver(room, jid, maxchars=0)
    archive.get_all.assert_not_called()
    await deliver(room, jid)
    assert len(delivered) == 50
    assert archive.get_all.call_args.kwargs["last_page_n"] == 50
    assert heartbeat == [5]
    delivered.clear()
    heartbeat.clear()
    await deliver(room, jid, maxstanzas=100)
    assert len(delivered) == 100 and heartbeat == [5]
    assert all(str(m["to"]) == str(jid) and m["delay"]["stamp"] for m in delivered)
    pool = await check_database_lifetime(deliver, jid)
    print(json.dumps({"muc_join_history": "pass", "default_limit": 50,
                      "explicit_request_preserved": 100, "zero_request": 0,
                      "heartbeat_after_messages": 5, "database_lifetime": pool}))


if __name__ == "__main__":
    asyncio.run(run())
