from __future__ import annotations

import abc
import logging
import time
from os.path import basename
from re import search
from typing import TYPE_CHECKING

import aiohttp
from linkpreview import Link, LinkPreview
from slidge.core.mixins import AvatarMixin as BaseAvatarMixin
from slidge.util import replace_mentions
from slidge.util.types import ChatState, Mention, Sticker, XMPPMessage
from slixmpp.exceptions import XMPPError

from .generated import go, whatsapp
from .read_state import acknowledge, unread_up_to, send_receipt, sync_group_read
from .message_info import prepare_outgoing, outgoing_sent
from .deepgram_transcription import handle_transcription_command

if TYPE_CHECKING:
    from .session import Session


URL_SEARCH_REGEX = r"(?P<url>https?://[^\s]+)"
GEO_URI_SEARCH_REGEX = (
    r"geo:(?P<lat>-?\d+(\.\d*)?),(?P<lon>-?\d+(\.\d*)?)(;u=(?P<acc>-?\d+(\.\d*)?))?"
)
VIEW_ONCE_THREAD = "urn:marco-ml:whatsapp:view-once:0"


VIDEO_PREVIEW_DOMAINS = (
    "https://youtube.com/watch",
    "https://m.youtube.com/watch",
    "https://youtu.be",
)


class AvatarMixin(BaseAvatarMixin):
    legacy_id: str
    session: Session

    async def update_whatsapp_avatar(self) -> None:
        unique_id = ""
        if self.avatar is not None:
            # assert=workaround for poor type annotations in slidge core
            assert not isinstance(self.avatar.unique_id, int)
            unique_id = self.avatar.unique_id or ""
        self.session.whatsapp.RequestAvatar(self.legacy_id, unique_id)  # type:ignore[no-untyped-call]


from .outgoing_sticker_pack import prepare_outgoing_pack


from .audio_modes import (
    AUDIO_MODE_NS, MAX_AUDIO_ATTACHMENT_BYTES,
    audio_attachment_mime, audio_attachment_caption,
)


