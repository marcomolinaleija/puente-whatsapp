import typing

from slixmpp import JID, CoroutineCallback, Iq, StanzaPath
from slixmpp.exceptions import XMPPError
from slixmpp.xmlstream import StanzaBase

from ..util import DispatcherMixin, exceptions_to_xmpp_errors

if typing.TYPE_CHECKING:
    from slidge.util.types import AnyGateway


class MucAdminMixin(DispatcherMixin):
    __slots__: list[str] = []

    def __init__(self, xmpp: "AnyGateway") -> None:
        super().__init__(xmpp)
        self.xmpp.register_handler(
            CoroutineCallback(
                "MUCModerate",
                StanzaPath("iq/moderate"),
                self.on_user_moderation,
            )
        )
        self.xmpp.register_handler(
            CoroutineCallback(
                "MUCSetAffiliation",
                StanzaPath("iq@type=set/mucadmin_query"),
                self.on_user_set_affiliation,
            )
        )
        self.xmpp.register_handler(
            CoroutineCallback(
                "MUCGetAffiliation",
                StanzaPath("iq@type=get/mucadmin_query"),
                self.on_muc_admin_query_get,
            )
        )

    @exceptions_to_xmpp_errors
    async def on_user_moderation(self, iq: StanzaBase) -> None:
        assert isinstance(iq, Iq)
        muc = await self.get_muc_from_stanza(iq)

        moderate = iq["moderate"]
        xmpp_id = iq["moderate"]["id"]
        if not xmpp_id:
            raise XMPPError("bad-request", "Missing moderated message ID")

        if not moderate["retract"]:
            raise XMPPError(
                "feature-not-implemented",
                "Slidge only implements moderation/retraction",
            )

        legacy_id = self._xmpp_msg_id_to_legacy(xmpp_id, muc)
        await muc.on_moderate(legacy_id, moderate["reason"] or None)
        iq.reply(clear=True).send()

    @exceptions_to_xmpp_errors
    async def on_user_set_affiliation(self, iq: StanzaBase) -> None:
        assert isinstance(iq, Iq)
        muc = await self.get_muc_from_stanza(iq)

        item = iq["mucadmin_query"]["item"]
        if item["jid"]:
            contact = await muc.session.contacts.by_jid(JID(item["jid"]))
        else:
            part = await muc.get_participant(
                item["nick"], fill_first=True, create=False
            )
            if part is None:
                raise XMPPError("item-not-found")
            assert part.contact is not None
            contact = part.contact

        if item["affiliation"]:
            part = await muc.get_participant_by_contact(contact)
            await part.on_set_affiliation(
                item["affiliation"],
                item["reason"] or None,
                item["nick"] or None,
            )
        elif item["role"] == "none":
            part = await muc.get_participant_by_contact(contact)
            await part.on_kick(item["reason"] or None)

        iq.reply(clear=True).send()

    @exceptions_to_xmpp_errors
    async def on_muc_admin_query_get(self, iq: StanzaBase) -> None:
        assert isinstance(iq, Iq)
        affiliation = iq["mucadmin_query"]["item"]["affiliation"]

        if not affiliation:
            raise XMPPError("bad-request")

        session = await self._get_session(iq, 1, logged=True)
        muc = await session.bookmarks.by_jid(iq.get_to())

        reply = iq.reply()
        reply.enable("mucadmin_query")
        async for participant in muc.get_participants(affiliation):
            reply["mucadmin_query"].append(participant.mucadmin_item())
        reply.send()
