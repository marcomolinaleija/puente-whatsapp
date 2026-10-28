"""Validate the real patched DTO/binding boundary without a WhatsApp account."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from slidge_whatsapp.audio_modes import AUDIO_MODE_NS
from slidge_whatsapp.generated import whatsapp
from slidge_whatsapp.mixins import RecipientMixin


async def verify() -> None:
    for mode, mime, suffix in (
        ("audio", "audio/mpeg", "mp3"),
        ("document", "audio/wav", "wav"),
        ("document", "audio/ogg; codecs=opus", "ogg"),
    ):
        data = b"opaque audio fixture"

        class Content:
            def __init__(self, payload):
                self.payload = payload

            async def iter_chunked(self, _size):
                yield self.payload[:4]
                yield self.payload[4:]

        @asynccontextmanager
        async def get(data=data):
            yield SimpleNamespace(
                status=200,
                content_type="application/octet-stream",
                content_length=None,
                content=Content(data),
                read=AsyncMock(side_effect=AssertionError("Unbounded audio read")),
            )

        attachment = SimpleNamespace(
            content_type=mime,
            is_sticker=False,
            get=get,
            url=f"https://upload.example.test/fixture.{suffix}",
        )
        sender = SimpleNamespace(
            wa=SimpleNamespace(GenerateMessageID=lambda: "fixture-id", SendMessage=Mock()),
            get_wa_chat=lambda: whatsapp.Chat(),
            _set_reply_to=Mock(),
        )
        message = SimpleNamespace(
            attachments=[attachment],
            body=attachment.url,
            thread=f"{AUDIO_MODE_NS}:{mode}",
            reply=SimpleNamespace(msg_id="quoted-fixture"),
            is_forwarded=True,
        )
        await RecipientMixin._on_file(sender, message)
        sent = sender.wa.SendMessage.call_args.args[0]
        audio = sent.Attachments[0]
        assert audio.MIME == f"application/x-can-audio-mode;{mode};{mime}"
        assert bytes(audio.Data) == data and audio.Filename == f"fixture.{suffix}"
        assert not audio.ViewOnce
        assert not audio.Caption
        assert sent.ReplyID == "quoted-fixture" and sent.IsForwarded


if __name__ == "__main__":
    asyncio.run(verify())
    print("Explicit audio modes: real DTO/binding boundary OK (no WhatsApp send)")
