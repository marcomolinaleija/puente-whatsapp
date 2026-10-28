import logging
from copy import copy
from pathlib import Path
from typing import ClassVar

from slixmpp import (
    JID,
    CoroutineCallback,
    ElementBase,
    Iq,
    Presence,
    StanzaPath,
    register_stanza_plugin,
)
from slixmpp.exceptions import IqError, IqTimeout, XMPPError
from slixmpp.plugins.base import BasePlugin, register_plugin
from slixmpp.plugins.xep_0060.stanza import Event, EventItem, EventItems, Item
from slixmpp.plugins.xep_0084.stanza import Data as AvatarData
from slixmpp.plugins.xep_0084.stanza import MetaData as AvatarMetadata
from slixmpp.plugins.xep_0172.stanza import UserNick
from slixmpp.plugins.xep_0292.stanza import VCard4
from slixmpp.plugins.xep_0402.stanza import Conference
from slixmpp.types import JidStr, OptJidStr

from ..contact import LegacyContact
from ..db.avatar import CachedAvatar, avatar_cache
from ..db.models import GatewayUser, Space
from ..util.lock import NamedLockMixin
from ..util.types import AnyGateway, AnySession

VCARD4_NAMESPACE = "urn:xmpp:vcard4"


class PepAvatar:
    def __init__(self) -> None:
        self.metadata: AvatarMetadata | None = None
        self.id: str | None = None
        self._avatar_data_path: Path | None = None

    @property
    def data(self) -> AvatarData | None:
        if self._avatar_data_path is None:
            return None
        data = AvatarData()
        data.set_value(self._avatar_data_path.read_bytes())
        return data

    def set_avatar_from_cache(self, cached_avatar: CachedAvatar) -> None:
        metadata = AvatarMetadata()
        self.id = cached_avatar.hash
        metadata.add_info(
            id=cached_avatar.hash,
            itype="image/png",
            ibytes=cached_avatar.path.stat().st_size,
            height=str(cached_avatar.height),
            width=str(cached_avatar.width),
        )
        self.metadata = metadata
        self._avatar_data_path = cached_avatar.path


