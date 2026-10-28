from __future__ import annotations

import asyncio
from pathlib import Path

from slidge_whatsapp import session as session_module
from slidge_whatsapp.generated import go, whatsapp

package_dir = Path(session_module.__file__).parent
event_source = (package_dir / "event.go").read_text(encoding="utf-8")
assert "msg.GetAccessibilityLabel()" in event_source
assert "msg.GetEmojis()" in event_source
assert "func lottieStickerAccessibilityCaption(" in event_source
assert "maxLottieStickerMetadataBytes" in event_source

core_attachment_source = (
    package_dir.parent / "slidge/core/mixins/attachment.py"
).read_text(encoding="utf-8")
assert "caption=None if attachment.is_sticker else attachment.caption" in core_attachment_source


async def verify_caption_binding() -> None:
    source = whatsapp.Attachment(  # type: ignore[no-untyped-call]
        MIME="image/webp",
        Filename="sticker.webp",
        Caption="\x1eWHATSAPP_STICKER\x1fUna tortuga levanta el pulgar.",
        Data=go.Slice_byte.from_bytes(b"RIFF"),  # type: ignore[no-untyped-call]
    )
    converted = await session_module.Attachment.convert(source)
    assert converted.caption == "Una tortuga levanta el pulgar."
    assert converted.is_sticker is True


asyncio.run(verify_caption_binding())
print("sticker accessibility runtime smoke: ok")
