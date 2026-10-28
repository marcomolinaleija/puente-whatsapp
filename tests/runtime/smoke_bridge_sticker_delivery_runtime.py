from __future__ import annotations

import asyncio
import hashlib
import io
import json
import tempfile
import zipfile
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from xml.etree import ElementTree as ET

from PIL import Image
from slidge.core import config
from slidge.core.dispatcher.message.message import MessageContentMixin, _IncomingAttachment
from slidge.db.models import Bob
from slidge.db.store import BobStore
from slidge.util.types import Sticker
from slidge_whatsapp.generated import go, whatsapp
from slidge_whatsapp.mixins import RecipientMixin
from slidge_whatsapp.session import Attachment
from slidge_whatsapp.sticker_pack import normalize_pack
from slixmpp import ClientXMPP, Message
from slixmpp.exceptions import XMPPError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session


async def verify_real_bob_cache() -> None:
    """Repeat the complete dispatch path with real SQLite/BoB, without WA accounts."""
    with tempfile.TemporaryDirectory() as directory:
        had_home = hasattr(config, "HOME_DIR")
        old_home = getattr(config, "HOME_DIR", None)
        engine = create_engine("sqlite:///:memory:")
        try:
            config.HOME_DIR = Path(directory)
            Bob.metadata.create_all(engine, tables=[Bob.__table__])
            bob = BobStore()

            @contextmanager
            def session():
                with Session(engine) as orm:
                    yield orm

            payload = io.BytesIO()
            Image.new("RGBA", (512, 512), "green").save(payload, format="WEBP", lossless=True)
            data = payload.getvalue()
            cid = "sha256+" + hashlib.sha256(data).hexdigest()

            @asynccontextmanager
            async def download():
                yield SimpleNamespace(raise_for_status=Mock(), read=AsyncMock(return_value=data))

            dispatcher = SimpleNamespace(
                xmpp=SimpleNamespace(store=SimpleNamespace(session=session, bob=bob))
            )
            wa = SimpleNamespace(GenerateMessageID=lambda: "fixture-send", SendMessage=Mock())
            sender = SimpleNamespace(wa=wa, get_wa_chat=lambda: whatsapp.Chat())
            fallback = AsyncMock()

            async def send(sticker):
                return await RecipientMixin.on_sticker(sender, sticker)

            recipient = SimpleNamespace(on_sticker=send, on_message=fallback)
            dispatch = MessageContentMixin._MessageContentMixin__dispatch_nonbob_sticker
            for key, label in (
                (None, "Primera etiqueta"),
                (None, "Etiqueta nueva"),
                (cid, "Con CID"),
                (None, ""),
            ):
                incoming = _IncomingAttachment(
                    SimpleNamespace(
                        get=download,
                        content_type="image/webp",
                        url="https://upload.example.test/fixture.webp",
                    ),
                    is_sticker=True,
                    cid=key,
                    description=label,
                )
                await dispatch(
                    dispatcher,
                    incoming,
                    recipient,
                    "URL fallback",
                    reply=SimpleNamespace(msg_id="quoted-fixture"),
                    thread="fixture",
                )
                native = wa.SendMessage.call_args.args[0]
                assert native.Attachments[0].Caption == label
                assert native.Attachments[0].MIME == "application/x-whatsapp-can-sticker"
                assert bytes(native.Attachments[0].Data) == data
                assert native.ReplyID == "quoted-fixture"
            fallback.assert_not_called()
            assert wa.SendMessage.call_count == 4
            with session() as orm:
                assert orm.query(Bob).count() == 1
                cached = bob.get_sticker(orm, cid)
                assert cached.path.read_bytes() == data
                assert cached.fallback is None and cached.reply is None
            assert len(list(bob.root_dir.iterdir())) == 1
        finally:
            if had_home:
                config.HOME_DIR = old_home
            else:
                del config.HOME_DIR
            engine.dispose()


