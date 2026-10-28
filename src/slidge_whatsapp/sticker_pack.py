"""Normalize authenticated WhatsApp packs into CAN v1, off the XMPP loop."""

from __future__ import annotations

import asyncio
import io
import json
import math
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from PIL import Image, ImageOps

MAX_BYTES = 120 * 1024 * 1024
MAX_ASSET = 5 * 1024 * 1024
_lock = asyncio.Lock()


async def normalize_pack(data: bytes) -> bytes:
    async with _lock:
        return await asyncio.to_thread(normalize_pack_sync, data)


def archive_files(data: bytes, *, count: int, limit: int) -> dict[str, bytes]:
    if not data or len(data) > limit:
        raise ValueError("Archive exceeds limits")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        if len(infos) > count:
            raise ValueError("Too many archive entries")
        seen, total = set(), 0
        for info in infos:
            path = PurePosixPath(info.filename)
            if (
                path.is_absolute()
                or ".." in path.parts
                or not path.parts
                or any(c in info.filename for c in "\\:\0")
                or stat.S_ISLNK(info.external_attr >> 16)
                or info.flag_bits & 1
                or info.filename.casefold() in seen
                or info.file_size > MAX_ASSET
            ):
                raise ValueError("Unsafe archive entry")
            seen.add(info.filename.casefold())
            total += info.file_size
            if total > limit:
                raise ValueError("Expanded archive exceeds limits")
        return {info.filename: archive.read(info) for info in infos if not info.is_dir()}


def text(value: object, limit: int) -> str:
    if not isinstance(value, str) or len(value) > limit or "\0" in value:
        raise ValueError("Invalid pack metadata")
    return value.strip()


def lottie_webp(data: bytes) -> bytes:
    from rlottie_python.rlottie_wrapper import LottieAnimation

    files = archive_files(data, count=64, limit=MAX_ASSET)
    filename = "animation/animation.json"
    animation = json.loads(files[filename])
    if not isinstance(animation, dict):
        raise ValueError("Invalid animation")
    if any(
        not isinstance(animation.get(k), (int, float)) or not 1 <= animation[k] <= 1280
        for k in ("w", "h")
    ):
        raise ValueError("Invalid animation dimensions")
    if not isinstance(animation.get("layers", []), list) or len(animation["layers"]) > 512:
        raise ValueError("Too many animation layers")
    rate, start, end = (animation.get(k) for k in ("fr", "ip", "op"))
    if not all(
        isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
        for v in (rate, start, end)
    ):
        raise ValueError("Invalid animation timing")
    if not 1 <= rate <= 120 or not 1 <= end - start <= 300 or (end - start) / rate > 10:
        raise ValueError("Animation exceeds limits")
    # rlottie resolves external assets relative to JSON. Only bounded local images are allowed.
    assets = animation.get("assets", [])
    if not isinstance(assets, list) or len(assets) > 64:
        raise ValueError("Too many animation assets")
    for asset in assets:
        if not isinstance(asset, dict):
            raise ValueError("Invalid asset")
        if "p" not in asset:
            continue
        prefix, image = asset.get("u", ""), asset["p"]
        if not isinstance(prefix, str) or not isinstance(image, str):
            raise ValueError("Invalid asset path")
        if asset.get("e") == 1 and image.startswith("data:image/"):
            if len(image) > 2 * 1024 * 1024:
                raise ValueError("Embedded asset too large")
            continue
        target = PurePosixPath("animation") / prefix / image
        if target.is_absolute() or ".." in target.parts or any(c in str(target) for c in "\\:\0"):
            raise ValueError("External animation asset forbidden")
        if str(target) not in files:
            raise ValueError("Animation asset missing")
        with Image.open(io.BytesIO(files[str(target)])) as bitmap:
            if bitmap.width * bitmap.height > 16_000_000:
                raise ValueError("Animation asset too large")
    with tempfile.TemporaryDirectory(prefix="can-sticker-pack-") as directory:
        root = Path(directory)
        for name, payload in files.items():
            destination = root.joinpath(*PurePosixPath(name).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
        rendered = root / "rendered.webp"
        with LottieAnimation.from_file(str(root / filename)) as renderer:
            renderer.save_animation(str(rendered), width=512, height=512)
        return rendered.read_bytes()


def compatible_webp(data: bytes) -> bytes:
    with Image.open(io.BytesIO(data)) as image:
        if image.format not in {"WEBP", "PNG"} or image.width * image.height > 16_000_000:
            raise ValueError("Unsupported sticker image")
        animated = image.n_frames > 1
        if animated:
            if image.format != "WEBP" or image.size != (512, 512) or len(data) > 500 * 1024:
                raise ValueError("Animated sticker incompatible; animation is not flattened")
            elapsed = 0
            if image.n_frames > 300:
                raise ValueError("Too many frames")
            for index in range(image.n_frames):
                image.seek(index)
                image.load()
                duration = image.info.get("duration", 0)
                elapsed += duration
                if duration < 8 or elapsed > 10_000:
                    raise ValueError("Animation timing exceeds limits")
            return data
        image.load()
        if image.format == "WEBP" and image.size == (512, 512) and len(data) <= 100 * 1024:
            return data
        frame = ImageOps.pad(
            ImageOps.exif_transpose(image).convert("RGBA"), (512, 512), color=(0, 0, 0, 0)
        )
        for quality in (None, 90, 75, 55, 35):
            output = io.BytesIO()
            frame.save(
                output, format="WEBP", lossless=quality is None, quality=quality or 100, method=6
            )
            if output.tell() <= 100 * 1024:
                return output.getvalue()
        raise ValueError("Sticker cannot fit WhatsApp limits")


def normalize_pack_sync(data: bytes) -> bytes:
    files = archive_files(data, count=201, limit=MAX_BYTES)
    metadata = json.loads(files["manifest.json"])
    if (
        not isinstance(metadata, dict)
        or metadata.get("format") != "whatsapp-sticker-pack"
        or metadata.get("version") != 1
    ):
        raise ValueError("Unsupported native pack envelope")
    entries = metadata.get("stickers")
    if not isinstance(entries, list) or not 1 <= len(entries) <= 200:
        raise ValueError("Invalid pack entries")
    manifest = {
        "format": "can-stickers",
        "version": 1,
        "name": text(metadata.get("name"), 128),
        "author": text(metadata.get("author"), 128),
        "stickers": [],
    }
    if not manifest["name"] or not manifest["author"]:
        raise ValueError("Missing pack name/author")
    output, seen = io.BytesIO(), set()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise ValueError("Invalid sticker entry")
            filename = entry.get("file")
            if not isinstance(filename, str) or filename not in files or filename in seen:
                raise ValueError("Missing or duplicate asset")
            seen.add(filename)
            data = files[filename]
            if entry.get("lottie") is True:
                data = lottie_webp(data)
            data = compatible_webp(data)
            destination = f"{index:03d}.webp"
            archive.writestr(destination, data)
            manifest["stickers"].append(
                {
                    "file": destination,
                    "name": text(entry.get("name"), 128),
                    "description": text(entry.get("description", ""), 8000),
                }
            )
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
    if output.tell() > MAX_BYTES:
        raise ValueError("Output pack too large")
    return output.getvalue()
