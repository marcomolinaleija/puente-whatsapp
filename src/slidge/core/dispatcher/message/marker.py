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
            return recipient.pop_unread_xmpp_ids_up_to(msg_id)
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
