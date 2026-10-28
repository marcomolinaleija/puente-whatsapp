"""Real installed bridge handlers, native constants, SQLite and IQs; no real account."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from slidge_whatsapp.generated import whatsapp
from slidge_whatsapp.message_info import (
    NAMESPACE,
    InfoStore,
    handle_info,
    info_xml,
    install,
    outgoing_sent,
    prepare_outgoing,
    record_receipt,
)
from slidge_whatsapp.session import Session
from slixmpp import JID, ClientXMPP
from slixmpp.exceptions import XMPPError
from slixmpp.stanza import Iq


async def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        db = InfoStore(Path(directory) / "info.sqlite3")
        account, group, message_id = "fixture@example.test", "100-200@g.us", "fixture-message"
        session = SimpleNamespace(
            user_jid=JID(account),
            xmpp=SimpleNamespace(_can_message_info_store=db),
        )
        people = [
            SimpleNamespace(is_user=True),
            SimpleNamespace(
                is_user=False,
                contact=SimpleNamespace(legacy_id="15551111111@s.whatsapp.net"),
                occupant_id="200@lid",
                nickname="Fixture Alpha",
            ),
            SimpleNamespace(
                is_user=False, contact=None, occupant_id="300@lid", nickname="Fixture Beta"
            ),
        ]

        async def participants():
            for person in people:
                yield person

        recipient = SimpleNamespace(
            session=session,
            get_participants=participants,
            participants_filled=True,
            n_participants=3,
        )
        message = SimpleNamespace(Chat=SimpleNamespace(JID=group, IsGroup=True), ID=message_id)
        snapshot = await prepare_outgoing(recipient, message)
        assert snapshot[0] and len(snapshot[1]) == 2
        await outgoing_sent(recipient, message, snapshot)
        contact = SimpleNamespace(nickname="Fixture Alpha", received=Mock(), displayed=Mock())
        session._Session__get_contact_or_participant = AsyncMock(return_value=(contact, recipient))
        session.log = Mock()
        receipt = SimpleNamespace(
            Chat=message.Chat,
            Actor=SimpleNamespace(IsMe=False, JID=people[1].contact.legacy_id, LID="200@lid"),
            Kind=whatsapp.ReceiptRead,
            Timestamp=2000,
            MessageIDs=[message_id],
        )
        await Session.on_wa_receipt(session, receipt)
        contact.displayed.assert_called_once_with(legacy_msg_id=message_id, carbon=False)
        receipt.Kind, receipt.Timestamp = whatsapp.ReceiptDelivered, 1000
        await Session.on_wa_receipt(session, receipt)
        contact.received.assert_called_once_with(message_id)
        data = await db.run(db.get, account, group, message_id)
        assert data["own"] and data["complete"] and len(data["rows"]) == 2
        alpha = next(p for p in data["rows"] if p["lid"] == "200@lid")
        assert alpha["read-at"] == 2000 and alpha["delivered-at"] == 1000
        # Self-read sync must not appear as another recipient.
        receipt.Actor.IsMe = True
        await record_receipt(session, receipt)
        assert await db.run(db.get, "other@example.test", group, message_id) is None

        session.bookmarks = SimpleNamespace(
            by_jid_only_if_exists=lambda jid: SimpleNamespace(legacy_id=group)
        )
        gateway = SimpleNamespace(
            get_session_from_jid=lambda jid: session if jid.bare == account else None
        )
        # Use a real (disconnected) XMLStream: mock generated IDs aren't XML strings.
        stream = ClientXMPP("fixture@example.test", "unused-fixture-password")
        stream.send = Mock()
        iq = Iq(stream=stream)
        iq["from"], iq["to"], iq["type"] = account + "/fixture", "room@bridge.example.test", "get"
        iq.append(info_xml(message_id, message_id, data))
        await handle_info(gateway, iq)
        assert stream.send.called
        # The actual response is generated using the installed stanza implementation.
        assert 'recipients-complete="true"' in str(stream.send.call_args.args[0])
        iq["from"] = "other@example.test/fixture"
        try:
            await handle_info(gateway, iq)
        except XMPPError as exc:
            assert exc.condition == "forbidden"
        else:
            raise AssertionError("Unregistered account accessed receipts")
        db.executor.shutdown(wait=True)
        reopened = InfoStore(Path(directory) / "info.sqlite3")
        assert reopened.get(account, group, message_id) == data
        reopened.executor.shutdown(wait=True)

    from slidge import global_config

    old_home = getattr(global_config, "HOME_DIR", None)
    with tempfile.TemporaryDirectory() as directory:
        global_config.HOME_DIR = Path(directory)
        plugin = Mock()
        gateway = Mock(default_ns="jabber:component:accept")
        gateway.__getitem__ = Mock(return_value=plugin)
        install(gateway)
        handler = gateway.register_handler.call_args.args[0]
        iq = Iq()
        iq.xml.tag = "{jabber:component:accept}iq"
        from xml.etree import ElementTree as ET

        iq.append(ET.Element(f"{{{NAMESPACE}}}info"))
        assert handler.match(iq)
        gateway._can_message_info_store.executor.shutdown(wait=True)
    if old_home is None:
        del global_config.HOME_DIR
    else:
        global_config.HOME_DIR = old_home
    print("PASS installed message-info handlers, private IQ, SQLite, identity, restart and matcher")


if __name__ == "__main__":
    asyncio.run(main())
