import uuid
from copy import copy
from datetime import UTC, datetime
from xml.etree import ElementTree as ET

from slixmpp import Message
from slixmpp.plugins.xep_0297.stanza import Forwarded
from slixmpp.xmlstream import StanzaBase

from ..util.util import fix_namespaces


def set_client_namespace(stanza: StanzaBase) -> None:
    fix_namespaces(stanza.xml, "jabber:component:accept", "jabber:client")


class HistoryMessage:
    def __init__(self, stanza: Message | str, when: datetime | None = None) -> None:
        if isinstance(stanza, str):
            from_db = True
            stanza = Message(xml=ET.fromstring(stanza))
        else:
            from_db = False

        self.id = stanza["stanza_id"]["id"] or uuid.uuid4().hex
        self.when: datetime = when or stanza["delay"]["stamp"] or datetime.now(tz=UTC)

        if not from_db:
            del stanza["delay"]
            del stanza["markable"]
            del stanza["hint"]
            del stanza["chat_state"]
            if not stanza["body"]:
                del stanza["body"]
            set_client_namespace(stanza)

        self.stanza: Message = stanza

    @property
    def stanza_component_ns(self) -> Message:
        stanza = copy(self.stanza)
        fix_namespaces(stanza.xml, old="jabber:client", new="jabber:component:accept")
        return stanza

    def forwarded(self) -> Forwarded:
        forwarded = Forwarded()
        forwarded["delay"]["stamp"] = self.when
        forwarded.append(self.stanza)
        return forwarded

    @property
    def occupant_id(self) -> str:
        return self.stanza["occupant-id"]["id"]  # type:ignore[no-any-return]
