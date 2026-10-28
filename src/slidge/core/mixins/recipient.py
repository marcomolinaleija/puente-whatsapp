import abc

from slixmpp import JID
from slixmpp.plugins.xep_0004.stanza.form import Form

from ...util.types import ChatState, Sticker, XMPPMessage


class ReactionMixin:
    REACTIONS_SINGLE_EMOJI = False

    async def restricted_emoji_extended_feature(self) -> Form | None:
        available = await self.available_emojis()
        if not self.REACTIONS_SINGLE_EMOJI and available is None:
            return None

        form = Form()
        form["type"] = "result"
        form.add_field("FORM_TYPE", "hidden", value="urn:xmpp:reactions:0:restrictions")
        if self.REACTIONS_SINGLE_EMOJI:
            form.add_field("max_reactions_per_user", value="1", type="text-single")
        if available:
            form.add_field("allowlist", value=list(available), type="text-multi")
        return form

    @abc.abstractmethod
    async def available_emojis(
        self, legacy_msg_id: str | None = None
    ) -> set[str] | None:
        """
        Override this to restrict the subset of reactions this recipient
        can handle.

        :return: A set of emojis or None if any emoji is allowed
        """
        return None


class ThreadMixin:
    @abc.abstractmethod
    async def create_thread(self, xmpp_id: str) -> str:
        return xmpp_id


class MessageMixin:
    @abc.abstractmethod
    async def on_message(self, message: XMPPMessage) -> str | None:
        """
        Triggered when the user sends a message to this :term:`Recipient`.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def on_sticker(self, sticker: Sticker) -> str | None:
        """
        Triggered when the user sends a sticker to this :term:`Recipient`.

        :param sticker: The sticker sent by the user.

        :return: An ID of some sort that can be used later to ack and mark the
            message as read by the user
        """
        raise NotImplementedError

    async def on_chat_state(self, chat_state: ChatState, thread: str | None) -> None:
        """
        Triggered when the user sends a chat state (:xep:`0085`) to this
        :term:`Recipient`.

        :param chat_state: The sticker sent by the user.
        """
        raise NotImplementedError

    async def on_displayed(self, legacy_msg_id: str, thread: str | None) -> None:
        """
        Triggered when the user sends a read marker (:xep:`0333`) to this
        :term:`Recipient`.

        This is only possible if a valid ``legacy_msg_id`` was passed when
        transmitting a message from a legacy chat to the user, eg in
        :meth:`slidge.contact.LegacyContact.send_text`
        or
        :meth:`slidge.group.LegacyParticipant.send_text`.

        :param legacy_msg_id: Identifier of the message/
        :param thread:
        """
        raise NotImplementedError

    async def on_react(
        self, legacy_msg_id: str, emojis: list[str], thread: str | None
    ) -> None:
        """
        Triggered when the user sends an emoji reaction (:xep:`0444`) to
        this :term:`Recipient`.

        :param legacy_msg_id: ID of the message the user reacts to
        :param emojis: Unicode characters representing reactions to the message ``legacy_msg_id``.
            An empty list means "no reaction", ie, remove all reactions if any were present before
        :param thread:
        """
        raise NotImplementedError

    async def on_retract(self, legacy_msg_id: str, thread: str | None) -> None:
        """
        Triggered when the user retracts (:xep:`0424`) a message.

        :param legacy_msg_id: Legacy ID of the retracted message
        :param thread:
        """
        raise NotImplementedError


class RecipientMixin(ReactionMixin, ThreadMixin, MessageMixin):
    jid: JID
    is_group: bool

    def __eq__(self, other: object) -> bool:
        return isinstance(other, self.__class__) and self.jid == other.jid
