from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, ClassVar

from slixmpp.plugins.xep_0004.stanza.form import Form
from slixmpp.plugins.xep_0030.stanza.info import DiscoInfo
from slixmpp.plugins.xep_0030.stanza.items import DiscoItems
from slixmpp.types import OptJid

from .base import Base

if TYPE_CHECKING:
    from slidge.command.base import ContactCommand, MUCCommand


class BaseDiscoMixin(Base):
    DISCO_TYPE: str = NotImplemented
    DISCO_CATEGORY: str = NotImplemented
    DISCO_NAME: str = NotImplemented
    DISCO_LANG = None

    commands: ClassVar[
        Mapping[str, "type[ContactCommand[Any]] | type[MUCCommand[Any]]"]
    ]

    def _get_disco_name(self) -> str | None:
        if self.DISCO_NAME is NotImplemented:
            return self.xmpp.COMPONENT_NAME
        return self.DISCO_NAME or self.xmpp.COMPONENT_NAME

    def features(self) -> list[str]:
        return []

    async def extended_features(self) -> list[Form] | None:
        return None

    async def get_disco_info(
        self, jid: OptJid = None, node: str | None = None
    ) -> DiscoInfo:
        info = DiscoInfo()
        if node == "http://jabber.org/protocol/commands":
            info.add_identity(category="automation", itype="command-list")
        elif node and node in self.commands:
            info.add_identity(
                category="automation",
                itype="command-node",
                name=self.commands[node].NAME,
            )
            info.add_feature("http://jabber.org/protocol/commands")
            info.add_feature("jabber:x:data")
        else:
            for feature in self.features():
                info.add_feature(feature)
            info.add_identity(
                category=self.DISCO_CATEGORY,
                itype=self.DISCO_TYPE,
                name=self._get_disco_name(),
                lang=self.DISCO_LANG,
            )
            if forms := await self.extended_features():
                for form in forms:
                    info.append(form)
        return info

    async def get_caps_ver(self, jid: OptJid = None, node: str | None = None) -> str:
        info = await self.get_disco_info(jid, node)
        caps = self.xmpp.plugin["xep_0115"]
        ver = caps.generate_verstring(info, caps.hash)
        return ver  # type:ignore[no-any-return]

    async def get_disco_items(self, node: str | None) -> DiscoItems:
        items = DiscoItems()
        if node == "http://jabber.org/protocol/commands":
            for node, command in self.commands.items():
                items.add_item(jid=self.jid, node=node, name=command.NAME)

        return items


class ChatterDiscoMixin(BaseDiscoMixin):
    AVATAR = True
    RECEIPTS = True
    MARKS = True
    CHAT_STATES = True
    UPLOAD = True
    CORRECTION = True
    REACTION = True
    RETRACTION = True
    REPLIES = True
    INVITATION_RECIPIENT = False

    DISCO_TYPE = "pc"
    DISCO_CATEGORY = "client"
    DISCO_NAME = ""

    is_participant: bool

    def features(self) -> list[str]:
        features = []
        if self.CHAT_STATES:
            features.append("http://jabber.org/protocol/chatstates")
        if self.RECEIPTS:
            features.append("urn:xmpp:receipts")
        if self.CORRECTION:
            features.append("urn:xmpp:message-correct:0")
        if self.MARKS:
            features.append("urn:xmpp:chat-markers:0")
        if self.UPLOAD:
            features.append("jabber:x:oob")
        if self.REACTION:
            features.append("urn:xmpp:reactions:0")
        if self.RETRACTION:
            features.append("urn:xmpp:message-retract:0")
        if self.REPLIES:
            features.append("urn:xmpp:reply:0")
        if self.INVITATION_RECIPIENT:
            features.append("jabber:x:conference")
        features.append("urn:ietf:params:xml:ns:vcard-4.0")
        if not self.is_participant:
            features.append("http://jabber.org/protocol/commands")
        return features

    async def extended_features(self) -> list[Form] | None:
        f = getattr(self, "restricted_emoji_extended_feature", None)
        if f is None:
            return None

        e = await f()
        if not e:
            return None

        return [e]


class ContactAccountDiscoMixin(BaseDiscoMixin):
    async def get_disco_info(
        self, jid: OptJid = None, node: str | None = None
    ) -> DiscoInfo:
        if jid and jid.resource:
            return await super().get_disco_info(jid, node)
        info = DiscoInfo()
        info.add_feature("http://jabber.org/protocol/pubsub")
        info.add_feature("http://jabber.org/protocol/pubsub#retrieve-items")
        info.add_feature("http://jabber.org/protocol/pubsub#subscribe")
        info.add_identity(
            category="account",
            itype="registered",
            name=self._get_disco_name(),
            lang=self.DISCO_LANG,
        )
        info.add_identity(
            category="pubsub",
            itype="pep",
            name=self._get_disco_name(),
            lang=self.DISCO_LANG,
        )
        return info
