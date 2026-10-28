"""Installed group-read hooks, real fixture ORM and compiled binding; no real account."""
from __future__ import annotations

import asyncio
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from slidge.db.models import ArchivedMessage, ArchivedMessageSource, Base, GroupMessages
from slidge_whatsapp.generated import whatsapp
from slidge_whatsapp.message_info import InfoStore
from slidge_whatsapp.mixins import RecipientMixin
from slidge_whatsapp.read_state import (
    acknowledge_id,
    original_timestamp,
    pending_ids,
    resolve_horizon,
    sync_group_read,
)
from slixmpp import JID
from slixmpp.exceptions import XMPPError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker


async def main():
    with tempfile.TemporaryDirectory() as directory:
        engine = create_engine("sqlite:///" + str(Path(directory) / "fixture.sqlite3"))
        Base.metadata.create_all(engine)
        sessions = sessionmaker(bind=engine)
        now = datetime(2026, 1, 1, tzinfo=UTC)
        with sessions() as orm:
            for i, room in ((1, 1), (2, 1), (3, 1), (4, 2)):
                orm.add(ArchivedMessage(
                    id=i, room_id=room, stanza_id=f"xmpp-{i}", legacy_id=f"native-{i}",
                    timestamp=now + timedelta(seconds=1 if i in (2, 3) else i),
                    author_jid=JID("member@example.test"), source=ArchivedMessageSource.LIVE,
                    stanza="<message/>", displayed_by_user=False,
                ))
            orm.commit()
        db = InfoStore(Path(directory) / "info.sqlite3")
        recipient = SimpleNamespace(
            session=SimpleNamespace(),
            xmpp=SimpleNamespace(
                store=SimpleNamespace(session=sessions), _can_message_info_store=db,
            ),
            stored=SimpleNamespace(id=1),
            get_wa_chat=lambda: whatsapp.Chat(JID="100-200@g.us", IsGroup=True),
            get_wa_actor=AsyncMock(return_value=whatsapp.Actor(LID="300@lid")),
            wa=SimpleNamespace(MarkChatRead=Mock()),
        )
        try:
            assert pending_ids(recipient, "xmpp-2") == ["xmpp-1", "xmpp-2"]
            assert pending_ids(recipient, "unknown") == []
            acknowledge_id(recipient, "xmpp-1")
            assert pending_ids(recipient, "xmpp-2") == ["xmpp-2"]
            expected_stamp = int((now + timedelta(seconds=1)).timestamp())
            assert original_timestamp(recipient, "native-2") == expected_stamp
            assert original_timestamp(recipient, "native-4") is None
            # Real Slidge stores some live identities only in group_msg, while
            # MAM has the XMPP stanza ID and timestamp but a NULL legacy_id.
            with sessions() as orm:
                orm.add(ArchivedMessage(
                    id=5, room_id=1, stanza_id="xmpp-mapped", legacy_id=None,
                    timestamp=now + timedelta(seconds=5), author_jid=JID("member@example.test"),
                    source=ArchivedMessageSource.LIVE, stanza="<message/>", displayed_by_user=False,
                ))
                orm.add(GroupMessages(
                    foreign_key=1, legacy_id="native-mapped", xmpp_id="xmpp-mapped",
                ))
                orm.commit()
            expected = (int((now + timedelta(seconds=5)).timestamp()), "native-mapped")
            assert resolve_horizon(recipient, "xmpp-mapped") == expected
            assert resolve_horizon(recipient, "native-mapped") == expected
            assert "xmpp-mapped" in pending_ids(recipient, "xmpp-mapped")
            other = SimpleNamespace(xmpp=recipient.xmpp, stored=SimpleNamespace(id=2))
            assert resolve_horizon(other, "xmpp-mapped") is None
            with sessions() as orm:
                conflict = GroupMessages(
                    foreign_key=1, legacy_id="native-conflict", xmpp_id="xmpp-mapped",
                )
                orm.add(conflict)
                orm.commit()
                assert resolve_horizon(recipient, "xmpp-mapped") is None
                orm.delete(conflict)
                orm.commit()
            with sessions() as orm:
                flags = dict(orm.execute(select(
                    ArchivedMessage.stanza_id, ArchivedMessage.displayed_by_user,
                )).all())
            assert flags == {"xmpp-1": True, "xmpp-2": False, "xmpp-3": False,
                             "xmpp-4": False, "xmpp-mapped": False}
            await asyncio.gather(
                sync_group_read(recipient, "native-2"), sync_group_read(recipient, "native-2"),
            )
            payload = recipient.wa.MarkChatRead.call_args.args[0]
            assert payload.Timestamp == int((now + timedelta(seconds=1)).timestamp())
            assert list(payload.MessageIDs) == ["native-2"]
            await sync_group_read(recipient, "native-2")
            recipient.wa.MarkChatRead.assert_called_once()
            try:
                await sync_group_read(recipient, "unknown")
            except XMPPError as exc:
                assert not exc.clear
            else:
                raise AssertionError("Unknown horizon accepted")
            # Failed state send is never cached as successful and retries remain bounded.
            recipient.wa.MarkChatRead = Mock(side_effect=RuntimeError("fixture"))
            try:
                await sync_group_read(recipient, "native-3")
            except XMPPError as exc:
                assert "lectura" in exc.text
                assert not exc.clear
            else:
                raise AssertionError("Native failure hidden")
            assert recipient.wa.MarkChatRead.call_count == 1
            assert recipient._can_read_state_horizon[1] == "native-2"
            try:
                await sync_group_read(recipient, "native-3")
            except XMPPError:
                pass
            else:
                raise AssertionError("Account cooldown bypassed")
            assert recipient.wa.MarkChatRead.call_count == 1
            recipient.session._can_read_state_retry_after = 0
            recipient.wa.MarkChatRead = Mock()
            await sync_group_read(recipient, "native-3")
            assert recipient._can_read_state_horizon[1] == "native-3"
            await sync_group_read(recipient, "xmpp-mapped")
            assert list(recipient.wa.MarkChatRead.call_args.args[0].MessageIDs) == ["native-mapped"]
            # Installed mixin delegates receipts to the non-blocking helper.
            recipient.wa.SendReceipt = Mock()
            await RecipientMixin.on_displayed(recipient, "native-2", None)
            recipient.wa.SendReceipt.assert_called_once()
            # Actual compiled binding is present and safely rejects an unlinked fixture.
            native = whatsapp.Session()
            try:
                native.MarkChatRead(payload)
            except RuntimeError as exc:
                assert "unauthenticated" in str(exc)
            else:
                raise AssertionError("Unlinked native session accepted")
        finally:
            db.executor.shutdown(wait=True)
            engine.dispose()
    print("PASS read-state runtime: native/XMPP/map horizons, scoped identity, cooldown, binding")


if __name__ == "__main__":
    asyncio.run(main())
