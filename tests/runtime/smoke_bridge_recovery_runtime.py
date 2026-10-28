"""Real installed SQL readers, offline with temporary fictional data."""
from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import threading
import time
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from xml.etree import ElementTree as ET

from slidge.core.dispatcher.muc.misc import MucMiscMixin
from slidge.core.dispatcher.util import DispatcherMixin, Ignore
from slidge.core.gateway import BaseGateway
from slidge.core.mixins.attachment import AttachmentMixin
from slidge.db.models import Attachment
from slidge.group.room import LegacyMUC
from slidge.util.archive_msg import HistoryMessage
from slidge_whatsapp import session as session_module
from slidge_whatsapp.contact import Roster
from slidge_whatsapp.group import Bookmarks
from slidge_whatsapp.session import Session
from slixmpp import JID, Message
from slixmpp.exceptions import XMPPError
from slixmpp.plugins.xep_0203.stanza import Delay
from slixmpp.plugins.xep_0359.stanza import StanzaID
from slixmpp.xmlstream import register_stanza_plugin
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

register_stanza_plugin(Message, Delay)
register_stanza_plugin(Message, StanzaID)


async def muc_errors(expect_failure=False):
    component = "whatsapp.example.invalid"
    room = SimpleNamespace(remove_user_resource=Mock())
    session = SimpleNamespace(
        raise_if_not_logged=Mock(), wait_for_ready=AsyncMock(), ignore_messages=set(),
        bookmarks=SimpleNamespace(by_jid=AsyncMock(return_value=room)),
    )
    dispatcher = SimpleNamespace(xmpp=SimpleNamespace(
        boundjid=JID(component), get_session_from_jid=Mock(return_value=session),
    ))
    for name in ("_get_session", "_get_session_from_jid", "get_muc_from_stanza"):
        setattr(dispatcher, name, MethodType(getattr(DispatcherMixin, name), dispatcher))
    handler = MucMiscMixin._MucMiscMixin__on_group_chat_error
    msg = Message(sto=f"room@{component}", sfrom=f"echo@{component}/fixture", stype="error")
    msg["error"]["condition"] = "service-unavailable"
    escaped = None
    try:
        await handler(dispatcher, msg)
    except Ignore as exc:
        escaped = exc
    assert (escaped is not None) == expect_failure, "reflected MUC error escaped its handler"
    gateway, fatal_loop = SimpleNamespace(has_crashed=False), SimpleNamespace(stop=Mock())
    if escaped is not None:
        with patch("slidge.core.gateway.log"):
            BaseGateway._BaseGateway__exception_handler(
                gateway, fatal_loop, {"exception": escaped},
            )
    assert gateway.has_crashed == expect_failure
    assert fatal_loop.stop.call_count == int(expect_failure)
    dispatcher.xmpp.get_session_from_jid.assert_not_called()
    room.remove_user_resource.assert_not_called()

    # A real client error still removes only that client's resource.
    msg["from"] = "fixture@example.invalid/can-fixture"
    await handler(dispatcher, msg)
    room.remove_user_resource.assert_called_once_with("can-fixture")
    room.remove_user_resource.reset_mock()
    msg["error"]["condition"] = "forbidden"
    await handler(dispatcher, msg)
    room.remove_user_resource.assert_not_called()
    msg["error"]["condition"] = "service-unavailable"
    dispatcher.get_muc_from_stanza = AsyncMock(side_effect=XMPPError("item-not-found"))
    await handler(dispatcher, msg)
    room.remove_user_resource.assert_not_called()
    dispatcher.get_muc_from_stanza = AsyncMock(return_value=room)
    room.remove_user_resource.side_effect = KeyError("fictional absent resource")
    await handler(dispatcher, msg)
    # Do not hide programming errors or swallow cancellation to make the check green.
    for error in (ValueError("fixture"), asyncio.CancelledError()):
        dispatcher.get_muc_from_stanza = AsyncMock(side_effect=error)
        try:
            await handler(dispatcher, msg)
        except type(error):
            pass
        else:
            raise AssertionError("unrelated error was swallowed")
    return {"reflected_muc_ignore_escaped": escaped is not None,
            "fatal_gateway_stop": gateway.has_crashed, "client_resource_handling": "pass"}


def archive_xml(expect_failure=False):
    valid = ('<message xmlns="jabber:client" id="fictional">'
             '<body>Espa\u00f1ol \U0001f642 &amp; &lt; &gt;\t\n\r</body>'
             '<stanza-id xmlns="urn:xmpp:sid:0" id="fixture-id"/></message>')
    assert HistoryMessage(valid).id == "fixture-id"
    raw = valid.replace("Espa\u00f1ol", "before\x0bafter")
    failed = False
    try:
        recovered = HistoryMessage(raw)
    except ET.ParseError:
        failed = True
    assert failed == expect_failure
    assert "\x0b" in raw, "decoding must not rewrite stored rows"
    if not expect_failure:
        assert recovered.id == "fixture-id"
        assert recovered.stanza["body"].startswith("before\ufffdafter")
        ET.fromstring(str(recovered.stanza))
        for code in (*range(32), 0xd800, 0xdfff, 0xfffe, 0xffff):
            char = chr(code)
            msg = Message()
            msg["body"] = f"before{char}after"
            child = ET.SubElement(msg.xml, "fixture", {"value": f"before{char}after"})
            child.tail = f"before{char}after"
            new = HistoryMessage(msg)
            ET.fromstring(str(new.stanza))
            expected = char if char in "\t\n\r" else "\ufffd"
            assert new.stanza["body"] == f"before{expected}after"
            assert child.get("value") == child.tail == f"before{expected}after"
        try:
            HistoryMessage("<message><body>structurally broken</message>")
        except ET.ParseError:
            pass
        else:
            raise AssertionError("structural XML errors must remain visible")
    return {"xml_vertical_tab_parse_failure": failed,
            "stored_rows_unchanged": True, "xml_character_cases": 36 if not failed else 1}


