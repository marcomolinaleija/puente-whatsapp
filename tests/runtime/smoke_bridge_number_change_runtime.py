"""Exercise patched Slidge methods against fictional in-memory SQLite, without XMPP/WA."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import MethodType, SimpleNamespace

from slidge.db.meta import Base
from slidge.db.models import Participant, Room
from slidge.group.room import LegacyMUC
from slidge_whatsapp.session import Session
from slixmpp import JID
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


async def exercise() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    with factory(expire_on_commit=False) as orm:
        room = Room(user_account_id=1, jid=JID("#fixture@component.example.org"),
                    legacy_id="120363000000000001@g.us", user_nick="Owner", updated=True)
        other = Room(user_account_id=2, jid=JID("#other@component.example.org"),
                     legacy_id="120363000000000002@g.us", user_nick="Owner", updated=True)
        orm.add_all((room, other))
        orm.flush()
        for parent, occupant, resource, own in (
            (room, "1001@lid", "Owner-old", True),
            (room, "1002@lid", "Owner", True),
            (room, "1003@lid", "Owner-remote", False),
            (other, "1002@lid", "Owner", False),
        ):
            orm.add(Participant(room=parent, nickname="Owner", nickname_no_illegal="Owner",
                                resource=resource, occupant_id=occupant, is_user=own))
        orm.commit()
        room_id, other_id = room.id, other.id

    class Part:
        def __init__(self, muc: object, stored: Participant):
            self.stored = stored
            if not stored.resource:
                stored.resource = stored.nickname
                stored.nickname_no_illegal = stored.nickname

        @property
        def is_user(self) -> bool:
            return self.stored.is_user

        @is_user.setter
        def is_user(self, value: bool) -> None:
            self.stored.is_user = value

    def save(part: Part) -> None:
        with factory(expire_on_commit=False) as orm:
            orm.add(part.stored)
            orm.commit()

    def nick_available(orm: object, room_id: int, nick: str) -> bool:
        return not orm.query(Participant).filter_by(room_id=room_id, resource=nick).count()

    muc = SimpleNamespace(
        session=SimpleNamespace(), stored=room, user_nick="Owner",
        xmpp=SimpleNamespace(store=SimpleNamespace(
            session=factory, rooms=SimpleNamespace(nick_available=nick_available))),
        log=logging.getLogger("number-change-smoke"),
        participant_from_store=lambda stored: Part(None, stored),
        _participant_cls=Part, send_affiliation_change=lambda *a: None,
        _LegacyMUC__store_participant=save,
    )
    muc.get_participant = MethodType(LegacyMUC.get_participant, muc)
    # Without an ID, the old nickname OR resource lookup raises MultipleResultsFound.
    # Multiple historical self rows must not cause arbitrary selection or deletion.
    virtual = await LegacyMUC.get_user_participant(muc)
    assert virtual.stored.occupant_id == "slidge-user"
    assert virtual.is_user
    # Current own metadata takes precedence over historical aliases, even with the same nick.
    muc.session._can_own_occupant_id = "1002@lid"
    current = await LegacyMUC.get_user_participant(muc)
    assert current.stored.occupant_id == "1002@lid"
    assert current.stored.resource == "Owner"
    # Explicit identity wins over the session cache; enrich an anonymous row, not a namesake.
    explicit = await LegacyMUC.get_user_participant(muc, occupant_id="1003@lid")
    assert explicit.stored.occupant_id == "1003@lid" and explicit.is_user
    with factory() as orm:
        assert orm.query(Participant).filter_by(room_id=room_id).count() == 4
        assert orm.query(Participant).filter_by(room_id=other_id).one().is_user is False
        old = orm.query(Participant).filter_by(room_id=room_id, occupant_id="1001@lid").one()
        assert old.resource == "Owner-old"
    # New identity is stored separately; old aliases and resources remain available for history.
    muc.session._can_own_occupant_id = "2000@lid"
    new = await LegacyMUC.get_user_participant(muc)
    again = await LegacyMUC.get_user_participant(muc)
    assert new.stored.id == again.stored.id and new.stored.occupant_id == "2000@lid"
    with factory() as orm:
        assert orm.query(Participant).filter_by(room_id=room_id).count() == 5

    # A re-pair with an already-completed login future still refreshes the own PN.
    future = asyncio.get_running_loop().create_future()
    future.set_result("old session")
    contacts = SimpleNamespace(user_legacy_id="15550000001@s.whatsapp.net")
    statuses: list[str] = []
    session = SimpleNamespace(
        contacts=contacts, user_phone="+15550000001", _can_own_occupant_id="1001@lid",
        _Session__connected=future,
        _Session__get_connected_status_message=lambda: "Connected as " + contacts.user_legacy_id,
        _Session__schedule_roster_sync=lambda: None,
        send_gateway_status=lambda text, **kw: statuses.append(text),
        send_gateway_message=lambda text: None,
    )
    await Session.on_wa_connect(
        session, SimpleNamespace(Error="", JID="15550000002@s.whatsapp.net")
    )
    assert contacts.user_legacy_id == "15550000002@s.whatsapp.net"
    assert session.user_phone == "+15550000002" and session._can_own_occupant_id is None
    assert "15550000002" in statuses[-1]
    await Session.on_wa_connect(
        session, SimpleNamespace(Error="failure", JID="15550000003@s.whatsapp.net")
    )
    assert contacts.user_legacy_id == "15550000002@s.whatsapp.net"
    engine.dispose()


if __name__ == "__main__":
    # Ensure source and generated native extension are both present in this image.
    assert "canHistoricalActor" in Path(
        "/venv/lib/python3.13/site-packages/slidge_whatsapp/event.go"
    ).read_text()
    asyncio.run(exercise())
    print("Number-change runtime smoke: ok (SQLite identity, aliases, isolation, re-pair)")
