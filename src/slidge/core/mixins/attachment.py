from __future__ import annotations

import base64
import contextlib
import functools
import io
import logging
import os
import shutil
import stat
import tempfile
import warnings
from collections.abc import AsyncIterator, Collection, Sequence
from datetime import datetime
from itertools import chain
from mimetypes import guess_extension, guess_type
from pathlib import Path
from typing import IO, Any, cast
from urllib.parse import quote as urlquote
from uuid import uuid4
from xml.etree import ElementTree as ET

import aiohttp
import thumbhash
from PIL import Image, ImageOps
from slixmpp import JID, Iq, Message
from slixmpp.plugins.xep_0264.stanza import Thumbnail
from slixmpp.plugins.xep_0447.stanza import StatelessFileSharing

from ...db.avatar import avatar_cache
from ...db.models import Attachment
from ...util.types import LegacyAttachment, MessageReference
from ...util.util import fix_suffix
from .. import config
from .message_text import TextMessageMixin, add_whatsapp_forwarded_flag


class AttachmentMixin(TextMessageMixin):
    @property
    def __is_component(self) -> bool:
        return self.session is NotImplemented

    async def __upload(self, att: _AttachmentWithName) -> str:
        assert config.UPLOAD_SERVICE
        att = await _ensure_metadata(att)
        iq_slot = await self.__request_upload_slot(
            config.UPLOAD_SERVICE,
            att.name,
            att.size,
            att.content_type,
        )
        slot = iq_slot["http_upload_slot"]
        headers = {
            "Content-Length": str(att.size),
            "Content-Type": att.content_type,
            **{header["name"]: header["value"] for header in slot["put"]["headers"]},
        }

        async with (
            aiohttp.ClientSession() as http,
            _get_data(att, http) as data,
            http.put(slot["put"]["url"], data=data, headers=headers) as resp,
        ):
            resp.raise_for_status()

        return slot["get"]["url"]  # type:ignore[no-any-return]

    async def __request_upload_slot(
        self,
        upload_service: JID | str,
        filename: str,
        size: int,
        content_type: str,
    ) -> Iq:
        iq_request = self.xmpp.make_iq_get(ito=upload_service)
        request = iq_request["http_upload_request"]
        request["filename"] = filename
        request["size"] = str(size)
        request["content-type"] = content_type
        if not self.__is_component:
            iq_request.set_from(self.session.user_jid)
            try:
                return await self.xmpp.plugin["xep_0356"].send_privileged_iq(iq_request)
            except Exception as e:
                warnings.warn(
                    "Could not request upload slot on behalf of "
                    f"{self.session.user_jid}: {e}."
                    "Falling back to not using privileges."
                )
        iq_request.set_from(config.UPLOAD_REQUESTER or self.xmpp.boundjid)
        return await iq_request.send()  # type:ignore[no-any-return]

    @staticmethod
    async def __no_upload(
        att: _AttachmentWithName, legacy_file_id: str | None
    ) -> tuple[Path, str]:
        file_id = uuid4().hex if legacy_file_id is None else legacy_file_id
        assert config.NO_UPLOAD_PATH is not None
        assert config.NO_UPLOAD_URL_PREFIX is not None
        destination_dir = Path(config.NO_UPLOAD_PATH) / file_id

        if destination_dir.exists():
            log.debug("Dest dir exists: %s", destination_dir)
            files = list(f for f in destination_dir.glob("**/*") if f.is_file())
            if len(files) == 1:
                log.debug(
                    "Found the legacy attachment '%s' at '%s'",
                    legacy_file_id,
                    files[0],
                )
                name = files[0].name
                uu = files[0].parent.name  # anti-obvious url trick, see below
                return files[0], "/".join([file_id, uu, name])
            else:
                log.warning(
                    (
                        "There are several or zero files in %s, "
                        "slidge doesn't know which one to pick among %s. "
                        "Removing the dir."
                    ),
                    destination_dir,
                    files,
                )
                shutil.rmtree(destination_dir)

        log.debug("Did not find a file in: %s", destination_dir)
        # let's use a UUID to avoid URLs being too obvious
        uu = str(uuid4())
        destination_dir = destination_dir / uu
        destination_dir.mkdir(parents=True)

        assert att.name
        destination = destination_dir / att.name
        if att.path:
            assert isinstance(att.path, Path)
            try:
                destination.hardlink_to(att.path)
            except OSError as e:
                if is_temp_path(att.path):
                    shutil.copy2(att.path, destination)
                else:
                    log.debug("Could not hardlink: %s, attempting symlink", e)
                    try:
                        destination.symlink_to(att.path)
                    except OSError as e:
                        log.debug("Could not symlink: %s, copying data", e)
                        shutil.copy2(att.path, destination)
        elif att.data:
            destination.write_bytes(att.data)
        else:
            with destination.open("wb") as f:
                if att.aio_stream:
                    async for chunk in att.aio_stream:
                        f.write(chunk)
                elif att.stream:
                    shutil.copyfileobj(att.stream, f)
                elif att.url:
                    async with (
                        aiohttp.ClientSession() as http,
                        http.get(att.url) as resp,
                    ):
                        resp.raise_for_status()
                        async for chunk in resp.content.iter_chunked(64 * 1024):
                            f.write(chunk)
                else:
                    raise RuntimeError

        if config.NO_UPLOAD_FILE_READ_OTHERS:
            log.debug("Changing perms of %s", destination)
            destination.chmod(destination.stat().st_mode | stat.S_IROTH)

        url = "/".join([file_id, uu, att.name])
        return destination, url

    async def __valid_url(self, url: str) -> bool:
        async with self.session.http.head(url) as r:
            return r.status < 400

    async def __get_stored(self, attachment: LegacyAttachment) -> Attachment:
        if attachment.legacy_file_id is not None and not self.__is_component:
            with self.xmpp.store.session() as orm:
                stored = (
                    orm.query(Attachment)
                    .filter_by(
                        legacy_file_id=str(attachment.legacy_file_id),
                        user_account_id=self.session.user_pk,
                    )
                    .one_or_none()
                )
                if stored is not None:
                    if not await self.__valid_url(stored.url):
                        stored.url = None  # type:ignore
                    return stored
        return Attachment(
            user_account_id=None if self.__is_component else self.session.user_pk,
            legacy_file_id=None
            if attachment.legacy_file_id is None
            else str(attachment.legacy_file_id),
            url=attachment.url if config.USE_ATTACHMENT_ORIGINAL_URLS else None,
        )

    async def __get_url(self, att: LegacyAttachment, stored: Attachment) -> str:
        att = _ensure_name(att)

        if len(att.name) > config.ATTACHMENT_MAXIMUM_FILE_NAME_LENGTH:
            log.debug("Trimming long filename: %s", att.name)
            base, ext = os.path.splitext(att.name)
            att.name = (
                base[: config.ATTACHMENT_MAXIMUM_FILE_NAME_LENGTH - len(ext)] + ext
            )

        if config.FIX_FILENAME_SUFFIX_MIME_TYPE and isinstance(att.path, Path):
            att.name, att.content_type = fix_suffix(
                att.path, att.content_type, att.name
            )

        att.legacy_file_id = stored.legacy_file_id

        if config.NO_UPLOAD_PATH:
            att.path, new_url = await self.__no_upload(att, stored.legacy_file_id)
            new_url = (config.NO_UPLOAD_URL_PREFIX or "") + "/" + urlquote(new_url)
        else:
            new_url = await self.__upload(att)

        if stored.legacy_file_id:
            stored.url = new_url

        return new_url

    def __set_sims(
        self,
        msg: Message,
        uploaded_url: str,
        attachment: LegacyAttachment,
        stored: Attachment,
        thumbnail: Thumbnail | None,
    ) -> None:
        if stored.sims is not None:
            ref = self.xmpp.plugin["xep_0372"].stanza.Reference(
                xml=ET.fromstring(stored.sims)
            )
            msg.append(ref)
            return

        if attachment.data or attachment.stream or attachment.path:
            ref = self.xmpp.plugin["xep_0385"].get_sims(
                attachment.path,  # type:ignore[arg-type]
                [uploaded_url],
                attachment.content_type,
                attachment.caption,
                file=attachment.stream,
                data=attachment.data,
            )
        else:
            sims = self.xmpp.plugin["xep_0385"].stanza.Sims()
            ref = self.xmpp.plugin["xep_0372"].stanza.Reference()
            ref["uri"] = uploaded_url
            ref["type"] = "data"
            sims["sources"].append(ref)
            ref = self.xmpp.plugin["xep_0372"].stanza.Reference()
            ref.append(sims)
            ref["type"] = "data"
            sims.enable("file")
            if attachment.content_type:
                sims["file"]["media-type"] = attachment.content_type
            if attachment.caption:
                sims["file"]["desc"] = attachment.caption
            if attachment.size:
                sims["file"]["size"] = attachment.size
        if attachment.name:
            ref["sims"]["file"]["name"] = attachment.name
        if attachment.aio_stream is not None:
            # revove the date in case we have downloaded the file to disk
            # because no size was provided by the legacy module
            ref["sims"]["file"]["date"] = None

        if thumbnail is not None:
            ref["sims"]["file"].append(thumbnail)

        stored.sims = str(ref)
        msg.append(ref)

    def __set_sfs(
        self,
        msg: Message,
        uploaded_url: str,
        attachment: LegacyAttachment,
        stored: Attachment,
        thumbnail: Thumbnail | None = None,
    ) -> None:
        if stored.sfs is not None:
            msg.append(StatelessFileSharing(xml=ET.fromstring(stored.sfs)))
            return

        if attachment.path or attachment.data or attachment.stream:
            sfs = self.xmpp.plugin["xep_0447"].get_sfs(  # type:ignore
                attachment.path,
                [uploaded_url],
                attachment.content_type,
                attachment.caption,
                data=attachment.data,
                file=attachment.stream,
            )
        else:
            sfs = self.xmpp.plugin["xep_0447"].stanza.StatelessFileSharing()
            ref = self.xmpp.plugin["xep_0447"].stanza.UrlData()
            ref["target"] = uploaded_url
            sfs["sources"].append(ref)
            sfs.enable("file")
            if attachment.content_type:
                sfs["file"]["media-type"] = attachment.content_type
        if attachment.name:
            sfs["file"]["name"] = attachment.name
        if attachment.disposition:
            sfs["disposition"] = attachment.disposition
        else:
            del sfs["disposition"]
        if thumbnail is not None:
            sfs["file"].append(thumbnail)
        if attachment.aio_stream is not None:
            # revove the date in case we have downloaded the file to disk
            # because no size was provided by the legacy module
            sfs["file"]["date"] = None
        if attachment.size:
            sfs["file"]["size"] = attachment.size
        stored.sfs = str(sfs)
        msg.append(sfs)

    def __send_url(
        self,
        msg: Message,
        legacy_msg_id: str | None,
        uploaded_url: str,
        caption: str | None = None,
        carbon: bool = False,
        when: datetime | None = None,
        correction: bool = False,
        **kwargs: Any,  # noqa:ANN401
    ) -> list[Message]:
        msg["oob"]["url"] = uploaded_url
        msg["body"] = uploaded_url
        if "sfs" in msg:
            msg["fallback"].enable("body")
            msg["fallback"]["for"] = self.xmpp.plugin["xep_0447"].stanza.NAMESPACE
        if caption:
            if correction:
                if not legacy_msg_id:
                    raise TypeError
                msg["replace"]["id"] = self._replace_id(legacy_msg_id)
            elif legacy_msg_id:
                self._set_msg_id(msg, legacy_msg_id)
            m1 = self._send(msg, carbon=carbon, correction=correction, **kwargs)
            m2 = self.send_text(
                caption, legacy_msg_id=None, when=when, carbon=carbon, **kwargs
            )
            return [m1, m2] if m2 else [m1]
        else:
            if correction:
                if not legacy_msg_id:
                    raise TypeError
                msg["replace"]["id"] = self._replace_id(legacy_msg_id)
            elif legacy_msg_id:
                self._set_msg_id(msg, legacy_msg_id)
            return [self._send(msg, carbon=carbon, **kwargs)]

    def __get_base_message(
        self,
        legacy_msg_id: str | None = None,
        reply_to: MessageReference | None = None,
        when: datetime | None = None,
        thread: str | None = None,
        carbon: bool = False,
        correction: bool = False,
        mto: JID | None = None,
    ) -> Message:
        if correction:
            if not legacy_msg_id:
                raise TypeError
            xmpp_ids = self._legacy_to_xmpp(legacy_msg_id)
            if xmpp_ids:
                original_xmpp_id = xmpp_ids[0]
                for xmpp_id in xmpp_ids:
                    if xmpp_id == original_xmpp_id:
                        continue
                    self.retract(xmpp_id, thread)

        if reply_to is not None and reply_to.body:
            # We cannot have a "quote fallback" for attachments since most (all?)
            # XMPP clients will only treat a message as an attachment if the
            # body is the URL and nothing else.
            reply_to_for_attachment: MessageReference | None = MessageReference(
                reply_to.legacy_id, reply_to.author
            )
        else:
            reply_to_for_attachment = reply_to

        return self._make_message(
            when=when,
            reply_to=reply_to_for_attachment,
            carbon=carbon,
            mto=mto,
            thread=thread,
        )

    async def send_file(
        self,
        attachment: LegacyAttachment | Path | str,
        legacy_msg_id: str | None = None,
        *,
        reply_to: MessageReference | None = None,
        when: datetime | None = None,
        thread: str | None = None,
        **kwargs: Any,  # noqa:ANN401
    ) -> tuple[str | None, list[Message]]:
        """
        Send a single file from this :term:`XMPP Entity`.

        :param attachment: The file to send.
            Ideally, a :class:`.LegacyAttachment` with a unique ``legacy_file_id``
            attribute set, to optimise potential future reuses.
            It can also be:
            - a :class:`pathlib.Path` instance to point to a local file, or
            - a ``str``, representing a fetchable HTTP URL.
        :param legacy_msg_id: If you want to be able to transport read markers from the gateway
            user to the legacy network, specify this
        :param reply_to: Quote another message (:xep:`0461`)
        :param when: when the file was sent, for a "delay" tag (:xep:`0203`)
        :param thread:
        """
        coro = self.__send_file(
            attachment,
            legacy_msg_id,
            reply_to=reply_to,
            when=when,
            thread=thread,
            **kwargs,
        )
        if (
            self.__is_component
            or not isinstance(attachment, LegacyAttachment)
            or attachment.legacy_file_id is None
        ):
            return await coro
        else:
            # prevents race conditions where we download the same thing several time
            # and end up attempting to insert it twice in the DB, raising an
            # IntegrityError.
            async with self.session.lock(("attachment", attachment.legacy_file_id)):
                return await coro

    async def __send_file(
        self,
        attachment: LegacyAttachment | Path | str,
        legacy_msg_id: str | None = None,
        *,
        reply_to: MessageReference | None = None,
        when: datetime | None = None,
        thread: str | None = None,
        store_multi: bool = True,
        carbon: bool = False,
        mto: JID | None = None,
        correction: bool = False,
        is_forwarded: bool = False,
        **send_kwargs: Any,  # noqa:ANN401
    ) -> tuple[str | None, list[Message]]:
        if isinstance(attachment, str):
            attachment = LegacyAttachment(url=attachment)
        elif isinstance(attachment, Path):
            attachment = LegacyAttachment(path=attachment)

        msg = self.__get_base_message(
            legacy_msg_id=legacy_msg_id,
            reply_to=reply_to,
            when=when,
            thread=thread,
            carbon=carbon,
            correction=correction,
            mto=mto,
        )
        add_whatsapp_forwarded_flag(msg, is_forwarded)
        if attachment.is_sticker:
            msg.enable("sticker")

        stored = await self.__get_stored(attachment)

        if attachment.content_type is None and (
            name := (attachment.name or attachment.url or attachment.path)
        ):
            attachment.content_type, _ = guess_type(name)

        if not attachment.name:
            if attachment.url:
                attachment.url.split("/")[-1]
            elif isinstance(attachment.path, Path):
                attachment.name = attachment.path.name
            else:
                attachment.name = uuid4().hex
                if attachment.content_type:
                    ext = guess_extension(attachment.content_type)
                    if ext:
                        attachment.name += ext

        if stored.url:
            new_url = stored.url
        else:
            try:
                new_url = await self.__get_url(attachment, stored)
            except Exception as e:
                log.error("Error with attachment: %s: %s", attachment, e)
                log.debug("", exc_info=e)
                msg["body"] = (
                    f"/me tried to send a file ({attachment.format_for_user()}), "
                    f"but something went wrong: {e}. "
                )
                self._set_msg_id(msg, legacy_msg_id)
                return None, [self._send(msg, **send_kwargs)]
            stored.url = new_url
        attachment.url = new_url
        thumbnail = await self.__get_thumbnail(attachment)
        self.__set_sims(msg, new_url, attachment, stored, thumbnail)
        self.__set_sfs(msg, new_url, attachment, stored, thumbnail)

        if isinstance(attachment.path, Path) and is_temp_path(
            attachment.path, async_iterator_download_only=True
        ):
            attachment.path.unlink()
            attachment.path = None

        if not self.__is_component:
            with self.xmpp.store.session(expire_on_commit=False) as orm:
                orm.add(stored)
                orm.commit()

        msgs = self.__send_url(
            msg,
            legacy_msg_id,
            uploaded_url=new_url,
            caption=None if attachment.is_sticker else attachment.caption,
            carbon=carbon,
            when=when,
            correction=correction,
            **send_kwargs,
        )
        if not self.__is_component and store_multi and legacy_msg_id:
            self.__store_multi(legacy_msg_id, msgs)

        return new_url, msgs

    def __send_body(
        self,
        body: str | None = None,
        legacy_msg_id: str | None = None,
        reply_to: MessageReference | None = None,
        when: datetime | None = None,
        thread: str | None = None,
        **kwargs: Any,  # noqa:ANN401
    ) -> Message | None:
        if body:
            return self.send_text(
                body,
                legacy_msg_id,
                reply_to=reply_to,
                when=when,
                thread=thread,
                **kwargs,
            )
        else:
            return None

    async def send_files(
        self,
        attachments: Collection[LegacyAttachment],
        legacy_msg_id: str | None = None,
        body: str | None = None,
        *,
        reply_to: MessageReference | None = None,
        when: datetime | None = None,
        thread: str | None = None,
        body_first: bool = False,
        correction: bool = False,
        correction_event_id: str | None = None,
        **kwargs: Any,  # noqa:ANN401
    ) -> None:
        # TODO: once the epic XEP-0385 vs XEP-0447 battle is over, pick
        #       one and stop sending several attachments this way
        # we attach the legacy_message ID to the last message we send, because
        # we don't want several messages with the same ID (especially for MUC MAM)
        if not attachments and not body:
            # ignoring empty message
            return
        body_msg_id = (
            legacy_msg_id if body_needs_msg_id(attachments, body, body_first) else None
        )
        send_body = functools.partial(
            self.__send_body,
            body=body,
            reply_to=reply_to,
            when=when,
            thread=thread,
            correction=correction,
            legacy_msg_id=body_msg_id,
            correction_event_id=correction_event_id,
            **kwargs,
        )
        all_msgs = []
        if body_first:
            all_msgs.append(send_body())
        for i, attachment in enumerate(attachments):
            legacy = legacy_msg_id if i == 0 and body_msg_id is None else None
            _url, msgs = await self.send_file(
                attachment,
                legacy,
                reply_to=reply_to,
                when=when,
                thread=thread,
                store_multi=False,
                **kwargs,
            )
            all_msgs.extend(msgs)
        if not body_first:
            all_msgs.append(send_body())
        self.__store_multi(legacy_msg_id, all_msgs)

    def __store_multi(
        self,
        legacy_msg_id: str | None,
        all_msgs: Sequence[Message | None],
    ) -> None:
        if legacy_msg_id is None:
            return
        ids = []
        for msg in all_msgs:
            if not msg:
                continue
            if stanza_id := msg.get_plugin("stanza_id", check=True):
                ids.append(stanza_id["id"])
            else:
                ids.append(msg.get_id())
        with self.xmpp.store.session() as orm:
            self.xmpp.store.id_map.set_msg(
                orm, self._recipient_pk(), str(legacy_msg_id), ids, self.is_participant
            )
            orm.commit()

    async def __get_thumbnail(self, attachment: LegacyAttachment) -> Thumbnail | None:
        if attachment.content_type is None:
            return None
        if not (attachment.data or attachment.stream or attachment.path):
            return None
        if not attachment.content_type.startswith("image"):
            return None

        try:
            h, x, y = await self.xmpp.loop.run_in_executor(
                avatar_cache._thread_pool,
                get_thumbhash,
                attachment,
            )
        except Exception as e:
            log.debug("Could not generate a thumbhash", exc_info=e)
            return None

        thumbnail = Thumbnail()
        thumbnail["width"] = x
        thumbnail["height"] = y
        thumbnail["media-type"] = "image/thumbhash"
        thumbnail["uri"] = "data:image/thumbhash;base64," + urlquote(h)
        return thumbnail


