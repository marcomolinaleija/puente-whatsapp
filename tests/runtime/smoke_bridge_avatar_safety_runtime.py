"""Exercise real v32 handlers and SQLite migration without a WhatsApp account."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from slidge.core.dispatcher.session_dispatcher import SessionDispatcher
from slidge.core.gateway import BaseGateway
from slidge.db import GatewayUser
from slidge_whatsapp.avatar_safety import disable_avatar_sync
from slidge_whatsapp.gateway import Gateway
from slidge_whatsapp.group import MUC
from slidge_whatsapp.session import Session
from slixmpp import JID
from slixmpp.exceptions import IqError
from slixmpp.stanza import Iq
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def verify_preferences_and_migration() -> None:
    base = next(f for f in BaseGateway.PREFERENCES if f.var == "sync_avatar")
    safe = next(f for f in Gateway.PREFERENCES if f.var == "sync_avatar")
    assert safe.value == "false" and safe is not base
    for field in Gateway.PREFERENCES:
        if field.var != "sync_avatar":
            original = next((f for f in BaseGateway.PREFERENCES if f.var == field.var), None)
            if original is not None:
                assert field.value == original.value

    engine = create_engine("sqlite:///:memory:")
    GatewayUser.__table__.create(engine)
    maker = sessionmaker(engine)
    preferences = (
        {"sync_avatar": True, "sync_presence": False},
        {"sync_avatar": False, "enable_link_previews": True},
        {"sync_presence": True},
        {"sync_avatar": "true", "reaction_fallback": False},
    )
    with maker() as orm:
        for index, pref in enumerate(preferences):
            orm.add(GatewayUser(
                jid=JID(f"fixture-{index}@example.test"), preferences=pref,
                avatar_hash="fixture-hash" if index == 0 else None,
                legacy_module_data={"device_id": f"fixture-device-{index}"},
            ))
        orm.commit()
    store = SimpleNamespace(session=maker)
    assert disable_avatar_sync(store) == 3
    assert disable_avatar_sync(store) == 0
    with maker() as orm:
        users = orm.query(GatewayUser).order_by(GatewayUser.id).all()
        for index, user in enumerate(users):
            assert user.preferences == {**preferences[index], "sync_avatar": False}
            assert user.avatar_hash is None
            assert user.legacy_module_data == {"device_id": f"fixture-device-{index}"}
    engine.dispose()


async def verify_handlers() -> None:
    whatsapp = SimpleNamespace(SetAvatar=Mock())
    session = SimpleNamespace(
        whatsapp=whatsapp, user_jid=JID("fixture@example.test"), log=Mock(),
        user=SimpleNamespace(avatar_hash="old-fixture", preferences={"sync_avatar": True}),
        send_gateway_message=Mock(), bookmarks=[],
    )

    async def on_avatar(*args):
        await Session.on_avatar(session, *args)

    session.on_avatar = on_avatar
    for data in (None, b"", b"fixture-image"):
        try:
            await on_avatar(data, "fixture-hash", "image/png", 32, 32)
        except NotImplementedError:
            pass
        else:
            raise AssertionError("Own avatar synchronization must be unsupported")

    async def missing_metadata(*_args, **_kwargs):
        iq = Iq()
        iq["type"] = "error"
        iq["error"]["condition"] = "item-not-found"
        raise IqError(iq)

    @contextmanager
    def orm_session(**_kwargs):
        yield SimpleNamespace(add=Mock(), commit=Mock())

    metadata = SimpleNamespace(namespace="urn:xmpp:avatar:metadata")
    retrieve = AsyncMock(return_value={
        "pubsub": {"items": {"item": {"avatar_data": {"value": b"fixture-image"}}}},
    })
    gateway = SimpleNamespace(
        xmpp=SimpleNamespace(plugin={
            "xep_0060": SimpleNamespace(get_items=missing_metadata),
            "xep_0084": SimpleNamespace(
                stanza=SimpleNamespace(MetaData=metadata), retrieve_avatar=retrieve,
            ),
        }),
        boundjid=JID("bridge.example.test"), log=Mock(),
        store=SimpleNamespace(session=orm_session),
    )
    await BaseGateway.fetch_user_avatar(gateway, session)
    dispatcher = SimpleNamespace(xmpp=SimpleNamespace(
        plugin=gateway.xmpp.plugin, boundjid=gateway.boundjid, store=gateway.store,
    ))
    for avatar_id in ("", "new-fixture"):
        session.user.avatar_hash = "old-fixture"
        await SessionDispatcher.on_avatar_metadata_info(dispatcher, session, {
            "id": avatar_id, "type": "image/png", "height": 32, "width": 32,
        })
    whatsapp.SetAvatar.assert_not_called()
    session.send_gateway_message.assert_not_called()

    # Group photo edits use their own handler and retain the room identity.
    room = SimpleNamespace(session=session, legacy_id="fixture-group@g.us")
    for data in (None, b"fixture-group-image"):
        await MUC.on_avatar(room, data, "image/jpeg")
        args = whatsapp.SetAvatar.call_args.args
        assert args[0] == room.legacy_id
        if data:
            assert bytes(args[1]) == data
        else:
            # The generated Go binding cannot convert an empty slice to bytes.
            assert len(args[1]) == 0
    assert whatsapp.SetAvatar.call_count == 2


if __name__ == "__main__":
    verify_preferences_and_migration()
    asyncio.run(verify_handlers())
    print("Avatar safety v32: own updates blocked, SQLite migrated, group edits preserved")
