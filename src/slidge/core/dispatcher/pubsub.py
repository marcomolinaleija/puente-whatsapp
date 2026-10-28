from slixmpp import CoroutineCallback, Iq, StanzaPath
from slixmpp.exceptions import XMPPError
from slixmpp.plugins.xep_0060.stanza import Affiliation, Item, Subscription
from slixmpp.plugins.xep_0084.stanza import Data as AvatarData
from slixmpp.plugins.xep_0084.stanza import MetaData as AvatarMetadata

from slidge.db.models import Space

from ...util.types import AnyGateway, AnySession
from .util import DispatcherMixin, exceptions_to_xmpp_errors


class PubSubMixin(DispatcherMixin):
    __slots__: list[str] = []

    def __init__(self, xmpp: AnyGateway) -> None:
        super().__init__(xmpp)

        for path, func in [
            ("get/pubsub/items", self.__get_items),
            ("set/pubsub/subscribe", self.__set_subscribe),
            ("set/pubsub/unsubscribe", self.__set_unsubscribe),
            ("get/pubsub/affiliations", self.__get_affiliations),
            ("get/pubsub_owner/affiliations", self.__owner_get_affiliations),
            ("get/pubsub/subscriptions", self.__get_subscriptions),
            ("get/pubsub_owner/subscriptions", self.__owner_get_subscriptions),
        ]:
            self.xmpp.register_handler(
                CoroutineCallback(
                    func.__name__,
                    StanzaPath(f"iq@to={self.xmpp.boundjid.bare}@type={path}"),
                    func,  # type:ignore[arg-type]  # ty:ignore[invalid-argument-type]
                )
            )

    async def __get_items(self, iq: Iq) -> None:
        node = iq["pubsub"]["items"]["node"]

        if node in (AvatarData.namespace, AvatarMetadata.namespace, "urn:xmpp:vcard4"):
            # handled in slidge/core/pubsub.py
            # TODO: have a single entrypoint for all get/pubsub/items
            return

        session = await self._get_session(iq, logged=True)
        legacy_id = await self.__get_legacy_id(session, node)
        item_ids = [item["id"] for item in iq["pubsub"]["items"]["items"]]

        # TODO: support for avatar and banner items here
        if not all(
            item_id.endswith(f"@{self.xmpp.boundjid.bare}") for item_id in item_ids
        ):
            raise XMPPError(
                "item-not-found",
                "Slidge only supports items that are MUCs.",
            )

        if item_ids:
            room_legacy_ids = [
                str(
                    await session.bookmarks.jid_local_part_to_legacy_id(x.split("@")[0])
                )
                for x in item_ids
            ]
        else:
            room_legacy_ids = []

        with self.xmpp.store.session() as orm:
            rooms = self.xmpp.store.spaces.get_rooms(
                orm, session.user_pk, str(legacy_id), room_legacy_ids
            )

        if item_ids and len(rooms) != len(item_ids):
            raise XMPPError(
                "item-not-found",
                f"Could not find items: {set(item_ids) - set(str(r.jid) for r in rooms)}",
            )

        reply = iq.reply()
        reply["pubsub"]["items"]["node"] = node
        for room in rooms:
            item = Item()
            item["id"] = room.jid
            item["conference"]["name"] = room.name
            reply["pubsub"]["items"].append(item)

        reply.send()

    async def __get_legacy_id(self, session: AnySession, node: str) -> object:
        try:
            legacy_id = await session.bookmarks.space_node_to_legacy_id(node)
        except Exception as e:
            raise XMPPError("item-not-found", str(e))
        with self.xmpp.store.session() as orm:
            if not self.xmpp.store.spaces.exists(orm, session.user_pk, str(legacy_id)):
                raise XMPPError(
                    "item-not-found", f"This is not a known space: '{legacy_id}'"
                )
        return legacy_id

    async def __set_subscribe(self, iq: Iq) -> None:
        node = iq["pubsub"]["subscribe"]["node"]

        session = await self._get_session(iq, logged=True)
        await self.__get_legacy_id(session, node)

        reply = iq.reply(clear=True)
        sub = reply["pubsub"]["subscription"]
        sub["node"] = node
        sub["jid"] = session.user_jid
        sub["subscription"] = "subscribed"
        reply.send()

    @exceptions_to_xmpp_errors
    async def __set_unsubscribe(self, iq: Iq) -> None:
        session = await self._get_session(iq, logged=True)

        jid = iq["pubsub"]["unsubscribe"]["jid"]
        if jid != session.user_jid.bare:
            raise XMPPError("bad-request", f"Cannot unsubscribe JID {jid}")

        node = iq["pubsub"]["unsubscribe"]["node"]
        legacy_id = await session.bookmarks.space_node_to_legacy_id(node)

        await session.on_leave_space(legacy_id)
        reply = iq.reply(clear=True)

        sub = reply["pubsub"]["subscription"]
        sub["node"] = node
        sub["jid"] = session.user_jid
        sub["subscription"] = "none"
        reply.send()

    async def __get_affiliations(self, iq: Iq) -> None:
        session = await self._get_session(iq, logged=True)

        reply = iq.reply()
        node = iq["pubsub"]["affiliations"]["node"]
        if node:
            legacy_id = await self.__get_legacy_id(session, node)
            with self.xmpp.store.session() as orm:
                if not self.xmpp.store.spaces.exists(
                    orm, session.user_pk, str(legacy_id)
                ):
                    raise XMPPError("item-not-found", f"Space '{legacy_id}' not found")
            affiliation = reply["pubsub"]["affiliations"]["affiliation"]
            affiliation["node"] = node
            affiliation["affiliation"] = "subscriber"
        else:
            with self.xmpp.store.session() as orm:
                spaces = list(self.xmpp.store.spaces.get_all(orm, session.user_pk))
            for space in spaces:
                reply["pubsub"]["affiliations"].append(
                    await self.__make_affiliation(session, space)
                )

        reply.send()

    @staticmethod
    async def __make_affiliation(session: AnySession, space: Space) -> Affiliation:
        affiliation = Affiliation()
        node = await session.bookmarks.space_legacy_id_to_node(space.legacy_id)
        affiliation["node"] = node
        affiliation["affiliation"] = "subscriber"
        return affiliation

    async def __owner_get_affiliations(self, iq: Iq) -> None:
        raise XMPPError(
            "forbidden", "Slidge does not implement managing space affiliations."
        )

    async def __owner_get_subscriptions(self, iq: Iq) -> None:
        node = iq["pubsub_owner"]["subscriptions"]["node"]
        if not node:
            raise XMPPError("bad-request", "No node was specified")

        session = await self._get_session(iq, logged=True)
        await self.__get_legacy_id(session, node)

        reply = iq.reply(clear=False)
        sub = reply["pubsub_owner"]["subscriptions"]["subscription"]
        sub["jid"] = session.user_jid
        sub["subscription"] = "subscribed"
        reply.send()

    async def __get_subscriptions(self, iq: Iq) -> None:
        _node = iq["pubsub"]["subscriptions"]["node"]

        session = await self._get_session(iq, logged=True)

        reply = iq.reply(clear=False)
        subscriptions = reply["pubsub"]["subscriptions"]
        if node := iq["pubsub"]["subscriptions"]["node"]:
            await self.__get_legacy_id(session, node)
            subscription = subscriptions["subscription"]
            subscription["node"] = node
            subscription["jid"] = session.user_jid
            subscription["subscription"] = "subscribed"
        else:
            with self.xmpp.store.session() as orm:
                spaces = list(self.xmpp.store.spaces.get_all(orm, session.user_pk))
            for space in spaces:
                subscriptions.append(await self.__make_subscription(session, space))

        reply.send()

    @staticmethod
    async def __make_subscription(session: AnySession, space: Space) -> Subscription:
        subscription = Subscription()
        node = await session.bookmarks.space_legacy_id_to_node(space.legacy_id)
        subscription["node"] = node
        subscription["jid"] = session.user_jid
        subscription["subscription"] = "subscribed"
        return subscription
