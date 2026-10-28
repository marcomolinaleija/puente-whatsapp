import base64
import io
import logging
import uuid
import warnings
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from PIL import Image
from slixmpp import Message
from slixmpp.plugins.xep_0511.stanza import LinkMetadata
from slixmpp.types import MessageTypes

from slidge.util import strip_illegal_chars

from ...db.models import GatewayUser
from ...util.types import (
    AnyMUC,
    AnyParticipant,
    ChatState,
    LinkPreview,
    MessageReference,
    ProcessingHint,
)
from .. import config
from .base import BaseSender


class MessageMaker(BaseSender):
    mtype: MessageTypes = NotImplemented
    _can_send_carbon: bool = NotImplemented
    STRIP_SHORT_DELAY = False
    USE_STANZA_ID = False

    muc: AnyMUC

    def _recipient_pk(self) -> int:
        return (
            self.muc.stored.id if self.is_participant else self.stored.id  # type:ignore
        )

    def _make_message(
        self,
        state: ChatState | None = None,
        hints: Iterable[ProcessingHint] = (),
        legacy_msg_id: str | None = None,
        when: datetime | None = None,
        reply_to: MessageReference | None = None,
        carbon: bool = False,
        link_previews: Iterable[LinkPreview] | None = None,
        **kwargs: object,
    ) -> Message:
        body = kwargs.pop("mbody", None)
        mfrom = kwargs.pop("mfrom", self.jid)
        mto = kwargs.pop("mto", None)
        thread = kwargs.pop("thread", None)
        # the msg needs to have jabber:client as xmlns, so
        # we don't want to associate with the XML stream
        msg_cls = Message if carbon and self._can_send_carbon else self.xmpp.Message
        msg = msg_cls(
            sfrom=mfrom,
            stype=kwargs.pop("mtype", None) or self.mtype,
            sto=mto,
            **kwargs,
        )
        if body:
            assert isinstance(body, str)
            msg["body"] = strip_illegal_chars(body, "�")
            state = "active"
        if thread:
            with self.xmpp.store.session() as orm:
                thread_str = str(thread)
                msg["thread"] = (
                    self.xmpp.store.id_map.get_thread(
                        orm,
                        self._recipient_pk(),
                        thread_str,
                        self.is_participant,
                    )
                    or thread_str
                )
        if state:
            msg["chat_state"] = state
        for hint in hints:
            msg.enable(hint)
        self._set_msg_id(msg, legacy_msg_id)
        self._add_delay(msg, when)
        if link_previews:
            self._add_link_previews(msg, link_previews)
        if reply_to:
            self._add_reply_to(msg, reply_to)
        return msg

    def _set_msg_id(self, msg: Message, legacy_msg_id: str | None = None) -> None:
        if legacy_msg_id is not None:
            msg.set_id(legacy_msg_id)
            if self.USE_STANZA_ID:
                msg["stanza_id"]["id"] = legacy_msg_id
                msg["stanza_id"]["by"] = self.muc.jid
        elif self.USE_STANZA_ID:
            msg["stanza_id"]["id"] = str(uuid.uuid4())
            msg["stanza_id"]["by"] = self.muc.jid

    def _legacy_to_xmpp(self, legacy_id: str) -> list[str]:
        with self.xmpp.store.session() as orm:
            ids = self.xmpp.store.id_map.get_xmpp(
                orm,
                self._recipient_pk(),
                str(legacy_id),
                self.is_participant,
            )
            if ids:
                return ids
        return [legacy_id]

    def _add_delay(self, msg: Message, when: datetime | None) -> None:
        if when:
            if when.tzinfo is None:
                when = when.astimezone(UTC)
            if self.STRIP_SHORT_DELAY:
                delay = (datetime.now().astimezone(UTC) - when).seconds
                if delay < config.IGNORE_DELAY_THRESHOLD:
                    return
            msg["delay"].set_stamp(when)
            msg["delay"].set_from(self.xmpp.boundjid.bare)

    def _add_reply_to(self, msg: Message, reply_to: MessageReference) -> None:
        xmpp_id = self._legacy_to_xmpp(reply_to.legacy_id)[0]
        msg["reply"]["id"] = xmpp_id

        muc = getattr(self, "muc", None)

        if entity := reply_to.author:
            if entity == "user" or isinstance(entity, GatewayUser):
                if isinstance(entity, GatewayUser):
                    warnings.warn(
                        "Using a GatewayUser as the author of a "
                        "MessageReference is deprecated. Use the string 'user' "
                        "instead.",
                        DeprecationWarning,
                    )
                if muc:
                    msg["reply"]["to"] = muc.user_muc_jid
                    fallback_nick = muc.user_nick
                else:
                    msg["reply"]["to"] = self.session.user_jid
                    # TODO: here we should use preferably use the PEP nick of the user
                    # (but it doesn't matter much)
                    fallback_nick = self.session.user_jid.user
            else:
                if muc:
                    if hasattr(entity, "muc"):
                        # TODO: accept a Contact here and use muc.get_participant_by_legacy_id()
                        # a bit of work because right now this is a sync function
                        entity = cast(AnyParticipant, entity)
                        fallback_nick = entity.nickname
                    else:
                        warnings.warn(
                            "The author of a message reference in a MUC must be a"
                            " Participant instance, not a Contact"
                        )
                        fallback_nick = entity.name
                else:
                    fallback_nick = entity.name
                msg["reply"]["to"] = entity.jid
        else:
            fallback_nick = None

        if fallback := reply_to.body:
            msg["reply"].add_quoted_fallback(fallback, fallback_nick)

    def _add_link_previews(
        self, msg: Message, link_previews: Iterable[LinkPreview]
    ) -> None:
        for preview in link_previews:
            if preview.is_empty:
                continue
            element = LinkMetadata()
            for i, name in enumerate(preview._fields):
                val = preview[i]
                if isinstance(val, Path):
                    val = val.read_bytes()
                if isinstance(val, bytes):
                    val = self._process_link_preview_image(val)
                if not val:
                    continue
                element[name] = val
            msg.append(element)

    @staticmethod
    def _process_link_preview_image(data: bytes) -> str | None:
        # this will block the main thread. if this proves to be an issue in practice,
        # this could be rewritten to use the thread pool we use to resize avatars.
        try:
            image = Image.open(io.BytesIO(data))
        except Exception:
            log.exception("Skipping link preview image")
            return None

        rewrite = False
        if image.format != "JPEG":
            rewrite = True

        if any(x > MAX_LINK_PREVIEW_IMAGE_SIZE for x in image.size):
            image.thumbnail((MAX_LINK_PREVIEW_IMAGE_SIZE, MAX_LINK_PREVIEW_IMAGE_SIZE))
            rewrite = True

        if rewrite:
            with io.BytesIO() as f:
                image.save(f, format="JPEG")
                data = f.getvalue()

        return "data:image/jpeg;base64," + base64.b64encode(data).decode("utf-8")


# Instead of having a hardcoded value for this, we would ideally use
# XEP-0478: Stream Limits Advertisement to know which size is authorized.
# However, this isn't possible until XEP-0225: Component Connections is a thing.
# Prosody defaults to 512kb for s2s connection and 10Mb for c2s connections.
# Some quick tests about JPEG image:
# median size of 50 base64-encoded JPEG random RGB image
# 128x128 pixels: 14kb ± 0.04
# 256x256 pixels: 53kb ± 0.06  # sounds like a good tradeoff
# 384x384 pixels: 119kb ± 0.11
# 512x512 pixels: 211kb ± 0.13
# 640x640 pixels: 329kb ± 0.15
# 768x768 pixels: 473kb ± 0.21
# 896x896 pixels: 644kb ± 0.27
# 1024x1024 pixels: 841kb ± 0.22
MAX_LINK_PREVIEW_IMAGE_SIZE = 256

log = logging.getLogger(__name__)
