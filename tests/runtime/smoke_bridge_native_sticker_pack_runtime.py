from __future__ import annotations

import argparse
import asyncio
import io
import json
import zipfile
from pathlib import Path

from PIL import Image


def fixture() -> bytes:
    payloads = []
    for animated in (False, True):
        output = io.BytesIO()
        image = Image.new("RGBA", (512, 512), "red")
        options = (
            {
                "save_all": True,
                "append_images": [Image.new("RGBA", (512, 512), "blue")],
                "duration": [100, 200],
                "loop": 0,
            }
            if animated
            else {}
        )
        image.save(output, format="WEBP", lossless=True, **options)
        payloads.append(output.getvalue())
    metadata = {
        "format": "can-stickers",
        "version": 1,
        "name": "Fixture",
        "author": "CAN",
        "stickers": [
            {
                "file": f"{i:03d}.webp",
                "name": "Fixture",
                "description": "Una figura saluda." if i == 0 else "Una figura cambia.",
            }
            for i in range(2)
        ],
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("manifest.json", json.dumps(metadata))
        for i, data in enumerate(payloads):
            archive.writestr(f"{i:03d}.webp", data)
    return output.getvalue()


async def verify() -> None:
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import Mock

    from slidge_whatsapp.generated import whatsapp
    from slidge_whatsapp.mixins import RecipientMixin
    from slidge_whatsapp.sticker_pack import normalize_pack

    data = fixture()

    class Content:
        async def iter_chunked(self, _size):
            yield data[:70]
            yield data[70:]

    @asynccontextmanager
    async def get():
        yield SimpleNamespace(
            status=200,
            content_type="application/octet-stream",
            content_length=len(data),
            content=Content(),
        )

    sender = SimpleNamespace(
        wa=SimpleNamespace(GenerateMessageID=lambda: "fixture-id", SendMessage=Mock()),
        get_wa_chat=lambda: whatsapp.Chat(),
        _set_reply_to=Mock(),
    )
    attachment = SimpleNamespace(
        content_type="application/x-can-sticker-pack",
        is_sticker=False,
        get=get,
        url="https://upload.example.test/pack",
    )
    message = SimpleNamespace(
        attachments=[attachment],
        body=attachment.url,
        thread=None,
        reply=SimpleNamespace(msg_id="quoted-fixture"),
        is_forwarded=True,
    )
    await RecipientMixin._on_file(sender, message)
    sent = sender.wa.SendMessage.call_args.args[0]
    assert sent.Attachments[0].MIME == "application/x-can-sticker-pack"
    assert sent.ReplyID == "quoted-fixture" and sent.IsForwarded
    with zipfile.ZipFile(io.BytesIO(bytes(sent.Attachments[0].Data))) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["format"] == "can-native-sticker-pack"
        assert manifest["stickers"][0]["description"] == "Una figura saluda."
    received = Path(__file__).with_name("native-pack-roundtrip.zip").read_bytes()
    converted = await normalize_pack(received)
    with zipfile.ZipFile(io.BytesIO(converted)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["name"] == "Fixture" and len(manifest["stickers"]) == 2
        assert manifest["stickers"][0]["description"] == "Una figura saluda."
        with Image.open(io.BytesIO(archive.read("001.webp"))) as image:
            assert image.n_frames == 2
    print(
        "native pack v30: typed binding, labels, animation and Go receiver roundtrip OK"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare", action="store_true")
    args = parser.parse_args()
    if args.prepare:
        from slidge_whatsapp.outgoing_sticker_pack import prepare_outgoing_pack_sync

        Path("/tmp/native-outgoing-envelope.zip").write_bytes(prepare_outgoing_pack_sync(fixture()))
    else:
        asyncio.run(verify())
