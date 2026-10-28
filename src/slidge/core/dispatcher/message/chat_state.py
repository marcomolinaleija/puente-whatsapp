from typing import TYPE_CHECKING

from slixmpp import Message
from slixmpp.xmlstream import StanzaBase

from ..util import DispatcherMixin, exceptions_to_xmpp_errors

RECORDING_AUDIO_NS = "urn:marco-ml:whatsapp:recording:0"

if TYPE_CHECKING:
    from slidge.util.types import AnyGateway


class ChatStateMixin(DispatcherMixin):
    __slots__: list[str] = []

    def __init__(self, xmpp: "AnyGateway") -> None:
        super().__init__(xmpp)
        xmpp.add_event_handler("chatstate", self.on_chatstate)

    @exceptions_to_xmpp_errors
    async def on_chatstate(self, msg: StanzaBase) -> None:
        assert isinstance(msg, Message)
        if msg["body"]:
            return
        recipient, thread = await self._get_recipient_and_thread(msg)
        state = msg["chat_state"]
        if msg.xml.find(f"{{{RECORDING_AUDIO_NS}}}recording") is not None:
            state = "recording_audio"
        await recipient.on_chat_state(state, thread)