class _AttachmentWithName(LegacyAttachment):
    name: str
    path: Path | None


class _AttachmentWithMetadata(_AttachmentWithName):
    size: int
    content_type: str


def _ensure_name(att: LegacyAttachment) -> _AttachmentWithName:
    if not att.name:
        if att.path:
            att.name = Path(att.path).name
        elif att.url:
            att.name = att.url.split("/")[-1]
        else:
            att.name = "unnamed-file"
    return cast(_AttachmentWithName, att)


async def _ensure_metadata(att: _AttachmentWithName) -> _AttachmentWithMetadata:
    if att.size is None:
        if att.data:
            att.size = len(att.data)
        elif att.stream:
            att.stream.seek(0, io.SEEK_END)
            att.size = att.stream.tell()
            att.stream.seek(0)
        elif att.path:
            assert isinstance(att.path, Path)
            att.size = att.path.stat().st_size
        elif att.url:
            async with (
                aiohttp.ClientSession() as http,
                http.head(att.url) as resp,
            ):
                att.size = resp.content_length
        elif att.aio_stream:
            warnings.warn("A size should be passed with async iterators")
            tmp_dir = Path(tempfile.mkdtemp(prefix=_TEMP_PREFIX))
            with (tmp_dir / att.name).open("wb") as fp:
                async for chunk in att.aio_stream:
                    fp.write(chunk)
            att.path = Path(fp.name)
            att.size = att.path.stat().st_size

    if not att.content_type:
        att.content_type, _encoding = guess_type(att.name)
        if not att.content_type:
            att.content_type = "application/octet-stream"

    return cast(_AttachmentWithMetadata, att)


