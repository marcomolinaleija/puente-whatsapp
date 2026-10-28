import base64
import hashlib
import logging
from copy import copy
from dataclasses import dataclass
from typing import TYPE_CHECKING
from xml.etree import ElementTree

from slixmpp import JID, Message
from slixmpp.exceptions import XMPPError
from slixmpp.plugins.xep_0511.stanza import LinkMetadata

from ....contact.contact import LegacyContact
from ....group.room import LegacyMUC
from ....util.types import (
    AnyRecipient,
    LinkPreview,
    RecipientType,
    Reply,
    XMPPAttachment,
    XMPPAttachmentMessage,
    XMPPMessage,
    XMPPTextMessage,
)
from ....util.util import dict_to_named_tuple, remove_emoji_variation_selector_16
from ... import config
from ..util import DispatcherMixin, exceptions_to_xmpp_errors

if TYPE_CHECKING:
    from slidge.util.types import AnyGateway


WHATSAPP_POLL_NAMESPACE = "urn:marco-ml:whatsapp:poll:0"
WHATSAPP_POLL_LOG = logging.getLogger(__name__)


@dataclass
class _IncomingAttachment:
    attachment: XMPPAttachment
    is_sticker: bool
    cid: str | None = None
    description: str = ""


class MessageContentMixin(DispatcherMixin):
    __slots__: list[str] = []

    def __init__(self, xmpp: "AnyGateway") -> None:
        super().__init__(xmpp)
        xmpp.add_event_handler("legacy_message", self.on_legacy_message)
        xmpp.add_event_handler("message_retract", self.on_message_retract)
        xmpp.add_event_handler("groupchat_message", self.on_groupchat_message)
        xmpp.add_event_handler("reactions", self.on_reactions)

    async def on_groupchat_message(self, msg: Message) -> None:
        await self.on_legacy_message(msg)

    @exceptions_to_xmpp_errors
    async def on_legacy_message(self, msg: Message) -> None:
        """
        Meant to be called from :class:`BaseGateway` only.

        :param msg:
        :return:
        """
        # we MUST not use `if m["replace"]["id"]` because it adds the tag if not
        # present. this is a problem for MUC echoed messages
        if "apply_to" in msg:
            # ignore message retraction (handled by a specific method)
            # this is an old version of the protocol that depends on the
            # message fastening deprecated xep.
            return
        if "reactions" in msg:
            # ignore message reaction fallback.
            # the reaction itself is handled by self.react_from_msg().
            return
        if "retract" in msg:
            # ignore message retraction fallback.
            # the retraction itself is handled by self.on_retract
            return
        recipient, thread = await self._get_recipient_and_thread(msg)
        vote = msg.xml.find(f"{{{WHATSAPP_POLL_NAMESPACE}}}vote")
        if vote is not None:
            poll_id = vote.attrib.get("id", "").strip()
            creator = vote.attrib.get("creator", "").strip()
            creator_lid = vote.attrib.get("creator-lid", "").strip()
            creator_is_me_value = vote.attrib.get("creator-is-me", "false").lower()
            if not poll_id or not creator:
                raise XMPPError(
                    "bad-request", "Poll votes require a poll ID and creator."
                )
            if creator_is_me_value not in {"true", "false"}:
                raise XMPPError("bad-request", "Invalid poll creator-is-me value.")
            if recipient.is_group and not creator_lid:
                raise XMPPError(
                    "bad-request", "Group poll votes require the creator LID."
                )

            options = [
                (option.text or "").strip()
                for option in vote.findall(f"{{{WHATSAPP_POLL_NAMESPACE}}}option")
            ]
            if not 1 <= len(options) <= 12 or any(not option for option in options):
                raise XMPPError("bad-request", "Invalid poll vote options.")
            if len(set(options)) != len(options):
                raise XMPPError("bad-request", "Poll vote options must be unique.")

            WHATSAPP_POLL_LOG.info(
                "Received poll vote id=%s option_count=%d", poll_id, len(options)
            )
            await recipient.on_poll_vote(
                poll_id=poll_id,
                creator=creator,
                creator_lid=creator_lid,
                creator_is_me=creator_is_me_value == "true",
                options=options,
            )
            WHATSAPP_POLL_LOG.info("Forwarded poll vote id=%s to WhatsApp", poll_id)

            confirmation = msg.reply(clear=True)
            # Keep a whitespace body because some XMPP routing layers discard
            # extension-only messages. The client strips it before rendering.
            confirmation["body"] = " "
            update_xml = ElementTree.Element(
                f"{{{WHATSAPP_POLL_NAMESPACE}}}poll-update",
                {
                    "id": poll_id,
                    "voter": "me",
                    "voter-is-me": "true",
                },
            )
            for option in options:
                ElementTree.SubElement(
                    update_xml,
                    f"{{{WHATSAPP_POLL_NAMESPACE}}}option",
                    {"hash": hashlib.sha256(option.encode("utf-8")).hexdigest()},
                )
            confirmation.append(update_xml)
            confirmation.append(ElementTree.Element("{urn:xmpp:hints}store"))
            confirmation.send()
            WHATSAPP_POLL_LOG.info(
                "Confirmed poll vote id=%s stanza_id=%s type=%s to=%s from=%s",
                poll_id,
                msg["id"],
                confirmation["type"],
                confirmation["to"],
                confirmation["from"],
            )
            self.__ack(msg)
            return
        replace = await self.__get_replace(msg, recipient)
        if msg.xml.find(".//{*}encrypted") is not None:
            raise XMPPError(
                "bad-request", "You cannot send encrypted messages through this gateway"
            )
        body, reply = await self.__get_reply(msg, recipient)
        cid = self.__get_xhtml_sticker_cid(msg)

        if cid:
            legacy_msg_id = await self.__dispatch_bob(
                msg.get_from(),
                cid,
                recipient,
                reply=reply,
                thread=thread,
            )
        else:
            attachments = self.__get_attachments(msg)
            if len(attachments) == 1 and attachments[0].is_sticker:
                legacy_msg_id = await self.__dispatch_nonbob_sticker(
                    attachments[0],
                    recipient,
                    msg["body"],
                    reply=reply,
                    thread=thread,
                )
            else:
                legacy_msg_id = await self.__dispatch_msg(
                    replace=replace,
                    body=body,
                    attachments=tuple(a.attachment for a in attachments),
                    recipient=recipient,
                    thread=thread,
                    reply=reply,
                    msg=msg,
                )

        if isinstance(recipient, LegacyMUC):
            stanza_id = await recipient.echo(msg, legacy_msg_id)
        else:
            stanza_id = None
            self.__ack(msg)

        if not legacy_msg_id:
            return

        with self.xmpp.store.session() as orm:
            if recipient.is_group:
                self.xmpp.store.id_map.set_origin(
                    orm, recipient.stored.id, legacy_msg_id, msg.get_id()
                )
                assert stanza_id is not None
                self.xmpp.store.id_map.set_msg(
                    orm,
                    recipient.stored.id,
                    legacy_msg_id,
                    [stanza_id],
                    True,
                )
            else:
                self.xmpp.store.id_map.set_msg(
                    orm,
                    recipient.stored.id,
                    legacy_msg_id,
                    [msg.get_id()],
                    False,
                )
            if recipient.session.MESSAGE_IDS_ARE_THREAD_IDS and thread:
                self.xmpp.store.id_map.set_thread(
                    orm, recipient.stored.id, thread, legacy_msg_id, recipient.is_group
                )
            orm.commit()

    def __get_xhtml_sticker_cid(self, msg: Message) -> str | None:
        if "html" not in msg:
            return None
        body = ElementTree.fromstring("<body>" + msg["html"].get_body() + "</body>")
        p = body.findall("p")
        if p is None:
            return None
        if len(p) != 1:
            return None
        if p[0].text is not None and p[0].text.strip():
            return None

        images = p[0].findall("img")
        if len(images) != 1:
            return None
        # no text, single img ⇒ this is a sticker
        # other cases should be interpreted as "custom emojis" in text
        src = images[0].get("src")
        if src is None:
            return None
        if src.startswith("cid:"):
            return src.removeprefix("cid:")
        return None

    async def __get_replace(
        self,
        msg: Message,
        recipient: RecipientType,
    ) -> str | None:
        if "replace" not in msg or "id" not in msg["replace"]:
            return None
        return (
            self._xmpp_msg_id_to_legacy(msg["replace"]["id"], recipient, True) or None
        )

    def __get_attachments(self, msg: Message) -> list[_IncomingAttachment]:
        description = ""
        # Only file metadata is alt text: never use body/URL/quoted text as a label.
        for parent in (
            "{urn:xmpp:sfs:0}file-sharing/{urn:xmpp:file:metadata:0}file",
            "{urn:xmpp:reference:0}reference/{urn:xmpp:sims:1}media-sharing/"
            "{urn:xmpp:file:metadata:0}file",
        ):
            node = msg.xml.find(parent + "/{urn:xmpp:file:metadata:0}desc")
            if node is not None and node.text:
                description = node.text.strip()[:8000]
                break
        is_sticker = (
            "sticker" in msg
            or msg.xml.find("{urn:xmpp:stickers:0}sticker") is not None
        )

        if (
            "sfs" in msg
            and "sources" in msg["sfs"]
            and "url-data" in msg["sfs"]["sources"]
            and "target" in msg["sfs"]["sources"]["url-data"]
        ):
            # TODO: support "attach source in later message", cf https://xmpp.org/extensions/xep-0447.html#example-5
            # TODO: support for other sources than URL
            # TODO: support for multiattachments in single message.
            #       What do we do if is_sticker and multiple files?
            attachment = _IncomingAttachment(
                XMPPAttachment(url=msg["sfs"]["sources"]["url-data"]["target"]),
                is_sticker=is_sticker, description=description,
            )
            if "file" in msg["sfs"]:
                attachment.attachment.content_type = (
                    msg["sfs"]["file"]["media-type"] or None
                )
                if "hash" in msg["sfs"]["file"]:
                    algo = msg["sfs"]["file"]["hash"]["algo"]
                    h = msg["sfs"]["file"]["hash"]["value"]
                    if algo == "sha-256" and h:
                        try:
                            digest = base64.b64decode(h, validate=True)
                        except ValueError:
                            digest = b""
                        if len(digest) == 32:
                            attachment.cid = "sha256+" + digest.hex()
            return [attachment]

        if "oob" in msg:
            return [
                _IncomingAttachment(
                    XMPPAttachment(url=msg["oob"]["url"]), is_sticker=is_sticker, description=description
                )
            ]

        if (
            "reference" in msg
            and "sims" in msg["reference"]
            and "sources" in msg["reference"]["sims"]
        ):
            for source in msg["reference"]["sims"]["sources"]["substanzas"]:
                if source["uri"].startswith("http"):
                    attachment = _IncomingAttachment(
                        XMPPAttachment(url=source["uri"]), is_sticker=is_sticker, description=description
                    )
                    break
            else:
                return []
            if "file" in msg["reference"]["sims"]:
                attachment.attachment.content_type = msg["reference"]["sims"]["file"]["media-type"] or None
            return [attachment]

        return []

    async def __dispatch_msg(
        self,
        *,
        replace: str | None,
        reply: Reply | None,
        body: str | None,
        attachments: tuple[XMPPAttachment, ...],
        recipient: AnyRecipient,
        thread: str | None,
        msg: Message,
    ) -> str | None:
        if replace:
            if body is not None:
                body = body.strip()
            if not body and not attachments and recipient.RETRACTION:
                await recipient.on_retract(replace, thread=thread)
                return None
            if not recipient.CORRECTION:
                if recipient.RETRACTION:
                    await recipient.on_retract(replace, thread=thread)
                    replace = None
                elif body:
                    body = "Correction:\n" + body

        if not any([attachments, body]):
            log.debug(
                "Ignoring msg with id '%s' because no body or attachments found",
                msg.get_id(),
            )
            return None
        link_previews = (
            parse_link_previews(msg["link_metadatas"]) if "link_metadata" in msg else ()
        )

        for attachment in attachments:
            if not body:
                body = None
                break
            body = body.replace(attachment.url, "").strip()

        if recipient.is_group:
            mentions = tuple(await recipient.parse_mentions(body, msg.xml))
        else:
            mentions = ()

        is_forwarded = (
            msg.xml.find("{urn:marco-ml:whatsapp:forwarded:0}forwarded") is not None
        )

        if attachments:
            xmpp_msg: XMPPMessage = XMPPAttachmentMessage(
                body=body,
                is_forwarded=is_forwarded,
                attachments=attachments,
                reply=reply,
                thread=thread,
                link_previews=link_previews,
                mentions=mentions,
                replace=replace,
            )
        else:
            xmpp_msg = XMPPTextMessage(
                body=body,
                is_forwarded=is_forwarded,
                attachments=attachments,
                reply=reply,
                thread=thread,
                link_previews=link_previews,
                mentions=mentions,
                replace=replace,
            )

        return await recipient.on_message(xmpp_msg)

    @exceptions_to_xmpp_errors
    async def on_message_retract(self, msg: Message) -> None:
        recipient, thread = await self._get_recipient_and_thread(msg)
        if not recipient.RETRACTION:
            raise XMPPError(
                "bad-request",
                "This legacy service does not support message retraction.",
            )
        xmpp_id: str = msg["retract"]["id"]
        legacy_id = self._xmpp_msg_id_to_legacy(xmpp_id, recipient, origin=True)
        await recipient.on_retract(legacy_id, thread=thread)
        if isinstance(recipient, LegacyMUC):
            await recipient.echo(msg, None)
        self.__ack(msg)

    @exceptions_to_xmpp_errors
    async def on_reactions(self, msg: Message) -> None:
        recipient, thread = await self._get_recipient_and_thread(msg)
        react_to: str = msg["reactions"]["id"]

        special_msg = recipient.session.SPECIAL_MSG_ID_PREFIX and react_to.startswith(
            recipient.session.SPECIAL_MSG_ID_PREFIX
        )

        if special_msg:
            legacy_id = react_to
        else:
            legacy_id = self._xmpp_msg_id_to_legacy(react_to, recipient)

        if not legacy_id:
            log.debug("Ignored reaction from user")
            raise XMPPError(
                "internal-server-error",
                "Could not convert the XMPP msg ID to a legacy ID",
            )

        emojis = [
            remove_emoji_variation_selector_16(r["value"]) for r in msg["reactions"]
        ]
        error_msg = None
        recipient = recipient

        if not special_msg:
            if recipient.REACTIONS_SINGLE_EMOJI and len(emojis) > 1:
                error_msg = "Maximum 1 emoji/message"

            if (
                not error_msg
                and (subset := await recipient.available_emojis(legacy_id))
                and not set(emojis).issubset(subset)
            ):
                error_msg = (
                    f"You can only react with the following emojis: {''.join(subset)}"
                )

        if error_msg:
            recipient.session.send_gateway_message(error_msg)
            if not isinstance(recipient, LegacyMUC):
                # no need to carbon for groups, we just don't echo the stanza
                recipient.react(legacy_id, carbon=True)
            await recipient.on_react(legacy_id, [], thread=thread)
            raise XMPPError(
                "policy-violation",
                text=error_msg,
                clear=False,
            )

        await recipient.on_react(legacy_id, emojis, thread=thread)
        if isinstance(recipient, LegacyMUC):
            await recipient.echo(msg, None)
        else:
            self.__ack(msg)

        with self.xmpp.store.session() as orm:
            multi = self.xmpp.store.id_map.get_xmpp(
                orm, recipient.stored.id, legacy_id, recipient.is_group
            )
        if not multi:
            return
        multi = [m for m in multi if react_to != m]

        if isinstance(recipient, LegacyMUC):
            for xmpp_id in multi:
                mc = copy(msg)
                mc["reactions"]["id"] = xmpp_id
                await recipient.echo(mc)
        elif isinstance(recipient, LegacyContact):
            for xmpp_id in multi:
                recipient.react(legacy_id, emojis, xmpp_id=xmpp_id, carbon=True)

    def __ack(self, msg: Message) -> None:
        if not self.xmpp.PROPER_RECEIPTS:
            self.xmpp.delivery_receipt.ack(msg)

    async def __get_reply(
        self, msg: Message, recipient: AnyRecipient
    ) -> tuple[str, Reply | None]:
        if "reply" not in msg:
            return msg["body"], None

        session = recipient.session
        reply_to_jid = JID(msg["reply"]["to"])
        reply_to = None
        reply_recipient = recipient

        # A private group reply must reference its MUC even though its outer stanza is a chat.
        is_private_group_reply = (
            msg["type"] == "chat"
            and reply_to_jid.local.startswith("#")
            and reply_to_jid.server == self.xmpp.boundjid.bare
        )
        if is_private_group_reply:
            try:
                muc = await session.bookmarks.by_jid(reply_to_jid)
                reply_recipient = muc
                nick = reply_to_jid.resource
                if nick == muc.user_nick:
                    reply_to = await muc.get_user_participant()
                elif not nick:
                    reply_to = muc.get_system_participant()
                else:
                    reply_to = await muc.get_participant(nick, store=False)
            except XMPPError:
                session.log.exception("Could not instantiate replied-to group participant")
        elif msg["type"] == "chat":
            if reply_to_jid.bare != session.user_jid.bare:
                try:
                    reply_to = await session.contacts.by_jid(reply_to_jid)
                except XMPPError:
                    session.log.exception("Could not instantiate replied-to contact")
        elif msg["type"] == "groupchat":
            nick = reply_to_jid.resource
            try:
                muc = await session.bookmarks.by_jid(reply_to_jid)
            except XMPPError:
                session.log.exception("Could not instantiate replied-to participant")
            else:
                if nick == muc.user_nick:
                    reply_to = await muc.get_user_participant()
                elif not nick:
                    reply_to = muc.get_system_participant()
                else:
                    reply_to = await muc.get_participant(nick, store=False)

        try:
            reply_to_msg_id = self._xmpp_msg_id_to_legacy(msg["reply"]["id"], reply_recipient)
        except XMPPError:
            session.log.debug("Could not determine reply-to legacy msg ID, sending quote instead.")
            return redact_url(msg["body"]), None

        if "fallback" in msg and (isinstance(reply_recipient, LegacyMUC) or recipient.REPLIES):
            text = msg["fallback"].get_stripped_body(self.xmpp.plugin["xep_0461"].namespace)
            try:
                reply_fallback = redact_url(msg["reply"].get_fallback_body())
            except AttributeError:
                reply_fallback = None
        else:
            text = msg["body"]
            reply_fallback = None

        return text, Reply(reply_to_msg_id, reply_to, reply_fallback)

    async def __dispatch_nonbob_sticker(
        self,
        attachment: _IncomingAttachment,
        recipient: AnyRecipient,
        fallback: str,
        reply: Reply | None = None,
        thread: str | None = None,
    ) -> str | None:
        sticker = None
        if attachment.cid:
            with self.xmpp.store.session() as orm:
                sticker = self.xmpp.store.bob.get_sticker(orm, attachment.cid)
        if sticker is None:
            async with attachment.attachment.get() as response:
                response.raise_for_status()
                data = await response.read()
            cid = "sha256+" + hashlib.sha256(data).hexdigest()
            with self.xmpp.store.session() as orm:
                sticker = self.xmpp.store.bob.get_sticker(orm, cid)
                if sticker is None:
                    sticker = self.xmpp.store.bob.set_sticker(
                        orm, cid, data, attachment.attachment.content_type
                    )
                    orm.commit()
                    if sticker is None:
                        sticker = self.xmpp.store.bob.get_sticker(orm, cid)
            if sticker is None:
                raise XMPPError("not-acceptable", "Unable to prepare native sticker")
        # Reply, thread and alt text belong to this send, not the shared cached object.
        sticker = copy(sticker)
        sticker.reply = reply
        sticker.thread = thread
        sticker.fallback = attachment.description or None
        return await recipient.on_sticker(sticker)

    async def __dispatch_bob(
        self,
        from_: JID,
        cid: str,
        recipient: AnyRecipient,
        reply: Reply | None = None,
        thread: str | None = None,
    ) -> str | None:
        with self.xmpp.store.session() as orm:
            sticker = self.xmpp.store.bob.get_sticker(orm, cid)
        if sticker is None:
            await self.xmpp.plugin["xep_0231"].get_bob(
                from_, cid, ifrom=self.xmpp.boundjid
            )
            with self.xmpp.store.session() as orm:
                sticker = self.xmpp.store.bob.get_sticker(orm, cid)
        assert sticker is not None
        sticker.reply = reply
        sticker.thread = thread
        return await recipient.on_sticker(sticker)


def redact_url(text: str) -> str:
    needle = config.NO_UPLOAD_URL_PREFIX or config.UPLOAD_URL_PREFIX
    if not needle:
        return text
    return text.replace(needle, "")


def parse_link_previews(link_metadatas: list[LinkMetadata]) -> tuple[LinkPreview, ...]:
    result = []
    for link_metadata in link_metadatas:
        preview: LinkPreview = dict_to_named_tuple(link_metadata, LinkPreview)  # type:ignore[arg-type]
        if (
            preview.image
            and isinstance(preview.image, str)
            and preview.image.startswith("data:image/jpeg;base64,")
        ):
            try:
                image = base64.b64decode(
                    preview.image.removeprefix("data:image/jpeg;base64,")
                )
            except Exception as e:
                log.warning(
                    "Could not decode base64-encoded image: %s '%s'", e, preview.image
                )
            else:
                preview = preview._replace(image=image)
        result.append(preview)
    return tuple(result)


log = logging.getLogger(__name__)