class PubSubComponent(NamedLockMixin, BasePlugin):
    xmpp: "AnyGateway"

    name = "pubsub"
    description = "Pubsub component"
    dependencies: ClassVar[set[str]] = {
        "xep_0030",
        "xep_0060",
        "xep_0115",
        "xep_0163",
    }
    default_config: ClassVar[dict[str, str | None]] = {"component_name": None}
    component_name: str

    def __init__(self, *a: object, **kw: object) -> None:
        super().__init__(*a, **kw)
        # slixmpp usually does this via self.xmpp["xep_0163"].register_pep(),
        # in the session_bind hook of plugins. However, for components,
        # session_bind is never called.
        register_stanza_plugin(EventItem, UserNick)
        register_stanza_plugin(EventItem, Conference)

    def plugin_init(self) -> None:
        self.xmpp.register_handler(
            CoroutineCallback(
                "pubsub_get_avatar_data",
                StanzaPath(f"iq@type=get/pubsub/items@node={AvatarData.namespace}"),
                self._get_avatar_data,  # type:ignore
            )
        )
        self.xmpp.register_handler(
            CoroutineCallback(
                "pubsub_get_avatar_metadata",
                StanzaPath(f"iq@type=get/pubsub/items@node={AvatarMetadata.namespace}"),
                self._get_avatar_metadata,  # type:ignore
            )
        )
        self.xmpp.register_handler(
            CoroutineCallback(
                "pubsub_get_vcard",
                StanzaPath(f"iq@type=get/pubsub/items@node={VCARD4_NAMESPACE}"),
                self._get_vcard,  # type:ignore
            )
        )

        disco = self.xmpp.plugin["xep_0030"]
        disco.add_identity("pubsub", "pep", self.component_name)
        disco.add_identity("account", "registered", self.component_name)
        disco.add_feature("http://jabber.org/protocol/pubsub#event")
        disco.add_feature("http://jabber.org/protocol/pubsub#retrieve-items")
        disco.add_feature("http://jabber.org/protocol/pubsub#persistent-items")

    async def __get_features(self, presence: Presence) -> list[str]:
        from_ = presence.get_from()
        ver_string = presence["caps"]["ver"]
        if ver_string:
            info = await self.xmpp.plugin["xep_0115"].get_caps(from_)
        else:
            info = None
        if info is None:
            async with self.lock(from_):
                try:
                    iq = await self.xmpp.plugin["xep_0030"].get_info(from_)
                except (IqError, IqTimeout):
                    log.debug("Could not get disco#info of %s, ignoring", from_)
                    return []
            info = iq["disco_info"]
        return info["features"]  # type:ignore[no-any-return]

    async def on_presence_available(
        self, p: Presence, contact: LegacyContact | None
    ) -> None:
        if "muc_join" in p:
            log.debug("Ignoring MUC presence here")
            return

        to = p.get_to()
        # we don't want to push anything for contacts that are not in the user's roster
        if to != self.xmpp.boundjid.bare and (contact is None or not contact.is_friend):
            return

        from_ = p.get_from()
        features = await self.__get_features(p)

        if AvatarMetadata.namespace + "+notify" in features:
            try:
                pep_avatar = await self._get_authorized_avatar(p, contact)
            except XMPPError:
                pass
            else:
                if pep_avatar.metadata is not None:
                    await self.__broadcast(
                        data=pep_avatar.metadata,
                        from_=p.get_to().bare,
                        to=from_,
                        id=pep_avatar.metadata["info"]["id"],
                    )
        if UserNick.namespace + "+notify" in features:
            try:
                pep_nick = await self._get_authorized_nick(p, contact)
            except XMPPError:
                pass
            else:
                await self.__broadcast(data=pep_nick, from_=p.get_to(), to=from_)

        if contact is not None and VCARD4_NAMESPACE + "+notify" in features:
            vcard = await contact.get_vcard()
            if vcard is not None:
                await self.broadcast_vcard_event(p.get_to(), from_, vcard)

    async def broadcast_vcard_event(self, from_: JID, to: JID, vcard: VCard4) -> None:
        item = Item()
        item.namespace = VCARD4_NAMESPACE
        item["id"] = "current"
        # vcard: VCard4 = await self.xmpp["xep_0292_provider"].get_vcard(from_, to)
        # The vcard content should NOT be in this event according to the spec:
        # https://xmpp.org/extensions/xep-0292.html#sect-idm45669698174224
        # but movim expects it to be here, and I guess it does not hurt

        log.debug("Broadcast vcard4 event: %s", vcard)
        await self.__broadcast(
            data=vcard,
            from_=JID(from_).bare,
            to=to,
            id="current",
            node=VCARD4_NAMESPACE,
        )

    async def __get_contact(self, stanza: Iq | Presence) -> LegacyContact:
        session = self.xmpp.get_session_from_stanza(stanza)
        return await session.contacts.by_jid(stanza.get_to())  # type:ignore[no-any-return]

    async def _get_authorized_avatar(
        self, stanza: Iq | Presence, contact: LegacyContact | None = None
    ) -> PepAvatar:
        if stanza.get_to() == self.xmpp.boundjid.bare:
            item = PepAvatar()
            if self.xmpp.avatar is not None:
                item.set_avatar_from_cache(self.xmpp.avatar)
            return item

        if contact is None:
            contact = await self.__get_contact(stanza)

        item = PepAvatar()
        if contact.stored.avatar is not None:
            stored = avatar_cache.get(contact.stored.avatar)
            assert stored is not None
            item.set_avatar_from_cache(stored)
        return item

    async def _get_authorized_nick(
        self, stanza: Iq | Presence, contact: LegacyContact | None = None
    ) -> UserNick:
        if stanza.get_to() == self.xmpp.boundjid.bare:
            return get_user_nick(self.xmpp.COMPONENT_NAME)

        if contact is None:
            contact = await self.__get_contact(stanza)

        if contact.name is not None:
            return get_user_nick(contact.name)
        else:
            return UserNick()

    def __reply_with(
        self, iq: Iq, content: AvatarData | AvatarMetadata | None, item_id: str | None
    ) -> None:
        requested_items = iq["pubsub"]["items"]

        if len(requested_items) == 0:
            self._reply_with_payload(iq, content, item_id)
        else:
            for item in requested_items:
                if item["id"] == item_id:
                    self._reply_with_payload(iq, content, item_id)
                    return
            else:
                raise XMPPError("item-not-found")

    async def _get_avatar_data(self, iq: Iq) -> None:
        pep_avatar = await self._get_authorized_avatar(iq)
        self.__reply_with(iq, pep_avatar.data, pep_avatar.id)

    async def _get_avatar_metadata(self, iq: Iq) -> None:
        pep_avatar = await self._get_authorized_avatar(iq)
        self.__reply_with(iq, pep_avatar.metadata, pep_avatar.id)

    async def _get_vcard(self, iq: Iq) -> None:
        # this is not the proper way that clients should retrieve VCards, but
        # gajim does it this way.
        # https://xmpp.org/extensions/xep-0292.html#sect-idm45669698174224
        session = self.xmpp.get_session_from_stanza(iq)
        contact = await session.contacts.by_jid(iq.get_to())
        vcard = await contact.get_vcard()
        if vcard is None:
            raise XMPPError("item-not-found")
        self._reply_with_payload(iq, vcard, "current", VCARD4_NAMESPACE)

    @staticmethod
    def _reply_with_payload(
        iq: Iq,
        payload: AvatarMetadata | AvatarData | VCard4 | None,
        id_: str | None,
        namespace: str | None = None,
    ) -> None:
        result = iq.reply()
        item = Item()
        if payload:
            item.set_payload(payload.xml)
            item["id"] = id_
            result["pubsub"]["items"]["node"] = (
                namespace if namespace else payload.namespace
            )
        result["pubsub"]["items"].append(item)
        result.send()

    async def __broadcast(
        self,
        data: ElementBase | None,
        from_: JidStr,
        to: OptJidStr = None,
        items: EventItems | None = None,
        **kwargs: object,
    ) -> None:
        from_ = JID(from_)
        if from_ != self.xmpp.boundjid.bare and to is not None:
            to = JID(to)
            session = self.xmpp.get_session_from_jid(to)
            if session is None:
                return
            await session.ready

        item = EventItem()
        if data:
            item.set_payload(data.xml)
        for k, v in kwargs.items():
            item[k] = v

        if items is None:
            items = EventItems()
            items.append(item)
            assert data is not None
            items["node"] = kwargs.get("node") or data.namespace

        event = Event()
        event.append(items)

        msg = self.xmpp.Message()
        msg.set_type("headline")
        msg.set_from(from_)
        msg.append(event)

        if to is None:
            with self.xmpp.store.session() as orm:
                for u in orm.query(GatewayUser).all():
                    new_msg = copy(msg)
                    new_msg.set_to(u.jid.bare)
                    new_msg.send()
        else:
            msg.set_to(to)
            msg.send()

    async def broadcast_avatar(
        self, from_: JidStr, to: JidStr, cached_avatar: CachedAvatar | None
    ) -> None:
        if cached_avatar is None:
            await self.__broadcast(AvatarMetadata(), from_, to)
        else:
            pep_avatar = PepAvatar()
            pep_avatar.set_avatar_from_cache(cached_avatar)
            assert pep_avatar.metadata is not None
            await self.__broadcast(
                pep_avatar.metadata, from_, to, id=pep_avatar.metadata["info"]["id"]
            )

    def broadcast_nick(
        self,
        user_jid: JID,
        jid: JidStr,
        nick: str | None = None,
    ) -> None:
        jid = JID(jid)
        nickname = get_user_nick(nick)
        log.debug("New nickname: %s", nickname)
        self.xmpp.loop.create_task(self.__broadcast(nickname, jid, user_jid.bare))

    def broadcast_space(self, session: AnySession, space: Space) -> None:
        items = EventItems()
        items["node"] = str(space.legacy_id)
        for room in space.rooms:
            item = EventItem()
            item["id"] = room.jid
            item.enable("conference")
            if room.name:
                item["conference"]["name"] = room.name
            items.append(item)
        session.create_task(
            self.__broadcast(
                None,
                self.xmpp.boundjid.bare,
                session.user_jid,
                items=items,
            )
        )

    def broadcast_space_metadata(
        self, session: AnySession, space: Space, node: str
    ) -> None:
        msg = self.xmpp.make_message(
            mtype="headline", mto=session.user_jid, mfrom=self.xmpp.boundjid.bare
        )
        msg["pubsub_event"]["configuration"]["node"] = node

        form = msg["pubsub_event"]["configuration"]["form"]
        form["type"] = "result"

        form.add_field(
            var="FORM_TYPE",
            ftype="hidden",
            value="http://jabber.org/protocol/pubsub#node_config",
        )
        form.add_field(var="pubsub#title", value=space.name)

        # MUSTs
        form.add_field(var="pubsub#type", value="urn:xmpp:spaces:0")
        form.add_field(var="pubsub#notify_retract", value="true")
        form.add_field(var="pubsub#persist_items", value="true")
        form.add_field(var="pubsub#purge_offline", value="false")
        # SHOULDs
        form.add_field(var="pubsub#notify_sub", value="true")
        form.add_field(var="pubsub#notify_config", value="true")
        form.add_field(var="pubsub#notify_delete", value="true")
        form.add_field(var="pubsub#publish_model", value="publishers")
        # MUST (private spaces)
        form.add_field(var="pubsub#access_model", value="authorize")

        msg.send()


def get_user_nick(nick: str | None = None) -> UserNick:
    user_nick = UserNick()
    if nick is not None:
        user_nick["nick"] = nick
    return user_nick


log = logging.getLogger(__name__)
register_plugin(PubSubComponent)
