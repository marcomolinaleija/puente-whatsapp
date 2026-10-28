"""Offline smoke of the installed native library, actual session hooks and IQ boundary."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from xml.etree import ElementTree as ET

from slidge_whatsapp import linking
from slidge_whatsapp.session import Session
from slixmpp import JID
from slixmpp.exceptions import XMPPError


async def run():
    qr_owner = linking.LinkingSession(SimpleNamespace(), clock=lambda: 100.0)
    await qr_owner.received_qr(json.dumps({"code": "fictional-offline-fixture", "ttl": 20}))
    assert qr_owner.qr.startswith(b"\x89PNG\r\n\x1a\n")
    assert qr_owner.qr_deadline == 120.0
    assert qr_owner.snapshot()["qr_ttl"] == "20.000"
    linking.install_session(Session)
    loop = asyncio.get_running_loop()
    completed = loop.create_future()
    hooks = []
    session = SimpleNamespace(
        contacts=SimpleNamespace(user_legacy_id="", ready=loop.create_future()),
        bookmarks=SimpleNamespace(ready=loop.create_future()),
        _Session__connected=completed,
        _Session__get_connected_status_message=lambda: "Connected as fictional account",
        _Session__schedule_roster_sync=lambda: hooks.append("roster"),
        send_gateway_message=Mock(), send_gateway_status=Mock(), legacy_module_data_set=Mock(),
        user_jid=JID("fixture@example.invalid"),
        whatsapp=SimpleNamespace(Disconnect=Mock()),
        xmpp=SimpleNamespace(loop=loop),
    )
    await Session.on_wa_pair(session, "fictional-device")
    owner = linking.state(session)
    assert not owner.authenticated
    session.legacy_module_data_set.assert_called_once_with({"device_id": "fictional-device"})
    await Session.on_wa_connect(
        session, SimpleNamespace(Error="", JID="12025550100@s.whatsapp.net")
    )
    assert owner.authenticated and completed.done() and hooks == ["roster"]
    await Session.on_wa_connect(
        session, SimpleNamespace(Error="", JID="12025550100@s.whatsapp.net")
    )
    assert completed.result() == "Connected as fictional account"
    await Session.on_wa_connect(session, SimpleNamespace(Error="can-link:disconnected", JID=""))
    assert not owner.authenticated

    handlers, replies = [], []

    class Gateway:
        default_ns = "jabber:component:accept"
        boundjid = JID("whatsapp.example.invalid")
        login_wrap = AsyncMock()
        raise_if_not_allowed_jid = Mock()

        def __getitem__(self, key):
            return SimpleNamespace(add_feature=Mock())

        def get_session_from_jid(self, sender):
            return session

        def register_handler(self, value):
            handlers.append(value)

    class IQ(dict):
        def __init__(self, sender="fixture@example.invalid/resource"):
            super().__init__({"from": JID(sender), "to": Gateway.boundjid, "type": "get"})
            self.xml = ET.Element("iq")
            ET.SubElement(self.xml, f"{{{linking.NAMESPACE}}}query", {"op": "status"})

        def reply(self):
            return SimpleNamespace(xml=ET.Element("iq"), send=lambda: replies.append(True))

        def exception(self, error):
            raise error

    gateway = Gateway()
    linking.install(gateway)
    await handlers[-1]._pointer(IQ())
    assert replies == [True]
    try:
        await handlers[-1]._pointer(IQ("other@example.invalid/resource"))
    except XMPPError as exc:
        assert exc.condition == "forbidden"
    else:
        raise AssertionError("Cross-account snapshot was allowed")
    try:
        await handlers[-1]._pointer(IQ("fixture@example.invalid"))
    except XMPPError:
        pass
    else:
        raise AssertionError("Bare-JID mutation boundary was allowed")
    native = {}
    for name in ("linking-go.jsonl", "linking-race.jsonl"):
        path = Path("/opt/linking-tools") / name
        if path.exists():
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            assert not any(row.get("Action") == "fail" for row in rows)
            native[name] = sum(
                row.get("Action") == "pass" and bool(row.get("Test")) for row in rows
            )
    print(json.dumps({"offline_linking": "pass", "native": native}))


if __name__ == "__main__":
    asyncio.run(run())