async def verify() -> None:
    await verify_real_bob_cache()
    data = b"RIFFfixtureWEBP"
    cid = "sha256+" + hashlib.sha256(data).hexdigest()
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "fixture.webp"
        path.write_bytes(data)
        stored = Sticker(path=path, content_type="image/webp", hashes={"sha-256": "fixture"})
        cache = {}

        @contextmanager
        def session():
            yield SimpleNamespace(commit=Mock())

        def insert(_orm, key, _data, _mime):
            if key in cache:
                return None
            cache[key] = stored
            return stored

        store = SimpleNamespace(
            session=session,
            bob=SimpleNamespace(get_sticker=lambda _orm, key: cache.get(key), set_sticker=insert),
        )
        dispatcher = SimpleNamespace(xmpp=SimpleNamespace(store=store))

        @asynccontextmanager
        async def download():
            yield SimpleNamespace(raise_for_status=Mock(), read=AsyncMock(return_value=data))

        recipient = SimpleNamespace(
            on_sticker=AsyncMock(return_value="native-id"), on_message=AsyncMock()
        )
        dispatch = MessageContentMixin._MessageContentMixin__dispatch_nonbob_sticker
        for key, label in (
            (None, "Primera etiqueta"),
            (None, "Otra etiqueta"),
            (cid, "Etiqueta con CID"),
            (None, ""),
        ):
            attachment = _IncomingAttachment(
                SimpleNamespace(
                    get=download,
                    content_type="image/webp",
                    url="https://upload.example.test/fixture",
                ),
                is_sticker=True,
                cid=key,
                description=label,
            )
            assert (
                await dispatch(
                    dispatcher,
                    attachment,
                    recipient,
                    "URL fallback",
                    reply="quoted-fixture",
                    thread="thread-fixture",
                )
                == "native-id"
            )
            sent = recipient.on_sticker.call_args.args[0]
            assert sent.fallback == (label or None)
            assert sent.reply == "quoted-fixture" and sent.thread == "thread-fixture"
            assert stored.fallback is None and stored.reply is None
        recipient.on_message.assert_not_called()

        # A genuine cache failure must be an XMPP error, never an apparently sent link.
        store.bob.get_sticker = lambda *_: None
        store.bob.set_sticker = lambda *_: None
        try:
            await dispatch(dispatcher, attachment, recipient, "fallback")
        except XMPPError:
            pass
        else:
            raise AssertionError("invalid sticker did not fail")

        # Exercise the installed parser and compiled gopy Attachment/Message binding.
        client = ClientXMPP("fixture@example.test", "unused")
        for plugin in ("xep_0447", "xep_0385", "xep_0449"):
            client.register_plugin(plugin)
        for namespace, outer in (
            ("urn:xmpp:sfs:0", "file-sharing"),
            ("urn:xmpp:sims:1", "media-sharing"),
        ):
            message = Message()
            ET.SubElement(message.xml, "{urn:xmpp:stickers:0}sticker")
            root = message.xml
            if outer == "media-sharing":
                root = ET.SubElement(root, "{urn:xmpp:reference:0}reference", {"type": "data"})
            sharing = ET.SubElement(root, f"{{{namespace}}}{outer}")
            file = ET.SubElement(sharing, "{urn:xmpp:file:metadata:0}file")
            ET.SubElement(file, "{urn:xmpp:file:metadata:0}desc").text = "Una figura saluda."
            ET.SubElement(file, "{urn:xmpp:file:metadata:0}media-type").text = "image/webp"
            sources = ET.SubElement(sharing, f"{{{namespace}}}sources")
            if outer == "file-sharing":
                ET.SubElement(
                    sources,
                    "{http://jabber.org/protocol/url-data}url-data",
                    {"target": "https://upload.example.test/fixture.webp"},
                )
            else:
                ET.SubElement(
                    sources,
                    "{urn:xmpp:reference:0}reference",
                    {"type": "data", "uri": "https://upload.example.test/fixture.webp"},
                )
            # Network stanzas are constructed from a complete XML tree; direct ET edits
            # after Message() don't refresh Slixmpp's instantiated plugin cache.
            message = Message(xml=message.xml)
            parsed = MessageContentMixin._MessageContentMixin__get_attachments(dispatcher, message)
            assert len(parsed) == 1 and parsed[0].description == "Una figura saluda."
        sender = SimpleNamespace(
            wa=SimpleNamespace(GenerateMessageID=lambda: "fixture-id", SendMessage=Mock()),
            get_wa_chat=lambda: whatsapp.Chat(),
        )
        sticker = Sticker(
            path=path,
            content_type="image/webp",
            hashes={},
            fallback="Una figura saluda.",
            reply=SimpleNamespace(msg_id="quote"),
        )
        await RecipientMixin.on_sticker(sender, sticker)
        native = sender.wa.SendMessage.call_args.args[0]
        assert native.Attachments[0].Caption == "Una figura saluda." and native.ReplyID == "quote"
        assert native.Attachments[0].MIME == "application/x-whatsapp-can-sticker"

    # The fixture was produced by tested Go reception, not a Python-only mock envelope.
    envelope = Path(__file__).with_name("native-pack-fixture.zip").read_bytes()
    incoming = whatsapp.Attachment(
        MIME="application/x-whatsapp-sticker-pack",
        Filename="stickers.canstickers",
        Caption="Paquete de stickers: Fixture",
        Data=go.Slice_byte.from_bytes(envelope),
    )
    converted = await Attachment.convert(incoming)
    assert converted.name == "stickers.canstickers" and converted.content_type == "application/zip"
    assert not converted.is_sticker
    with zipfile.ZipFile(io.BytesIO(converted.data)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["format"] == "can-stickers"
        assert manifest["stickers"][0]["description"] == "Una figura saluda."
        with Image.open(io.BytesIO(archive.read("000.webp"))) as bitmap:
            assert bitmap.size == (512, 512) and bitmap.format == "WEBP"
    broken = whatsapp.Attachment(
        MIME="application/x-whatsapp-sticker-pack",
        Data=go.Slice_byte.from_bytes(b"broken"),
        Caption="private unused metadata",
    )
    failure = await Attachment.convert(broken)
    assert failure.content_type == "text/plain" and "private" not in failure.caption

    # Render a moving WAS/Lottie fixture; do not flatten it or leak external resources.
    from smoke_bridge_stickers_runtime import LOTTIE_SMOKE

    animation = json.loads(json.dumps(LOTTIE_SMOKE))
    animation["layers"][0]["ks"]["p"] = {
        "a": 1,
        "k": [
            {
                "t": 0,
                "s": [16, 32, 0],
                "e": [48, 32, 0],
                "o": {"x": [0.3], "y": [0.3]},
                "i": {"x": [0.7], "y": [0.7]},
            },
            {"t": 29, "s": [48, 32, 0]},
        ],
    }
    lottie_zip = io.BytesIO()
    with zipfile.ZipFile(lottie_zip, "w") as archive:
        archive.writestr("animation/animation.json", json.dumps(animation))
    metadata = {
        "format": "whatsapp-sticker-pack",
        "version": 1,
        "name": "Fixture",
        "author": "CAN",
        "stickers": [
            {
                "file": "000.was",
                "name": "Movimiento",
                "description": "Una figura se mueve.",
                "lottie": True,
            }
        ],
    }
    pack_zip = io.BytesIO()
    with zipfile.ZipFile(pack_zip, "w") as archive:
        archive.writestr("000.was", lottie_zip.getvalue())
        archive.writestr("manifest.json", json.dumps(metadata))
    rendered = await normalize_pack(pack_zip.getvalue())
    with zipfile.ZipFile(io.BytesIO(rendered)) as archive:
        with Image.open(io.BytesIO(archive.read("000.webp"))) as image:
            assert image.n_frames > 1 and image.size == (512, 512)


asyncio.run(verify())
print("sticker v29 runtime: cache, native binding, alt text, replies and pack import envelope OK")
