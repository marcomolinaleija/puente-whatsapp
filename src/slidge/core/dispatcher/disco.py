import logging
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
import sqlalchemy.orm
from slixmpp import JID
from slixmpp.exceptions import XMPPError
from slixmpp.plugins.xep_0004.stanza import Form
from slixmpp.plugins.xep_0030.stanza.info import DiscoInfo
from slixmpp.plugins.xep_0030.stanza.items import DiscoItems
from slixmpp.types import OptJid

from ...db.models import Room, Space
from ...util.types import AnySession
from .util import DispatcherMixin

if TYPE_CHECKING:
    from slidge.util.types import AnyGateway, AnySession


class DiscoMixin(DispatcherMixin):
    __slots__: list[str] = []

    def __init__(self, xmpp: "AnyGateway") -> None:
        super().__init__(xmpp)

        xmpp.plugin["xep_0030"].set_node_handler(
            "get_info",
            jid=None,
            node=None,
            handler=self.get_info,
        )

        xmpp.plugin["xep_0030"].set_node_handler(
            "get_items",
            jid=None,
            node=None,
            handler=self.get_items,
        )

    async def get_info(
        self,
        jid: OptJid,
        node: str | None,
        ifrom: OptJid,
        data: Any,  # noqa:ANN401
    ) -> DiscoInfo | None:
        # TODO: OMG the control flow in this method 🤮
        if ifrom == self.xmpp.boundjid.bare or (
            jid in (self.xmpp.boundjid.bare, None)
            and (not node or node.startswith("http://slixmpp.com/ver/"))
        ):
            return self.xmpp.plugin["xep_0030"].static.get_info(jid, node, ifrom, data)

        if ifrom is None:
            raise XMPPError("subscription-required")

        assert jid is not None
        session = await self._get_session_from_jid(jid=ifrom)

        if not jid.username:
            assert node is not None  # 🤮
            return await self.__spaces_info(session, node)

        log.debug("Looking for entity: %s", jid)

        entity = await session.get_contact_or_group_or_participant(jid)

        if entity is None:
            raise XMPPError("item-not-found")

        return await entity.get_disco_info(jid, node)

    async def __spaces_info(self, session: AnySession, node: str) -> DiscoInfo:
        legacy_id = await session.bookmarks.space_node_to_legacy_id(node)
        with self.xmpp.store.session(expire_on_commit=False) as orm:
            space = self.xmpp.store.spaces.get_by_legacy_id(
                orm, session.user_pk, legacy_id, full=True
            )
            if space is None:
                raise XMPPError("item-not-found", f"No space for node '{node}'")
        space = await session.bookmarks.update_space_if_needed(space)
        form = Form()
        form["type"] = "result"
        form.add_field(
            var="FORM_TYPE",
            ftype="hidden",
            value="http://jabber.org/protocol/pubsub#meta-data",
        )
        form.add_field(
            var="pubsub#type",
            value="urn:xmpp:spaces:0",
        )
        form.add_field(
            var="pubsub#creator",
            type="jid-single",
            value=self.xmpp.boundjid.bare
            if space.creator is None
            else str(space.creator.jid),
        )
        form.add_field(var="pubsub#title", value=space.name)
        form.add_field(
            var="pubsub#owner",
            type="jid-multi",
            value=[str(owner.jid) for owner in space.owners],
        )
        if space.member_count is not None:
            form.add_field(var="pubsub#num_subscribers", value=str(space.member_count))
        if space.description is not None:
            form.add_field(var="pubsub#description", value=space.description)
        info = DiscoInfo()
        info.add_identity(category="pubsub", itype="leaf")
        info.add_feature("http://jabber.org/protocol/pubsub")
        for feat in (
            "meta-data",
            "item-ids",
            "manage-subscriptions",
            "modify-affiliations",
            "outcast-affiliation",
            "retract-items",
            "retrieve-affiliations",
            "retrieve-items",
            "retrieve-subscriptions",
            "subscribe",
            "subscription-notifications",
        ):
            info.add_feature(f"http://jabber.org/protocol/pubsub#{feat}")
        info.append(form)
        return info

    async def get_items(
        self,
        jid: OptJid,
        node: str | None,
        ifrom: OptJid,
        data: Any,  # noqa:ANN401
    ) -> DiscoItems:
        if ifrom is None:
            raise XMPPError("bad-request")

        assert ifrom is not None
        session = await self._get_session_from_jid(ifrom)

        if jid == self.xmpp.boundjid.bare or not jid:
            if node:
                return DiscoItems()
            else:
                return await self.__list_rooms(session, data)

        entity = await session.get_contact_or_group_or_participant(jid)

        if entity is None:
            raise XMPPError("item-not-found")

        return await entity.get_disco_items(node)

    async def __list_rooms(
        self,
        session: "AnySession",
        data: dict,  # type:ignore[type-arg]
    ) -> DiscoItems:
        try:
            included_types = data["disco_items"]["filter"]["included_types"]
        except KeyError:
            included_types = []

        d = DiscoItems()
        with self.xmpp.store.session() as orm:
            if included_types == ["urn:xmpp:spaces:0"]:
                if not self.xmpp.SPACES:
                    raise XMPPError(
                        "feature-not-implemented",
                        "This gateway does not support XEP-0503",
                    )
                await self.__spaces(orm, session, d)
            elif not included_types:
                self.__rooms(orm, session, d)
            else:
                raise XMPPError("item-not-found", f"No items for {included_types}")

        return d

    async def __spaces(
        self, orm: sqlalchemy.orm.Session, session: AnySession, d: DiscoItems
    ) -> None:
        await session.bookmarks.update_spaces_info()
        for space in orm.execute(
            sa.select(Space)
            .options(sa.orm.load_only(Space.legacy_id, Space.name))
            .filter_by(user=session.user)
        ).scalars():
            d.add_item(
                JID(self.xmpp.boundjid.bare),
                name=space.name,
                node=await session.bookmarks.space_legacy_id_to_node(space.legacy_id),
            )

    @staticmethod
    def __rooms(
        orm: sqlalchemy.orm.Session, session: AnySession, d: DiscoItems
    ) -> None:
        for room in orm.execute(
            sa.select(Room)
            .options(sa.orm.load_only(Room.jid, Room.name))
            .filter_by(user=session.user)
            .order_by(Room.name)
        ).scalars():
            d.add_item(room.jid, name=room.name)


log = logging.getLogger(__name__)
