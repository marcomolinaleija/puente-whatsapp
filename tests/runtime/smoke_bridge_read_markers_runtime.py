"""Installed marker dispatcher + real SQLite, offline and with fictional data only."""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from slidge.contact.contact import LegacyContact
from slidge.core import config
from slidge.core.dispatcher.message import marker as marker_module
from slidge.core.dispatcher.message.marker import MarkerMixin
from slidge.db.meta import Base, get_engine
from slidge.db.models import Contact, ContactSent, GatewayUser
from slidge.db.store import SlidgeStore
from slidge.util.lock import NamedLockMixin
from slixmpp import JID, Message
from slixmpp.plugins.xep_0333.stanza import Displayed
from slixmpp.xmlstream import register_stanza_plugin
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

register_stanza_plugin(Message, Displayed)


@contextmanager
def fixture():
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "fixture.sqlite"
        engine = get_engine(f"sqlite:///{path}")

        @event.listens_for(engine, "connect")
        def fixture_timeout(connection, _record):
            connection.execute("PRAGMA busy_timeout=200")

        Base.metadata.create_all(engine)
        with patch.object(config, "HOME_DIR", Path(temp), create=True):
            store = SlidgeStore(engine)
        with store.session(expire_on_commit=False) as orm:
            user = GatewayUser(jid=JID("fixture@example.invalid"))
            contact = Contact(user=user, legacy_id="fixture", jid=JID("contact@example.invalid"))
            orm.add(contact)
            orm.flush()
            pk = contact.id
            orm.add_all(ContactSent(contact_id=pk, msg_id=f"fixture-{i}") for i in range(5))
            orm.commit()
        session = NamedLockMixin()
        recipient = SimpleNamespace(stored=contact, session=session, is_group=False,
                                    on_displayed=AsyncMock())
        recipient.xmpp = SimpleNamespace(store=store, MARK_ALL_MESSAGES=True)
        recipient.pop_unread_xmpp_ids_up_to = lambda msg_id: (
            LegacyContact.pop_unread_xmpp_ids_up_to(recipient, msg_id)
        )
        dispatcher = SimpleNamespace(xmpp=recipient.xmpp,
                                     _get_session=AsyncMock(return_value=session),
                                     _xmpp_to_legacy_thread=AsyncMock(return_value=None),
                                     _xmpp_msg_id_to_legacy=lambda value, _: value)
        dispatcher._MarkerMixin__to_mark = lambda recipient, msg_id: (
            MarkerMixin._MarkerMixin__to_mark(dispatcher, recipient, msg_id)
        )
        try:
            yield path, engine, store, recipient, dispatcher
        finally:
            engine.dispose()


async def dispatch(dispatcher, recipient, horizon="fixture-2"):
    msg = Message()
    msg["displayed"]["id"] = horizon
    with patch.object(marker_module, "get_recipient", AsyncMock(return_value=recipient)):
        # The real handler, without only its error-to-XMPP decorator.
        await MarkerMixin.on_marker_displayed.__wrapped__(dispatcher, msg)


def remaining(store):
    with store.session() as orm:
        return list(orm.scalars(select(ContactSent.msg_id).order_by(ContactSent.id)))


async def reader_contention(expect_blocking):
    with fixture() as (path, engine, store, recipient, dispatcher):
        reader = sqlite3.connect(path)
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM contact_sent").fetchall()
        released = []

        def release():
            reader.close()
            released.append(True)

        timer = asyncio.get_running_loop().call_later(0.025, release)
        failed = False
        try:
            await dispatch(dispatcher, recipient)
        except OperationalError as exc:
            assert "database is locked" in str(exc), type(exc).__name__
            failed = True
        finally:
            timer.cancel()
            reader.close()
        assert failed == expect_blocking, "commit blocked the loop needed to release the reader"
        if expect_blocking:
            assert not released
            recipient.on_displayed.assert_not_awaited()
        else:
            assert released
            assert [call.args[0] for call in recipient.on_displayed.await_args_list] == [
                "fixture-0", "fixture-1", "fixture-2",
            ]
            assert remaining(store) == ["fixture-3", "fixture-4"]
        assert engine.pool.checkedout() == 0
        return {"blocked_commit": failed, "reader_released_on_loop": bool(released)}