class RecipientMixin(abc.ABC):
    session: Session
    log: logging.Logger
    legacy_id: str

    @property
    def wa(self) -> whatsapp.Session:
        return self.session.whatsapp

    @abc.abstractmethod
    def get_wa_chat(self) -> whatsapp.Chat: ...

    @abc.abstractmethod
    async def get_wa_actor(self, legacy_msg_id: str) -> whatsapp.Actor: ...

    @abc.abstractmethod
    def _set_reply_to(
        self, xmpp_msg: XMPPMessage, wa_msg: whatsapp.Message
    ) -> None: ...

    async def on_poll_vote(
        self,
        *,
        poll_id: str,
        creator: str,
        creator_lid: str,
        creator_is_me: bool,
        options: list[str],
    ) -> None:
        chat = self.get_wa_chat()
        if chat.IsGroup and not creator_lid:
            raise XMPPError("bad-request", "Group poll votes require the creator LID.")
        if not chat.IsGroup and not creator:
            raise XMPPError("bad-request", "Direct poll votes require the creator JID.")

        poll_options = whatsapp.Slice_whatsapp_PollOption(  # type:ignore[no-untyped-call]
            [whatsapp.PollOption(Title=option) for option in options]  # type:ignore[no-untyped-call]
        )
        message = whatsapp.Message(  # type:ignore[no-untyped-call]
            Kind=whatsapp.MessagePoll,
            ID=poll_id,
            Actor=whatsapp.Actor(  # type:ignore[no-untyped-call]
                JID=creator,
                LID=creator_lid,
                IsMe=creator_is_me,
            ),
            Chat=chat,
            Poll=whatsapp.Poll(Options=poll_options),  # type:ignore[no-untyped-call]
        )
        self.log.info(
            "Building WhatsApp poll vote id=%s own_creator=%s option_count=%d",
            poll_id,
            creator_is_me,
            len(options),
        )
        try:
            self.wa.SendMessage(message)  # type:ignore[no-untyped-call]
        except Exception:
            self.log.exception("WhatsApp poll vote failed id=%s", poll_id)
            raise
        self.log.info("WhatsApp poll vote accepted id=%s", poll_id)

    async def on_message(self, message: XMPPMessage) -> str | None:
        if message.attachments:
            return await self._on_file(message)
        if message.body:
            if message.replace:
                await self._on_correct(message)
                return None
            else:
                return await self._on_text(message)
        raise XMPPError("internal-server-error", "This should not happen!")

    async def on_sticker(self, sticker: Sticker) -> str:
        """Send the XEP-0449 dispatch path as a native WhatsApp sticker."""
        try:
            data = sticker.path.read_bytes()
        except OSError as exc:
            raise XMPPError("internal-server-error", "Unable to read XMPP sticker") from exc

        message_id: str = self.wa.GenerateMessageID()  # type:ignore[no-untyped-call]
        attachment = whatsapp.Attachment(  # type:ignore[no-untyped-call]
            MIME="application/x-whatsapp-can-sticker",
            Filename=sticker.path.name,
            Data=go.Slice_byte.from_bytes(data),  # type:ignore[no-untyped-call]
            Caption=(sticker.fallback or "").strip(),
        )
        message = whatsapp.Message(  # type:ignore[no-untyped-call]
            Kind=whatsapp.MessageAttachment,
            ID=message_id,
            Chat=self.get_wa_chat(),
            ReplyID=sticker.reply.msg_id if sticker.reply else "",
            Attachments=whatsapp.Slice_whatsapp_Attachment([attachment]),  # type:ignore[no-untyped-call]
        )
        info_snapshot = await prepare_outgoing(self, message)
        self.wa.SendMessage(message)  # type:ignore[no-untyped-call]
        await outgoing_sent(self, message, info_snapshot)
        return message_id

    async def _on_text(self, xmpp_msg: XMPPMessage) -> str:
        """
        Send outgoing plain-text message to given WhatsApp contact.
        """
        assert xmpp_msg.body
        command_response = await handle_transcription_command(
            self.session.http,
            str(self.session.user_jid.bare),
            xmpp_msg.body,
            chat=str(self.jid.bare),
        )
        if command_response is not None:
            self.session.send_gateway_message(command_response)
            return self.wa.GenerateMessageID()  # type:ignore[no-untyped-call,no-any-return]
        message_id: str = self.wa.GenerateMessageID()  # type:ignore[no-untyped-call]
        message_preview = await self.__get_preview(xmpp_msg.body) or whatsapp.Preview()  # type:ignore[no-untyped-call]
        message_location = (
            await self.__get_location(xmpp_msg.body) or whatsapp.Location()  # type:ignore[no-untyped-call]
        )
        message = whatsapp.Message(  # type:ignore[no-untyped-call]
            ID=message_id,
            Chat=self.get_wa_chat(),
            Body=replace_mentions(xmpp_msg.body, xmpp_msg.mentions, mention_map),
            Preview=message_preview,
            Location=message_location,
            IsForwarded=xmpp_msg.is_forwarded,
            MentionJIDs=go.Slice_string(  # type:ignore[no-untyped-call]
                [m.contact.legacy_id for m in xmpp_msg.mentions]
            ),
        )
        self._set_reply_to(xmpp_msg, message)
        info_snapshot = await prepare_outgoing(self, message)
        self.wa.SendMessage(message)  # type:ignore[no-untyped-call]
        await outgoing_sent(self, message, info_snapshot)
        return message_id

    async def _on_file(self, xmpp_msg: XMPPMessage) -> str:
        """
        Send outgoing media message (i.e. audio, image, document) to given WhatsApp contact.
        """
        # TODO: handle multiple attachments when we support that in slidge core
        att = xmpp_msg.attachments[0]
        async with att.get() as resp:
            if resp.status == 200:
                if att.content_type == "application/x-can-sticker-pack":
                    if resp.content_length and resp.content_length > 32 * 1024 * 1024:
                        raise XMPPError("not-acceptable", "Native sticker pack too large")
                    chunks = bytearray()
                    async for chunk in resp.content.iter_chunked(256 * 1024):
                        chunks.extend(chunk)
                        if len(chunks) > 32 * 1024 * 1024:
                            raise XMPPError("not-acceptable", "Native sticker pack too large")
                    data = bytes(chunks)
                elif (xmpp_msg.thread or "").startswith(AUDIO_MODE_NS):
                    if resp.content_length and resp.content_length > MAX_AUDIO_ATTACHMENT_BYTES:
                        raise XMPPError("not-acceptable", "Audio attachment exceeds 64 MiB")
                    chunks = bytearray()
                    async for chunk in resp.content.iter_chunked(256 * 1024):
                        if len(chunks) + len(chunk) > MAX_AUDIO_ATTACHMENT_BYTES:
                            raise XMPPError("not-acceptable", "Audio attachment exceeds 64 MiB")
                        chunks.extend(chunk)
                    data = bytes(chunks)
                else:
                    data = await resp.read()
            else:
                raise XMPPError(
                    "internal-server-error",
                    "Unable to retrieve file from XMPP server, try again",
                )
            content_type = resp.content_type
        try:
            content_type = audio_attachment_mime(
                xmpp_msg.thread, att.content_type or content_type, is_sticker=att.is_sticker
            ) if (xmpp_msg.thread or "").startswith(AUDIO_MODE_NS) else audio_attachment_mime(
                xmpp_msg.thread, content_type
            )
        except ValueError as exc:
            raise XMPPError("not-acceptable", str(exc)) from exc
        if att.content_type == "application/x-can-sticker-pack":
            try:
                data = await prepare_outgoing_pack(data)
            except Exception as exc:
                raise XMPPError(
                    "not-acceptable", "Invalid native pack (1 to 60 compatible stickers required)"
                ) from exc
            content_type = "application/x-can-sticker-pack"
        message_id: str = self.wa.GenerateMessageID()  # type:ignore[no-untyped-call]
        message_attachment = whatsapp.Attachment(  # type:ignore[no-untyped-call]
            MIME="application/x-whatsapp-can-sticker" if att.is_sticker else content_type,
            Filename=basename(att.url),
            Data=go.Slice_byte.from_bytes(data),  # type:ignore[no-untyped-call]
            Caption="" if att.is_sticker else audio_attachment_caption(
                xmpp_msg.thread, xmpp_msg.body, att.url
            ),
            ViewOnce=xmpp_msg.thread == VIEW_ONCE_THREAD,
        )
        message = whatsapp.Message(  # type:ignore[no-untyped-call]
            Kind=whatsapp.MessageAttachment,
            ID=message_id,
            Chat=self.get_wa_chat(),
            ReplyID=xmpp_msg.reply.msg_id if xmpp_msg.reply else "",
            IsForwarded=xmpp_msg.is_forwarded,
            Attachments=whatsapp.Slice_whatsapp_Attachment([message_attachment]),  # type:ignore[no-untyped-call]
        )
        self._set_reply_to(xmpp_msg, message)
        info_snapshot = await prepare_outgoing(self, message)
        self.wa.SendMessage(message)  # type:ignore[no-untyped-call]
        await outgoing_sent(self, message, info_snapshot)
        return message_id

    async def _on_correct(self, xmpp_msg: XMPPMessage) -> None:
        """
        Request correction (aka editing) for a given WhatsApp message.
        """
        assert xmpp_msg.body
        message = whatsapp.Message(  # type:ignore[no-untyped-call]
            Kind=whatsapp.MessageEdit,
            ID=xmpp_msg.replace,
            Chat=self.get_wa_chat(),
            Body=replace_mentions(xmpp_msg.body, xmpp_msg.mentions, mention_map),
        )
        self.wa.SendMessage(message)  # type:ignore[no-untyped-call]

    async def __get_preview(self, text: str) -> whatsapp.Preview | None:
        enable_previews = self.session.user.preferences.get(
            "enable_link_previews", True
        )
        if not enable_previews:
            return None
        match = search(URL_SEARCH_REGEX, text)
        if not match:
            return None
        url = match.group("url")
        try:
            async with self.session.http.get(url) as resp:
                if resp.status != 200:
                    self.log.debug(
                        "Could not generate a preview for %s because response status was %s",
                        url,
                        resp.status,
                    )
                    return None
                if resp.content_type != "text/html":
                    self.log.debug(
                        "Could not generate a preview for %s because content type is %s",
                        url,
                        resp.content_type,
                    )
                    return None
                try:
                    html = await resp.text()
                except Exception as e:
                    self.log.debug(
                        "Could not generate a preview for %s", url, exc_info=e
                    )
                    return None
                preview = LinkPreview(Link(url, html))
                if not preview.title:
                    return None
                thumbnail = (
                    await get_url_bytes(self.session.http, preview.image)
                    if preview.image
                    else None
                )
                kind = (
                    whatsapp.PreviewVideo
                    if url.startswith(VIDEO_PREVIEW_DOMAINS)
                    else whatsapp.PreviewPlain
                )
                return whatsapp.Preview(  # type:ignore[no-untyped-call]
                    Kind=kind,
                    Title=preview.title,
                    Description=preview.description or "",
                    URL=url,
                    Thumbnail=(
                        go.Slice_byte.from_bytes(thumbnail)  # type:ignore[no-untyped-call]
                        if thumbnail
                        else go.Slice_byte()  # type:ignore[no-untyped-call]
                    ),
                )
        except Exception as e:
            self.log.debug("Could not generate a preview for %s", url, exc_info=e)
            return None

    async def __get_location(self, text: str) -> whatsapp.Location | None:
        match = search(GEO_URI_SEARCH_REGEX, text)
        if not match:
            return None
        latitude = match.group("lat")
        longitude = match.group("lon")
        if latitude == "" or longitude == "":
            return None
        return whatsapp.Location(  # type:ignore[no-untyped-call]
            Latitude=float(latitude),
            Longitude=float(longitude),
            Accuracy=int(match.group("acc") or 0),
        )

    async def on_chat_state(self, chat_state: ChatState, thread: str | None) -> None:
        match chat_state:
            case "composing":
                self.__send_state(whatsapp.ChatStateComposing)
            case "recording_audio":
                self.__send_state(whatsapp.ChatStateComposing, "audio")
            case "paused":
                self.__send_state(whatsapp.ChatStatePaused)

    def __send_state(self, kind: int, media: str = "") -> None:
        state = whatsapp.ChatState(Chat=self.get_wa_chat(), Kind=kind, Media=media)  # type:ignore[no-untyped-call]
        self.wa.SendChatState(state)  # type:ignore[no-untyped-call]

    async def can_unread_up_to(self, stanza_id):
        return await unread_up_to(self, stanza_id)

    async def can_ack_read(self, stanza_id):
        await acknowledge(self, stanza_id)

    async def on_chat_read(self, native_id):
        await sync_group_read(self, native_id)

    async def on_displayed(self, legacy_msg_id: str, thread: str | None) -> None:
        receipt = whatsapp.Receipt(  # type:ignore[no-untyped-call]
            MessageIDs=go.Slice_string([legacy_msg_id]),  # type:ignore[no-untyped-call]
            Chat=self.get_wa_chat(),
            OriginActor=await self.get_wa_actor(legacy_msg_id),
            Timestamp=round(int(time.time())),
        )
        await send_receipt(self, receipt)

    async def on_react(
        self, legacy_msg_id: str, emojis: list[str], thread: str | None
    ) -> None:
        """
        Send or remove emoji reaction to existing WhatsApp message.
        Slidge core makes sure that the emojis parameter is always empty or a
        *single* emoji.
        """
        message = whatsapp.Message(  # type:ignore[no-untyped-call]
            Kind=whatsapp.MessageReaction,
            ID=legacy_msg_id,
            Chat=self.get_wa_chat(),
            Body=emojis[0] if emojis else "",
            OriginActor=await self.get_wa_actor(legacy_msg_id),
        )
        self.wa.SendMessage(message)  # type:ignore[no-untyped-call]

    async def on_retract(self, legacy_msg_id: str, thread: str | None) -> None:
        """
        Request deletion (aka retraction) for a given WhatsApp message.
        """
        message = whatsapp.Message(  # type:ignore[no-untyped-call]
            Kind=whatsapp.MessageRevoke,
            ID=legacy_msg_id,
            Chat=self.get_wa_chat(),
        )
        self.wa.SendMessage(message)  # type:ignore[no-untyped-call]


def mention_map(mention: Mention) -> str:
    # mentions are @phonenumber, without the @s.whatsapp.net or @lid suffix
    return f"@{mention.contact.phone}"  # type:ignore


def strip_quote_prefix(text: str) -> str:
    """
    Return multi-line text without leading quote marks (i.e. the ">" character).
    """
    return "\n".join(x.lstrip(">").strip() for x in text.split("\n")).strip()


async def get_url_bytes(client: aiohttp.ClientSession, url: str) -> bytes | None:
    async with client.get(url) as resp:
        if resp.status == 200:
            return await resp.read()
    return None