def body_needs_msg_id(
    attachments: Collection[LegacyAttachment], body: str | None, body_first: bool
) -> bool:
    if attachments:
        return bool(body and body_first)
    else:
        return True


def get_thumbhash(att: LegacyAttachment) -> tuple[str, int, int]:
    img, width, height = get_image(att)
    rgba_2d = list(img.get_flattened_data())
    rgba = list(chain(*rgba_2d))  # type:ignore[arg-type,var-annotated]
    ints = thumbhash.rgba_to_thumb_hash(img.width, img.height, rgba)
    return base64.b64encode(bytes(ints)).decode(), width, height


def get_image(att: LegacyAttachment) -> tuple[Image.Image, int, int]:
    if att.data:
        img = Image.open(io.BytesIO(att.data))
    elif att.path:
        with cast(Path, att.path).open("rb") as fp:
            img = Image.open(fp)
            img.load()
    elif att.stream:
        att.stream.seek(0)
        img = Image.open(att.stream)
    else:
        raise RuntimeError("No way to read the image")
    width, height = img.size
    rgba = img.convert("RGBA")
    if width > 100 or height > 100:
        rgba.thumbnail((100, 100))
    return ImageOps.exif_transpose(rgba), width, height


def is_temp_path(path: Path, async_iterator_download_only: bool = False) -> bool:
    try:
        rel = path.relative_to(_TEMP_ROOT)
    except ValueError:
        return False
    if async_iterator_download_only:
        return rel.parts[0].startswith(_TEMP_PREFIX)
    else:
        return True


@contextlib.asynccontextmanager
async def _get_data(
    att: LegacyAttachment, http: aiohttp.ClientSession
) -> AsyncIterator[bytes | IO[bytes] | AsyncIterator[bytes]]:
    if (data := att.data or att.stream or att.aio_stream) is not None:
        yield data
    elif att.path is not None:
        assert isinstance(att.path, Path)
        with att.path.open("rb") as fp:
            yield fp
    elif att.url is not None:
        async with http.get(att.url) as resp_get:
            resp_get.raise_for_status()
            yield resp_get.content.iter_any()
    else:
        raise RuntimeError("NEVER")


_TEMP_ROOT = Path(tempfile.gettempdir())
_TEMP_PREFIX = "slidge-async-iterator-download"


log = logging.getLogger(__name__)