def pool_after_failed_commit(expect_blocking):
    with fixture() as (path, engine, store, recipient, _):
        connections = []

        @event.listens_for(engine, "checkout")
        def remember_connection(connection, _record, _proxy):
            connections.append(connection)

        reader = sqlite3.connect(path)
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM contact_sent").fetchall()
        try:
            try:
                # Deliberately exercise the unchanged synchronous core method too.
                LegacyContact.pop_unread_xmpp_ids_up_to(recipient, "fixture-2")
            except OperationalError:
                pass
            else:
                raise AssertionError("fixture did not force a failed commit")
        finally:
            reader.close()
        # Do not check out/in a second time: that reset would hide the defect.
        pending = connections[-1].in_transaction
        locked = False
        with sqlite3.connect(path, timeout=0.05) as independent:
            try:
                count = independent.execute("SELECT count(*) FROM contact_sent").fetchone()[0]
                assert count == 5, "a failed read-marker commit changed durable rows"
                independent.execute("UPDATE contact_sent SET msg_id=msg_id")
                independent.commit()
            except sqlite3.OperationalError as exc:
                assert "database is locked" in str(exc)
                locked = True
        assert pending == locked == expect_blocking, (pending, locked)
        assert engine.pool.checkedout() == 0
        return {"orphaned_transaction_after_commit_failure": pending}


async def persistent_contention():
    with fixture() as (path, engine, store, recipient, dispatcher):
        reader = sqlite3.connect(path)
        try:
            reader.execute("BEGIN")
            reader.execute("SELECT * FROM contact_sent").fetchall()
            try:
                await dispatch(dispatcher, recipient)
            except OperationalError:
                pass
            else:
                raise AssertionError("persistent contention falsely acknowledged a read")
            recipient.on_displayed.assert_not_awaited()
        finally:
            reader.close()
        assert remaining(store) == [f"fixture-{i}" for i in range(5)]
        assert engine.pool.checkedout() == 0
        # Failure must release both the transaction and its existing named lock.
        await dispatch(dispatcher, recipient)
        assert remaining(store) == ["fixture-3", "fixture-4"]


async def overlapping_and_cancelled():
    with fixture() as (_, engine, store, recipient, dispatcher):
        pop = dispatcher._MarkerMixin__to_mark
        owner_thread = threading.get_ident()
        worker_threads = []
        original = store.contacts.pop_sent_up_to
        entered, finish = threading.Event(), threading.Event()

        def slow_pop(*args):
            worker_threads.append(threading.get_ident())
            entered.set()
            assert finish.wait(2), "worker was not released"
            return original(*args)

        with patch.object(store.contacts, "pop_sent_up_to", slow_pop):
            first = asyncio.create_task(pop(recipient, "fixture-2"))
            while not entered.is_set():
                await asyncio.sleep(0.001)
            first.cancel()
            await asyncio.sleep(0)
            second = asyncio.create_task(pop(recipient, "fixture-2"))
            await asyncio.sleep(0.02)
            assert len(worker_threads) == 1, "cancel released lock while SQL worker still ran"
            finish.set()
            result = await asyncio.gather(first, second, return_exceptions=True)
        assert isinstance(result[0], asyncio.CancelledError)
        assert result[1] == [], ("duplicate horizon consumed newer rows", result[1])
        assert worker_threads and owner_thread not in worker_threads
        assert remaining(store) == ["fixture-3", "fixture-4"]
        result = await asyncio.gather(pop(recipient, "fixture-3"), pop(recipient, "fixture-4"))
        assert result == [["fixture-3"], ["fixture-4"]]
        assert engine.pool.checkedout() == 0


async def bypasses():
    with fixture() as (_, engine, store, recipient, dispatcher):
        assert await dispatcher._MarkerMixin__to_mark(recipient, "unknown-horizon") == []
        assert remaining(store) == [f"fixture-{i}" for i in range(5)]
        dispatcher.xmpp.MARK_ALL_MESSAGES = False
        assert await dispatcher._MarkerMixin__to_mark(recipient, "fixture-2") == ["fixture-2"]
        assert remaining(store) == [f"fixture-{i}" for i in range(5)]
        assert engine.pool.checkedout() == 0
        with store.session.begin() as orm:
            other = Contact(user_account_id=1, legacy_id="other-fixture",
                            jid=JID("other@example.invalid"))
            orm.add(other)
            orm.flush()
            orm.add(ContactSent(contact_id=other.id, msg_id="fixture-0"))
        dispatcher.xmpp.MARK_ALL_MESSAGES = True
        await dispatch(dispatcher, recipient)
        assert remaining(store) == ["fixture-3", "fixture-4", "fixture-0"]

        class Group(SimpleNamespace):
            pass

        group = Group(can_unread_up_to=AsyncMock(return_value=["group-fixture"]))
        with patch.object(marker_module, "LegacyMUC", Group):
            assert await dispatcher._MarkerMixin__to_mark(group, "horizon") == ["group-fixture"]
        group.can_unread_up_to.assert_awaited_once_with("horizon")


async def run(expect_blocking):
    result = await reader_contention(expect_blocking)
    result.update(pool_after_failed_commit(expect_blocking))
    if not expect_blocking:
        await persistent_contention()
        await overlapping_and_cancelled()
        await bypasses()
    return {"marker_runtime": "baseline_reproduced" if expect_blocking else "pass", **result}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expect-blocking", action="store_true")
    print(json.dumps(asyncio.run(run(parser.parse_args().expect_blocking))))
