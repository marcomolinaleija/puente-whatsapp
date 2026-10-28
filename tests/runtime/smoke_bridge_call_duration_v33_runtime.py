"""Native Go fixture -> generated binding -> installed Python handler -> XMPP call XML."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from xml.etree import ElementTree as ET

from slidge_whatsapp.generated import whatsapp
from slidge_whatsapp.session import CALL_TRANSPORT_PREFIX, Session
from slixmpp import JID


async def main():
    metadata = json.loads(Path("/opt/can-tests/call-history-fixture.json").read_text())
    assert metadata["direction"] == "incoming" and metadata["duration_seconds"] == 73
    call = whatsapp.Call(Actor=whatsapp.Actor(
        JID=metadata["peer_jid"], LID=CALL_TRANSPORT_PREFIX + json.dumps(metadata),
    ))
    recipient = SimpleNamespace(name="Fixture contact", jid=JID("fixture@bridge.example.test"))
    session = SimpleNamespace(
        contacts=SimpleNamespace(by_legacy_id=AsyncMock(return_value=recipient)),
        send_gateway_message=Mock(),
    )
    await Session.on_wa_call(session, call)
    kwargs = session.send_gateway_message.call_args.kwargs
    xml = kwargs["extra_xml"]
    assert xml.get("direction") == "incoming"
    assert xml.get("duration-seconds") == "73"
    assert xml.get("source") == "history_sync"
    assert xml.get("chat-jid") == "fixture@bridge.example.test"
    assert xml.get("call-id") == "incoming-history-fixture"
    assert "when" in kwargs
    print("PASS incoming call: native history, binding, installed handler and routed XML")
    # This artifact contains only the fictitious native test call, never a real account.
    Path("/tmp/can-call-fixture.xml").write_text(ET.tostring(xml, encoding="unicode"))


if __name__ == "__main__":
    asyncio.run(main())
