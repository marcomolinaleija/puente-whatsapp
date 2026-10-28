import logging
import uuid
import warnings

from slixmpp import Iq, Message
from slixmpp.plugins.xep_0004.stanza.form import Form

from ...util.types import AnyMUC, ChatState, Marker
from .attachment import AttachmentMixin
from .message_maker import MessageMaker
from .message_text import TextMessageMixin

# this is for MDS
PUBLISH_OPTIONS = Form()
PUBLISH_OPTIONS["type"] = "submit"
PUBLISH_OPTIONS.add_field(
    "FORM_TYPE", "hidden", value="http://jabber.org/protocol/pubsub#publish-options"
)
PUBLISH_OPTIONS.add_field("pubsub#persist_items", value="true")
PUBLISH_OPTIONS.add_field("pubsub#max_items", value="max")
PUBLISH_OPTIONS.add_field("pubsub#send_last_published_item", value="never")
PUBLISH_OPTIONS.add_field("pubsub#access_model", value="whitelist")


class ChatStateMixin(MessageMaker):
    def __init__(self) -> None:
        super().__init__()
        self.__last_chat_state: ChatState | None = None

    def _chat_state(
        self, state: ChatState, forced: bool = False, **kwargs: object
    ) -> None:
        carbon = kwargs.get("carbon", False)
        if carbon or (state == self.__last_chat_state and not forced):
            return
        self.__last_chat_state = state
        msg = self._make_message(state=state, hints={"no-store"})
        self._send(msg, **kwargs)

    def active(self, **kwargs: object) -> None:
        """
        Send an "active" chat state (:xep:`0085`) from this
        :term:`XMPP Entity`.
        """
        self._chat_state("active", forced=False, **kwargs)

    def composing(self, **kwargs: object) -> None:
        """
        Send a "composing" (ie "typing notification") chat state (:xep:`0085`)
        from this :term:`XMPP Entity`.
        """
        self._chat_state("composing", forced=True, **kwargs)

    def paused(self, **kwargs: object) -> None:
        """
        Send a "paused" (ie "typing paused notification") chat state
        (:xep:`0085`) from this :term:`XMPP Entity`.
        """
        self._chat_state("paused", forced=False, **kwargs)

    def inactive(self, **kwargs: object) -> None:
        """
        Send an "inactive" (ie "contact has not interacted with the chat session
        interface for an intermediate period of time") chat state (:xep:`0085`)
        from this :term:`XMPP Entity`.
        """
        self._chat_state("inactive", forced=False, **kwargs)

    def gone(self, **kwargs: object) -> None:
        """
        Send a "gone" (ie "contact has not interacted with the chat session interface,
        system, or device for a relatively long period of time") chat state
        (:xep:`0085`) from this :term:`XMPP Entity`.
        """
        self._chat_state("gone", forced=False, **kwargs)


