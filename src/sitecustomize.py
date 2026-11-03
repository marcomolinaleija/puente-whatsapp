from slidge.core.mixins.message import ChatStateMixin


def _chat_state_with_media(self, state, forced=False, **kwargs):
    media = kwargs.pop("media", "")
    carbon = kwargs.get("carbon", False)
    if carbon or (state == self._ChatStateMixin__last_chat_state and not forced):
        return
    self._ChatStateMixin__last_chat_state = state
    msg = self._make_message(state=state, hints={"no-store"})
    if media == "audio":
        for child in msg.xml:
            if child.tag == "{http://jabber.org/protocol/chatstates}composing":
                child.set("media", "audio")
                break
    self._send(msg, **kwargs)


ChatStateMixin._chat_state = _chat_state_with_media
