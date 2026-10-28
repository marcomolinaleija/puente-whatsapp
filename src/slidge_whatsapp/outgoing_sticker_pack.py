"""Validate CAN v1 and prepare a bounded native pack envelope, off the network loop."""

from __future__ import annotations

import asyncio
import io
import json
import zipfile

from PIL import Image, ImageOps

from .sticker_pack import archive_files, compatible_webp, text

MAX_NATIVE_ITEMS = 60
MAX_NATIVE_BYTES = 32 * 1024 * 1024
_lock = asyncio.Lock()


async def prepare_outgoing_pack(data: bytes) -> bytes:
    async with _lock:
        return await asyncio.to_thread(prepare_outgoing_pack_sync, data)


def prepare_outgoing_pack_sync(data: bytes) -> bytes:
    files = archive_files(data, count=MAX_NATIVE_ITEMS + 4, limit=MAX_NATIVE_BYTES)
    metadata = json.loads(files["manifest.json"])
    if (
        not isinstance(metadata, dict)
        or metadata.get("format") != "can-stickers"
        or metadata.get("version") != 1
    ):
        raise ValueError("Unsupported CAN pack")
    entries = metadata.get("stickers")
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_NATIVE_ITEMS:
        raise ValueError("Native packs require 1 to 60 stickers")
    manifest = {
        "format": "can-native-sticker-pack",
        "version": 1,
        "name": text(metadata.get("name"), 128),
        "author": text(metadata.get("author"), 128),
        "stickers": [],
    }
    if not manifest["name"] or not manifest["author"]:
        raise ValueError("Missing pack name/author")
    payloads, seen = [], set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError("Invalid sticker descriptor")
        name = entry.get("file")
        if (
            not isinstance(name, str)
            or name not in files
            or name in seen
            or not name.lower().endswith(".webp")
        ):
            raise ValueError("Missing or duplicate WebP")
        seen.add(name)
        original = files[name]
        # Creation is the client's job: reject incompatible files rather than lossy conversion.
        if compatible_webp(original) != original:
            raise ValueError("Sticker must already meet WhatsApp requirements")
        with Image.open(io.BytesIO(original)) as image:
            animated = image.n_frames > 1
        payloads.append(original)
        manifest["stickers"].append(
            {
                "file": f"{index:03d}.webp",
                "animated": animated,
                "description": text(entry.get("description", ""), 8000),
            }
        )
    if set(files) - seen - {"manifest.json", "title.txt", "author.txt", "cover.png"}:
        raise ValueError("Unreferenced pack files")
    with Image.open(io.BytesIO(payloads[0])) as image:
        source = image.convert("RGBA")
        tray = ImageOps.pad(source, (96, 96), color=(0, 0, 0, 0))
        thumbnail = ImageOps.pad(source, (252, 252), color=(0, 0, 0, 0))
        background = Image.new("RGB", thumbnail.size, "white")
        background.paste(thumbnail, mask=thumbnail.getchannel("A"))
        tray_bytes, thumb_bytes = io.BytesIO(), io.BytesIO()
        tray.save(tray_bytes, format="WEBP", lossless=True)
        background.save(thumb_bytes, format="JPEG", quality=85)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for index, payload in enumerate(payloads):
            archive.writestr(f"{index:03d}.webp", payload)
        archive.writestr("tray.webp", tray_bytes.getvalue())
        archive.writestr("thumbnail.jpg", thumb_bytes.getvalue())
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
    if output.tell() > MAX_NATIVE_BYTES:
        raise ValueError("Native pack exceeds limits")
    return output.getvalue()
