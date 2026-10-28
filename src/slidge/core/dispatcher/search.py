from typing import TYPE_CHECKING

from slixmpp import JID, CoroutineCallback, Iq, StanzaPath
from slixmpp.exceptions import XMPPError

from .util import DispatcherMixin, exceptions_to_xmpp_errors

if TYPE_CHECKING:
    from slidge.util.types import AnyGateway


class SearchMixin(DispatcherMixin):
    __slots__: list[str] = []

    def __init__(self, xmpp: "AnyGateway") -> None:
        super().__init__(xmpp)

        xmpp.plugin["xep_0055"].api.register(self.search_get_form, "search_get_form")  # type:ignore[arg-type]
        xmpp.plugin["xep_0055"].api.register(self._search_query, "search_query")  # type:ignore[arg-type]

        xmpp.plugin["xep_0030"].add_feature("jabber:iq:gateway")
        xmpp.register_handler(
            CoroutineCallback(
                "iq:gateway",
                StanzaPath("iq/gateway"),
                self._handle_gateway_iq,
            )
        )

    async def search_get_form(
        self,
        _gateway_jid: JID,
        _node: str,
        ifrom: JID,
        iq: Iq,
    ) -> Iq:
        """
        Prepare the search form using :attr:`.BaseSession.SEARCH_FIELDS`
        """
        await self._get_session(iq)

        xmpp = self.xmpp

        reply = iq.reply()
        form = reply["search"]["form"]
        form["title"] = xmpp.SEARCH_TITLE
        form["instructions"] = xmpp.SEARCH_INSTRUCTIONS
        for field in xmpp.SEARCH_FIELDS:
            form.append(field.get_xml())
        return reply  # type:ignore[no-any-return]

    async def _search_query(
        self,
        _gateway_jid: JID,
        _node: str,
        ifrom: JID,
        iq: Iq,
    ) -> Iq:
        """
        Handles a search request
        """
        session = await self._get_session(iq)

        result = await session.on_search(iq["search"]["form"].get_values())

        if not result:
            raise XMPPError("item-not-found", text="Nothing was found")

        reply = iq.reply()
        form = reply["search"]["form"]
        for field in result.fields:
            form.add_reported(field.var, label=field.label, type=field.type)
        for item in result.items:
            form.add_item(item)
        return reply  # type:ignore[no-any-return]

    @exceptions_to_xmpp_errors
    async def _handle_gateway_iq(self, iq: Iq) -> None:
        if iq.get_to() != self.xmpp.boundjid.bare:
            raise XMPPError("bad-request", "This can only be used on the component JID")

        if len(self.xmpp.SEARCH_FIELDS) > 1:
            raise XMPPError(
                "feature-not-implemented", "Use jabber search for this gateway"
            )

        session = await self._get_session(iq)
        field = self.xmpp.SEARCH_FIELDS[0]

        reply = iq.reply()
        if iq["type"] == "get":
            reply["gateway"]["desc"] = self.xmpp.SEARCH_TITLE
            reply["gateway"]["prompt"] = field.label
        elif iq["type"] == "set":
            prompt = iq["gateway"]["prompt"]
            result = await session.on_search({field.var: prompt})
            if result is None or not result.items:
                raise XMPPError(
                    "item-not-found", "No contact was found with the info you provided."
                )
            if len(result.items) > 1:
                raise XMPPError(
                    "bad-request", "Your search yielded more than one result."
                )
            reply["gateway"]["jid"] = result.items[0]["jid"]

        reply.send()
