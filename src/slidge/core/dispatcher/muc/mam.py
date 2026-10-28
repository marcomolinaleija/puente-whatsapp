import asyncio
from typing import TYPE_CHECKING

from slixmpp import CoroutineCallback, Iq, StanzaPath
from slixmpp.exceptions import XMPPError
from slixmpp.xmlstream import StanzaBase

from ... import config
from ..util import DispatcherMixin, exceptions_to_xmpp_errors

if TYPE_CHECKING:
    from typing import Any

    from slidge import BaseGateway


class MamMixin(DispatcherMixin):
    __slots__: list[str] = []

    def __init__(self, xmpp: "BaseGateway[Any]") -> None:
        super().__init__(xmpp)
        self.__mam_cleanup_task = xmpp.loop.create_task(self.__mam_cleanup())  # type:ignore[misc]
        xmpp.register_handler(
            CoroutineCallback(
                "MAM_query",
                StanzaPath("iq@type=set/mam"),
                self.__handle_mam,
            )
        )
        xmpp.register_handler(
            CoroutineCallback(
                "MAM_get_from",
                StanzaPath("iq@type=get/mam"),
                self.__handle_mam_get_form,
            )
        )
        xmpp.register_handler(
            CoroutineCallback(
                "MAM_get_meta",
                StanzaPath("iq@type=get/mam_metadata"),
                self.__handle_mam_metadata,
            )
        )

    async def __mam_cleanup(self) -> None:
        if not config.MAM_MAX_DAYS:
            return
        while True:
            await asyncio.sleep(3600 * 6)
            with self.xmpp.store.session() as orm:
                self.xmpp.store.mam.nuke_older_than(orm, config.MAM_MAX_DAYS)
                orm.commit()

    @exceptions_to_xmpp_errors
    async def __handle_mam(self, iq: Iq) -> None:
        muc = await self.get_muc_from_stanza(iq)
        await muc.send_mam(iq)

    async def __handle_mam_get_form(self, iq: StanzaBase) -> None:
        assert isinstance(iq, Iq)
        ito = iq.get_to()

        if ito == self.xmpp.boundjid.bare:
            raise XMPPError(
                text="No MAM on the component itself, use a JID with a resource"
            )

        session = await self._get_session(iq, 0, logged=True)
        await session.bookmarks.by_jid(ito)

        reply = iq.reply()
        form = self.xmpp.plugin["xep_0004"].make_form()
        form.add_field(ftype="hidden", var="FORM_TYPE", value="urn:xmpp:mam:2")
        form.add_field(ftype="jid-single", var="with")
        form.add_field(ftype="text-single", var="start")
        form.add_field(ftype="text-single", var="end")
        form.add_field(ftype="text-single", var="before-id")
        form.add_field(ftype="text-single", var="after-id")
        form.add_field(ftype="boolean", var="include-groupchat")
        field = form.add_field(ftype="list-multi", var="ids")
        field["validate"]["datatype"] = "xs:string"
        field["validate"]["open"] = True
        reply["mam"].append(form)
        reply.send()

    @exceptions_to_xmpp_errors
    async def __handle_mam_metadata(self, iq: Iq) -> None:
        muc = await self.get_muc_from_stanza(iq)
        await muc.send_mam_metadata(iq)
