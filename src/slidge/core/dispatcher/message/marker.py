import asyncio
from sqlalchemy import select
from ....db.models import ContactSent

from typing import TYPE_CHECKING

from slixmpp import JID, Message
from slixmpp.xmlstream import StanzaBase

from ....group.room import LegacyMUC
from ....util.types import AnyRecipient
from ..util import DispatcherMixin, exceptions_to_xmpp_errors, get_recipient

if TYPE_CHECKING:
    from slidge.util.types import AnyGateway, AnyRecipient


class MarkerMixin(DispatcherMixin):
    __slots__: list[str] = []

    def __init__(self, xmpp: "AnyGateway") -> None:
        super().__init__(xmpp)
        xmpp.add_event_handler("marker_displayed", self.on_marker_displayed)
        xmpp.add_event_handler(
            "message_displayed_synchronization_publish",
            self.on_message_displayed_synchronization_publish,
        )

    @exceptions_to_xmpp_errors
    async def on_marker_displayed(self, msg: StanzaBase) -> None:
        assert isinstance(msg, Message)
        session = await self._get_session(msg)

        recipient = await get_recipient(session, msg)
        legacy_thread = await self._xmpp_to_legacy_thread(session, msg, recipient)
        to_mark = await self.__to_mark(recipient, msg["displayed"]["id"])
        for xmpp_id in to_mark:
            await recipient.on_displayed(
                self._xmpp_msg_id_to_legacy(xmpp_id, recipient), legacy_thread
            )
            ack = getattr(recipient, 'can_ack_read', None)
            if isinstance(recipient, LegacyMUC) and ack is not None:
                await ack(xmpp_id)
        if isinstance(recipient, LegacyMUC):
            hook = getattr(recipient, 'on_chat_read', None)
            if hook is not None:
                await hook(msg['displayed']['id'])
            await recipient.echo(msg, None)

    async def __to_mark(self, recipient: AnyRecipient, msg_id: str) -> list[str]:
        hook = getattr(recipient, 'can_unread_up_to', None)
        if isinstance(recipient, LegacyMUC) and hook is not None:
            return await hook(msg_id)
        if self.xmpp.MARK_ALL_MESSAGES:
            # CAN nonblocking contact read markers v1
            # Snapshot the scalar on the owner loop; no ORM instance crosses threads.
            contact_pk = recipient.stored.id
            assert contact_pk is not None
            store = self.xmpp.store

            def pop():
                # Each worker owns and closes its SQLAlchemy session, even on failure.
                with store.session.begin() as orm:
                    # A duplicate/unknown horizon must not consume newer messages.
                    if orm.scalar(select(ContactSent.id).where(
                        ContactSent.contact_id == contact_pk, ContactSent.msg_id == msg_id,
                    )) is None:
                        return []
                    ids = store.contacts.pop_sent_up_to(orm, contact_pk, msg_id)
                    return ids

            # Reuse the session's named locks: overlapping markers cannot pop twice.
            async with recipient.session.lock(("can-read-markers", contact_pk)):
                task = asyncio.create_task(asyncio.to_thread(pop))
                try:
                    return await asyncio.shield(task)
                except asyncio.CancelledError:
                    # A cancelled coroutine must not unlock while its SQL worker runs.
                    await asyncio.gather(task, return_exceptions=True)
                    raise
        else:
            return [msg_id]

    @exceptions_to_xmpp_errors
    async def on_message_displayed_synchronization_publish(
        self, msg: StanzaBase
    ) -> None:
        assert isinstance(msg, Message)
        chat_jid = JID(msg["pubsub_event"]["items"]["item"]["id"])
        if chat_jid.server != self.xmpp.boundjid.bare:
            return

        session = await self._get_session(msg, timeout=None)

        if chat_jid == self.xmpp.boundjid.bare:
            return

        recipient = await session.get_contact_or_group_or_participant(chat_jid)
        if not isinstance(recipient, LegacyMUC):
            session.log.debug("Ignoring non-groupchat MDS event")
            return

        stanza_id = msg["pubsub_event"]["items"]["item"]["displayed"]["stanza_id"]["id"]
        to_mark = await self.__to_mark(recipient, stanza_id)
        for xmpp_id in to_mark:
            await recipient.on_displayed(
                self._xmpp_msg_id_to_legacy(xmpp_id, recipient), None
            )
            ack = getattr(recipient, 'can_ack_read', None)
            if isinstance(recipient, LegacyMUC) and ack is not None:
                await ack(xmpp_id)

        # Do not feed reflected app-state publications back into WhatsApp.
        hook = getattr(recipient, 'on_chat_read', None)
        if to_mark and hook is not None:
            await hook(stanza_id)
