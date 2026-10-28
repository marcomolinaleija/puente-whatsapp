"""Real group marker/echo and SQLite regression; offline, fictional data only."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sqlite3
import sys
import threading
from contextlib import contextmanager
from copy import copy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, "/opt/read-marker-tools")
from slidge.core.dispatcher.message import marker as marker_module
from slidge.db.models import ArchivedMessage, Room
from slidge.group.archive import MessageArchive
from slidge.group.room import LegacyMUC
from slidge.util.lock import NamedLockMixin
from slixmpp import JID, ClientXMPP, Message
from smoke_bridge_read_markers_runtime import dispatch, fixture
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError


class Group(SimpleNamespace):
    echo = LegacyMUC.echo


@contextmanager
def group_fixture():
    with fixture() as (path, engine, store, _, dispatcher):
        with store.session(expire_on_commit=False) as orm:
            room = Room(user_account_id=1, legacy_id="fictional-group",
                        jid=JID("group@bridge.invalid"), name="Fixture")
            orm.add(room)
            orm.commit()
        owner = threading.get_ident()

        class OwnerParticipant(SimpleNamespace):
            def __getattribute__(self, name):
                assert threading.get_ident() == owner, "participant crossed thread boundary"
                return super().__getattribute__(name)

        participant = OwnerParticipant(
            stored=SimpleNamespace(occupant_id="fixture-occupant"),
            muc=SimpleNamespace(is_anonymous=False), role="moderator", affiliation="owner",
            contact=None, is_user=True, user_jid=JID("fixture@example.invalid"),
        )
        recipient = Group(
            archive=MessageArchive(room, store), stored=room, session=NamedLockMixin(),
            jid=room.jid, user_muc_jid=JID("group@bridge.invalid/fixture"),
            get_user_participant=AsyncMock(return_value=participant),
            user_full_jids=lambda: [JID("fixture@example.invalid/one"),
                                    JID("fixture@example.invalid/two")],
            log=logging.getLogger("fixture"), can_unread_up_to=AsyncMock(return_value=[]),
            on_chat_read=AsyncMock(), on_displayed=AsyncMock(),
        )
        dispatcher._MarkerMixin__to_mark = AsyncMock(return_value=[])
        try:
            yield path, engine, store, recipient, dispatcher
        finally:
            assert engine.pool.checkedout() == 0, "leaked SQL session"


def messages(store):
    with store.session() as orm:
        return [(m.stanza_id, m.stanza, m.timestamp, m.legacy_id)
                for m in orm.scalars(select(ArchivedMessage).order_by(ArchivedMessage.id))]


def marker():
    msg = Message()
    msg["type"] = "groupchat"
    msg["displayed"]["id"] = "fixture-horizon"
    return msg


async def contention(expect_blocking=False, persistent=False):
    with group_fixture() as (path, engine, store, group, dispatcher):
        reader = sqlite3.connect(path)
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM mam").fetchall()
        released, sent, workers = [], [], []
        owner = threading.get_ident()

        @event.listens_for(engine, "checkout")
        def checkout(*_):
            workers.append(threading.get_ident())

        def release():
            reader.close()
            released.append(True)

        def send(msg):
            assert threading.get_ident() == owner, "XMPP send left owner loop"
            assert messages(store), "echo sent before durable archive"
            sent.append(copy(msg))

        timer = None if persistent else asyncio.get_running_loop().call_later(0.025, release)
        failed = False
        try:
            with (patch.object(marker_module, "LegacyMUC", Group),
                  patch.object(Message, "send", send)):
                await dispatch(dispatcher, group, "fixture-horizon")
        except OperationalError as exc:
            assert "database is locked" in str(exc)
            failed = True
        finally:
            if timer:
                timer.cancel()
            reader.close()
        assert failed == (expect_blocking or persistent)
        if failed:
            assert not sent and not messages(store), "failed commit leaked an echo/archive"
        else:
            assert released and len(sent) == 2
            rows = messages(store)
            assert len(rows) == 1
            assert all(m["stanza_id"]["id"] == rows[0][0] for m in sent)
            xml = rows[0][1]
            for value in ("fixture-horizon", "fixture-occupant", "moderator", "owner",
                          "fixture@example.invalid", "group@bridge.invalid/fixture"):
                assert value in xml, (value, xml)
        if expect_blocking:
            assert not released
        else:
            assert any(t != owner for t in workers), "SQL did not leave owner thread"
        if persistent:
            with patch.object(Message, "send", send):
                await group.echo(marker(), "after-failure")
            assert len(sent) == 2, "failure retained room lock or poisoned connection"
        return {"commit_failed": failed, "loop_released_reader": bool(released),
                "echoes": len(sent)}


async def ordered_and_cancelled():
    with group_fixture() as (_, engine, store, group, _):
        entered, finish = threading.Event(), threading.Event()
        original = store.mam.add_message
        calls, sent = [], []

        def slow(*args):
            calls.append(args[2].id)
            if len(calls) == 1:
                entered.set()
                assert finish.wait(3), "worker release timeout"
            return original(*args)

        with patch.object(store.mam, "add_message", slow), patch.object(
            Message, "send", lambda m: sent.append(m["stanza_id"]["id"])
        ):
            first = asyncio.create_task(group.echo(marker(), "cancelled"))
            async with asyncio.timeout(3):
                while not entered.is_set():
                    await asyncio.sleep(0.001)
            first.cancel()
            second = asyncio.create_task(group.echo(marker(), "second"))
            await asyncio.sleep(0.025)
            assert calls == ["cancelled"], "cancellation released lock before worker finished"
            finish.set()
            results = await asyncio.gather(first, second, return_exceptions=True)
            assert isinstance(results[0], asyncio.CancelledError)
            assert results[1] == "second"
            assert sent == ["second", "second"]
            # Concurrent echoes stay ordered across archive and both XMPP resources.
            await asyncio.gather(group.echo(marker(), "third"), group.echo(marker(), "fourth"))
            assert sent == ["second", "second", "third", "third", "fourth", "fourth"]
        assert [r[0] for r in messages(store)] == ["cancelled", "second", "third", "fourth"]


async def archive_semantics():
    with group_fixture() as (_, _, store, group, _):
        participant = await group.get_user_participant()
        stamp = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
        # Sync and async preparation must have identical namespaces, timestamps and metadata.
        first = marker()
        first["stanza_id"]["id"] = "equivalent"
        first["delay"]["stamp"] = stamp
        first.set_from(group.user_muc_jid)
        group.archive.add(copy(first), participant, legacy_msg_id="legacy-equivalent")
        before = messages(store)
        await group.archive.add_async(copy(first), participant, legacy_msg_id="legacy-equivalent")
        assert messages(store) == before, "async path changed archive metadata or duplicated ID"
        # Unknown cached room pk must be resolved inside the worker, scoped by account.
        original = group.archive.room
        group.archive.room = Room(user_account_id=1, legacy_id="fictional-group", jid=original.jid)
        second = copy(first)
        second["stanza_id"]["id"] = "resolved"
        await group.archive.add_async(second, participant, legacy_msg_id="legacy-resolved")
        group.archive.room = original
        assert len(messages(store)) == 2
        ignored = marker()
        ignored.enable("no-store")
        await group.archive.add_async(ignored, participant)
        assert len(messages(store)) == 2, "no-store marker was persisted"
        replaced = copy(first)
        replaced["stanza_id"]["id"] = "replacement"
        replaced["replace"]["id"] = "equivalent"
        await group.archive.add_async(replaced, participant, legacy_msg_id="legacy-equivalent")
        assert len(messages(store)) == 3
        assert messages(store)[-1][3] is None, "replacement changed legacy correlation"
        # Account/room isolation: equal stanza IDs are not globally deduplicated.
        with store.session(expire_on_commit=False) as orm:
            other = Room(user_account_id=1, legacy_id="other", jid=JID("other@bridge.invalid"))
            orm.add(other)
            orm.commit()
        await MessageArchive(other, store).add_async(copy(first), participant)
        assert len(messages(store)) == 4


async def run(expect_blocking):
    # Register stanza plugins only; never connect an XMPP socket.
    client = ClientXMPP("fixture@example.invalid/offline", "not-a-real-password")
    for name in ("xep_0045", "xep_0308", "xep_0333", "xep_0334", "xep_0359", "xep_0421"):
        client.register_plugin(name)
    result = await contention(expect_blocking)
    if not expect_blocking:
        await contention(persistent=True)
        await ordered_and_cancelled()
        await archive_semantics()
    return {"group_echo_runtime": "baseline_reproduced" if expect_blocking else "pass", **result}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expect-blocking", action="store_true")
    print(json.dumps(asyncio.run(run(parser.parse_args().expect_blocking))))