class MarkerMixin(MessageMaker):
    def _make_marker(
        self, legacy_msg_id: str, marker: Marker, carbon: bool = False
    ) -> Message:
        msg = self._make_message(carbon=carbon)
        msg[marker]["id"] = self._legacy_to_xmpp(legacy_msg_id)[-1]
        return msg

    def ack(self, legacy_msg_id: str, **kwargs: object) -> None:
        """
        Send an "acknowledged" message marker (:xep:`0333`) from this :term:`XMPP Entity`.

        :param legacy_msg_id: The message this marker refers to
        """
        self._send(
            self._make_marker(
                legacy_msg_id, "acknowledged", carbon=bool(kwargs.get("carbon"))
            ),
            **kwargs,
        )

    def received(self, legacy_msg_id: str, **kwargs: object) -> None:
        """
        Send a "received" message marker (:xep:`0333`) from this :term:`XMPP Entity`.
        If called on a :class:`LegacyContact`, also send a delivery receipt
        marker (:xep:`0184`).

        :param legacy_msg_id: The message this marker refers to
        """
        carbon = bool(kwargs.get("carbon"))
        if self.mtype == "chat":
            for msg_id in self._legacy_to_xmpp(legacy_msg_id):
                self._send(
                    self.xmpp.delivery_receipt.make_ack(
                        msg_id,
                        mfrom=self.jid,
                        mto=self.user_jid,
                    )
                )
        self._send(
            self._make_marker(legacy_msg_id, "received", carbon=carbon), **kwargs
        )

    def displayed(self, legacy_msg_id: str, **kwargs: object) -> None:
        """
        Send a "displayed" message marker (:xep:`0333`) from this :term:`XMPP Entity`.

        :param legacy_msg_id: The message this marker refers to
        """
        if (
            self.xmpp.MARK_ALL_MESSAGES
            and (muc := getattr(self, "muc", None))
            and getattr(self, "is_user", False)
        ):
            muc_jid = muc.jid.bare
            with self.xmpp.store.session() as orm:
                if self.xmpp.store.mam.is_displayed_by_user(
                    orm, muc_jid, str(legacy_msg_id)
                ):
                    self.session.log.debug(
                        "Ignoring carbon marker for message already displayed by user."
                    )
                    return
                else:
                    muc.pop_unread_xmpp_ids_up_to(
                        self._legacy_to_xmpp(legacy_msg_id)[-1]
                    )

        self._send(
            self._make_marker(
                legacy_msg_id, "displayed", carbon=bool(kwargs.get("carbon"))
            ),
            **kwargs,
        )
        if getattr(self, "is_user", False):
            self.session.create_task(self.__send_mds(legacy_msg_id))

    async def __send_mds(self, legacy_msg_id: str) -> None:
        # Send a MDS displayed marker on behalf of the user for a group chat
        if muc := getattr(self, "muc", None):
            muc_jid = muc.jid.bare
        else:
            # This is not implemented for 1:1 chat because it would rely on
            # storing the XMPP-server injected stanza-id, which we don't track
            # ATM.
            # In practice, MDS should mostly be useful for public group chats,
            # so it should not be an issue.
            # We'll see if we need to implement that later
            return
        xmpp_msg_id = self._legacy_to_xmpp(legacy_msg_id)[-1]
        iq = Iq(sto=self.user_jid.bare, sfrom=self.user_jid.bare, stype="set")
        iq["pubsub"]["publish"]["node"] = self.xmpp.plugin["xep_0490"].stanza.NS
        iq["pubsub"]["publish"]["item"]["id"] = muc_jid
        displayed = self.xmpp.plugin["xep_0490"].stanza.Displayed()
        displayed["stanza_id"]["id"] = xmpp_msg_id
        displayed["stanza_id"]["by"] = muc_jid
        iq["pubsub"]["publish"]["item"]["payload"] = displayed
        iq["pubsub"]["publish_options"] = PUBLISH_OPTIONS
        try:
            await self.xmpp.plugin["xep_0356"].send_privileged_iq(iq)
        except Exception as e:
            self.session.log.debug("Could not MDS mark", exc_info=e)


class ContentMessageMixin(AttachmentMixin, TextMessageMixin):
    pass


class CarbonMessageMixin(ContentMessageMixin, MarkerMixin):
    def _privileged_send(self, msg: Message) -> None:
        i = msg.get_id()
        if i:
            self.session.ignore_messages.add(i)
        else:
            i = "slidge-carbon-" + str(uuid.uuid4())
            msg.set_id(i)
        msg.del_origin_id()
        try:
            self.xmpp.plugin["xep_0356"].send_privileged_message(msg)
        except PermissionError:
            warnings.warn(
                f"Slidge does not have the privilege (XEP-0356) to send messages on behalf of {self.user_jid}. "
                "If this is a local user, consider configuring your XMPP server for that."
            )


class InviteMixin(MessageMaker):
    def invite_to(
        self,
        muc: AnyMUC,
        reason: str | None = None,
        password: str | None = None,
        **send_kwargs: object,
    ) -> None:
        """
        Send an invitation to join a group (:xep:`0249`) from this :term:`XMPP Entity`.

        :param muc: the muc the user is invited to
        :param reason: a text explaining why the user should join this muc
        :param password: maybe this will make sense later? not sure
        :param send_kwargs: additional kwargs to be passed to _send()
            (internal use by slidge)
        """
        msg = self._make_message(mtype="normal")
        msg["groupchat_invite"]["jid"] = muc.jid
        if reason:
            msg["groupchat_invite"]["reason"] = reason
        if password:
            msg["groupchat_invite"]["password"] = password
        self._send(msg, **send_kwargs)


class MessageMixin(InviteMixin, ChatStateMixin, MarkerMixin, ContentMessageMixin):
    pass


class MessageCarbonMixin(InviteMixin, ChatStateMixin, CarbonMessageMixin):
    pass


log = logging.getLogger(__name__)