async def readers(expect_old):
    with tempfile.TemporaryDirectory() as temp:
        engine = create_engine(f"sqlite:///{temp}/fixture.sqlite", connect_args={"timeout": 0.05})
        session = sessionmaker(engine)
        try:
            Attachment.__table__.create(engine)
            with engine.begin() as conn:
                conn.execute(text("CREATE TABLE fixture (id INTEGER PRIMARY KEY, stanza TEXT)"))
                conn.execute(text("INSERT INTO fixture VALUES (:id, :stanza)"), [
                    {"id": i, "stanza":
                     '<message xmlns="jabber:client"><body>fixture</body></message>'}
                    for i in range(20)
                ])
            with session() as orm:
                orm.add(Attachment(user_account_id=1, legacy_file_id="fixture", url="https://example.invalid"))
                orm.commit()

            def get_messages(orm, room, ids):
                assert ids == ["fixture"]
                yield from orm.execute(text("SELECT stanza FROM fixture ORDER BY id")).yield_per(1)

            room = SimpleNamespace(
                stored=SimpleNamespace(id=1), xmpp=SimpleNamespace(store=SimpleNamespace(
                    session=session, mam=SimpleNamespace(get_messages=get_messages),
                )),
            )
            iterator = LegacyMUC.get_archived_messages(room, "fixture")
            next(iterator)
            reader_held = engine.pool.checkedout() != 0

            def write():
                with engine.begin() as conn:
                    conn.execute(text("UPDATE fixture SET stanza=stanza WHERE id=19"))

            locked = False
            try:
                await asyncio.to_thread(write)
            except OperationalError as exc:
                if "database is locked" not in str(exc):
                    raise
                locked = True
            finally:
                iterator.close()
            assert reader_held == expect_old and locked == expect_old, (reader_held, locked)

            held_at_http = []

            async def validate(url):
                held_at_http.append(engine.pool.checkedout())
                return True

            recipient = SimpleNamespace(
                _AttachmentMixin__is_component=False, session=SimpleNamespace(user_pk=1),
                xmpp=SimpleNamespace(store=SimpleNamespace(session=session)),
                _AttachmentMixin__valid_url=validate,
            )
            stored = await AttachmentMixin._AttachmentMixin__get_stored(
                recipient, SimpleNamespace(legacy_file_id="fixture"),
            )
            assert stored.url == "https://example.invalid"
            assert held_at_http == ([1] if expect_old else [0]), held_at_http
            assert engine.pool.checkedout() == 0
            return {"archive_reader_held": reader_held, "writer_locked": locked,
                    "attachment_checkouts_at_http": held_at_http[0]}
        finally:
            engine.dispose()


async def roster_roundtrip(expect_old):
    loop = asyncio.get_running_loop()
    owner_thread = threading.get_ident()
    completed, off_loop = [], []

    async def callback():
        return None

    def native(*args, **kwargs):
        off_loop.append(threading.get_ident() != owner_thread)
        # Simulates native contact/app-state work that needs a Python callback.
        # Calling this on the owner loop blocks its own completion.
        future = asyncio.run_coroutine_threadsafe(callback(), loop)
        try:
            future.result(timeout=0.2)
        except TimeoutError:
            future.cancel()
            raise
        completed.append(True)
        return []

    ready = loop.create_future()
    ready.set_result(None)
    wa = SimpleNamespace(GetContacts=native, GetGroups=native, SubscribeToPresences=native)
    session = SimpleNamespace(whatsapp=wa, contacts=SimpleNamespace(ready=ready),
                              log=Mock(), user_jid=SimpleNamespace(bare="fixture@example.invalid"))
    roster = SimpleNamespace(session=session, _refresh_mexico_aliases=Mock())
    bookmarks = SimpleNamespace(session=session)

    async def contacts():
        assert [item async for item in Roster.fill(roster)] == []

    checks = (
        contacts,
        lambda: Bookmarks.fill(bookmarks),
        lambda: Session._Session__sync_roster_after_connect(session),
        lambda: Session.on_preferences(session, {}, {"roster_add_non_friends": True}),
    )
    for check in checks:
        try:
            with patch.object(session_module.SyncContacts, "sync", new_callable=AsyncMock), \
                    patch.object(session_module.asyncio, "sleep", new_callable=AsyncMock):
                await check()
        except TimeoutError:
            assert expect_old, "native callback blocked the event loop"
    assert all(off_loop) == (not expect_old)
    assert len(completed) == (0 if expect_old else 5), (off_loop, completed)
    return {"native_roster_roundtrips": len(completed), "native_calls_off_loop": all(off_loop)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expect-old", action="store_true")
    parser.add_argument("--muc-errors-only", action="store_true")
    parser.add_argument("--expect-muc-failure", action="store_true")
    parser.add_argument("--archive-xml-only", action="store_true")
    parser.add_argument("--expect-xml-failure", action="store_true")
    args = parser.parse_args()
    if args.muc_errors_only:
        print(json.dumps(asyncio.run(muc_errors(args.expect_muc_failure))))
        raise SystemExit(0)
    if args.archive_xml_only:
        print(json.dumps(archive_xml(args.expect_xml_failure)))
        raise SystemExit(0)
    result = asyncio.run(readers(args.expect_old))
    result.update(asyncio.run(roster_roundtrip(args.expect_old)))
    if not args.expect_old:
        result.update(asyncio.run(muc_errors()))
        result.update(archive_xml())
    print(json.dumps({"recovery_runtime": "baseline_reproduced" if args.expect_old else "pass",
                      **result}))
