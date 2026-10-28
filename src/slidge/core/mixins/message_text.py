import logging
from collections.abc import Iterable
from datetime import datetime
from xml.etree import ElementTree as ET

from slixmpp import Message

from ...util.archive_msg import HistoryMessage
from ...util.types import (
    LinkPreview,
    MessageReference,
    ProcessingHint,
)
from ...util.util import add_quote_prefix, remove_emoji_variation_selector_16
from .message_maker import MessageMaker


WHATSAPP_FORWARDED_TAG = "{urn:marco-ml:whatsapp:forwarded:0}forwarded"


def add_whatsapp_forwarded_flag(msg: Message, is_forwarded: bool) -> None:
    if is_forwarded:
        msg.xml.append(ET.Element(WHATSAPP_FORWARDED_TAG))


class TextMessageMixin(MessageMaker):
    def __default_hints(
        self, hints: Iterable[ProcessingHint] | None = None
    ) -> Iterable[ProcessingHint]:
        if hints is not None:
            return hints
        elif self.mtype == "chat":
            return {"markable", "store"}
        elif self.mtype == "groupchat":
            return {"markable"}
        else:
            raise RuntimeError("Never")

    def _replace_id(self, legacy_msg_id: str) -> str:
        if self.mtype == "groupchat":
            with self.xmpp.store.session() as orm:
                ids = self.xmpp.store.id_map.get_origin(
                    orm, self._recipient_pk(), str(legacy_msg_id)
                )
                if ids:
                    if len(ids) > 1:
                        log.warning(
                            "More than 1 origin msg ID for '%s': '%s'",
                            legacy_msg_id,
                            ids,
                        )
                    return ids[0]
                return legacy_msg_id
        else:
            return self._legacy_to_xmpp(legacy_msg_id)[0]

    def send_text(
        self,
        body: str,
        legacy_msg_id: str | None = None,
        *,
        when: datetime | None = None,
        reply_to: MessageReference | None = None,
        thread: str | None = None,
        hints: Iterable[ProcessingHint] | None = None,
        carbon: bool = False,
        archive_only: bool = False,
        correction: bool = False,
        correction_event_id: str | None = None,
        link_previews: list[LinkPreview] | None = None,
        is_forwarded: bool = False,
        extra_xml: ET.Element | None = None,
        **send_kwargs: object,
    ) -> Message | None:
        """
        Send a text message from this :term:`XMPP Entity`.

        :param body: Content of the message
        :param legacy_msg_id: If you want to be able to transport read markers from the gateway
            user to the legacy network, specify this
        :param when: when the message was sent, for a "delay" tag (:xep:`0203`)
        :param reply_to: Quote another message (:xep:`0461`)
        :param hints:
        :param thread:
        :param carbon: (only used if called on a :class:`LegacyContact`)
            Set this to ``True`` if this is actually a message sent **to** the
            :class:`LegacyContact` by the :term:`User`.
            Use this to synchronize outgoing history for legacy official apps.
        :param correction: whether this message is a correction or not
        :param correction_event_id: in the case where an ID is associated with the legacy
            'correction event', specify it here to use it on the XMPP side. If not specified,
            a random ID will be used.
        :param link_previews: A little of sender (or server, or gateway)-generated
            previews of URLs linked in the body.
        :param archive_only: (only in groups) Do not send this message to user,
            but store it in the archive. Meant to be used during ``MUC.backfill()``
        """
        if carbon and not self.is_participant:
            with self.xmpp.store.session() as orm:
                if not correction and self.xmpp.store.id_map.was_sent_by_user(
                    orm, self._recipient_pk(), str(legacy_msg_id), False
                ):
                    log.warning(
                        "Carbon message for a message an XMPP has sent? This is a bug! %s",
                        legacy_msg_id,
                    )
                    return None
        hints = self.__default_hints(hints)
        msg = self._make_message(
            mbody=body,
            legacy_msg_id=correction_event_id if correction else legacy_msg_id,
            when=when,
            reply_to=reply_to,
            hints=hints or (),
            carbon=carbon,
            thread=thread,
            link_previews=link_previews,
        )
        add_whatsapp_forwarded_flag(msg, is_forwarded)
        if extra_xml is not None:
            msg.xml.append(extra_xml)
        if correction:
            if not legacy_msg_id:
                raise TypeError
            msg["replace"]["id"] = self._replace_id(legacy_msg_id)
        return self._send(
            msg,
            archive_only=archive_only,
            carbon=carbon,
            legacy_msg_id=legacy_msg_id,
            **send_kwargs,
        )

    def correct(
        self,
        legacy_msg_id: str,
        new_text: str,
        *,
        when: datetime | None = None,
        reply_to: MessageReference | None = None,
        thread: str | None = None,
        hints: Iterable[ProcessingHint] | None = None,
        carbon: bool = False,
        archive_only: bool = False,
        correction_event_id: str | None = None,
        link_previews: list[LinkPreview] | None = None,
        **send_kwargs: object,
    ) -> None:
        """
        Modify a message that was previously sent by this :term:`XMPP Entity`.

        Uses last message correction (:xep:`0308`)

        :param new_text: New content of the message
        :param legacy_msg_id: The legacy message ID of the message to correct
        :param when: when the message was sent, for a "delay" tag (:xep:`0203`)
        :param reply_to: Quote another message (:xep:`0461`)
        :param hints:
        :param thread:
        :param carbon: (only in 1:1) Reflect a message sent to this ``Contact`` by the user.
            Use this to synchronize outgoing history for legacy official apps.
        :param archive_only: (only in groups) Do not send this message to user,
            but store it in the archive. Meant to be used during ``MUC.backfill()``
        :param correction_event_id: in the case where an ID is associated with the legacy
            'correction event', specify it here to use it on the XMPP side. If not specified,
            a random ID will be used.
        :param link_previews: A little of sender (or server, or gateway)-generated
            previews of URLs linked in the body.
        """
        self.send_text(
            new_text,
            legacy_msg_id,
            when=when,
            reply_to=reply_to,
            hints=hints,
            carbon=carbon,
            thread=thread,
            correction=True,
            archive_only=archive_only,
            correction_event_id=correction_event_id,
            link_previews=link_previews,
            **send_kwargs,
        )

    def react(
        self,
        legacy_msg_id: str,
        emojis: Iterable[str] = (),
        thread: str | None = None,
        **kwargs: object,
    ) -> None:
        """
        Send a reaction (:xep:`0444`) from this :term:`XMPP Entity`.

        :param legacy_msg_id: The message which the reaction refers to.
        :param emojis: An iterable of emojis used as reactions
        :param thread:
        """
        xmpp_id = kwargs.pop("xmpp_id", None)
        emojis = set(remove_emoji_variation_selector_16(e) for e in emojis)
        if xmpp_id:
            assert isinstance(xmpp_id, str)
            xmpp_ids = [xmpp_id]
        else:
            xmpp_ids = self._legacy_to_xmpp(legacy_msg_id)
        for xmpp_id in xmpp_ids:
            msg = self._make_message(
                hints={"store"}, carbon=bool(kwargs.get("carbon")), thread=thread
            )
            self.xmpp.plugin["xep_0444"].set_reactions(
                msg, to_id=xmpp_id, reactions=emojis
            )
            self.__add_reaction_fallback(msg, legacy_msg_id, emojis)
            self._send(msg, **kwargs)

    def __add_reaction_fallback(
        self,
        msg: Message,
        legacy_msg_id: str,
        emojis: Iterable[str] = (),
    ) -> None:
        if not self.session.user.preferences.get("reaction_fallback", False):
            return
        msg["fallback"]["for"] = self.xmpp.plugin["xep_0444"].namespace
        msg["fallback"].enable("body")
        msg["body"] = " ".join(emojis)
        if not self.is_participant:
            return
        with self.xmpp.store.session() as orm:
            archived = self.xmpp.store.mam.get_by_legacy_id(
                orm, self.muc.stored.id, str(legacy_msg_id)
            )
            if archived is None:
                return
            history_msg = HistoryMessage(archived.stanza)
            msg["body"] = (
                add_quote_prefix(history_msg.stanza["body"]) + "\n" + msg["body"]
            )

    def retract(
        self,
        legacy_msg_id: str,
        thread: str | None = None,
        **kwargs: object,
    ) -> None:
        """
        Send a message retraction (:XEP:`0424`) from this :term:`XMPP Entity`.

        :param legacy_msg_id: Legacy ID of the message to delete
        :param thread:
        """
        xmpp_ids = self._legacy_to_xmpp(legacy_msg_id)
        replace_id = self._replace_id(legacy_msg_id)
        if replace_id not in xmpp_ids:
            xmpp_ids.append(replace_id)
        for xmpp_id in xmpp_ids:
            msg = self._make_message(
                state=None,
                hints={"store"},
                mbody=f"/me retracted the message {legacy_msg_id}",
                carbon=bool(kwargs.get("carbon")),
                thread=thread,
            )
            msg.enable("fallback")
            # namespace version mismatch between slidge and slixmpp, update me later
            msg["fallback"]["for"] = self.xmpp.plugin["xep_0424"].namespace[:-1] + "1"
            msg["retract"]["id"] = msg["replace"]["id"] = xmpp_id
            self._send(msg, **kwargs)


log = logging.getLogger(__name__)
