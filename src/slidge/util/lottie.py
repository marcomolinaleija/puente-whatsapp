"""
This module implements vector animated stickers in the lottie format to webp images.


"""

import asyncio
import logging
import warnings
from pathlib import Path

import aiohttp

try:
    from rlottie_python.rlottie_wrapper import LottieAnimation
except ImportError:
    LottieAnimation = None  # type: ignore[assignment,misc]

from ..core import config
from .types import LegacyAttachment


async def from_url(
    url: str, sticker_id: str, http: aiohttp.ClientSession
) -> LegacyAttachment:
    """
    Get a webp attachment from a URL.

    :param url: URL where the lottie sticker can be downloaded.
    :param sticker_id: A unique identifier for this sticker.
    :param http: The aiohttp.ClientSession used to download the sticker.

    :return: A `LegacyAttachment` with the sticker in the webp format if
        `config.CONVERT_STICKERS == True`, in the original lottie format
        otherwise.
    """
    if not config.CONVERT_STICKERS:
        return _attachment(sticker_id, url=url)
    lottie_path = sticker_path(sticker_id).with_suffix(".json")
    await _download(lottie_path, url, http)
    webp_path = sticker_path(sticker_id).with_suffix(".webp")
    return await convert(lottie_path, webp_path, sticker_id)


async def from_path(path: Path, sticker_id: str) -> LegacyAttachment:
    """
    Get a webp attachment from a path.

    :param path: path to the lottie sticker file.
    :param sticker_id: A unique identifier for this sticker.

    :return: A `LegacyAttachment` with the sticker in the webp format if
        `config.CONVERT_STICKERS == True`, in the original lottie format
        otherwise.
    """
    if not config.CONVERT_STICKERS:
        return _attachment(sticker_id, path=path)
    out_path = sticker_path(sticker_id).with_suffix(".webp")
    return await convert(path, out_path, sticker_id)


def sticker_path(sticker_id: str = "") -> Path:
    """
    Get the path where a sticker is meant be stored

    :param sticker_id: A unique ID for this sticker
    :return: A `path`. It has no suffix, so you might want to use `Path.with_suffix()`
        before writing into it.
    """
    root = config.HOME_DIR / "lottie"
    root.mkdir(exist_ok=True)
    return root / sticker_id


def _attachment(
    sticker_id: str, path: Path | None = None, url: str | None = None
) -> LegacyAttachment:
    content_type = "image/webp" if path is not None and path.suffix == ".webp" else None
    return LegacyAttachment(
        url=url,
        path=path,
        legacy_file_id="lottie-" + sticker_id,
        disposition="inline",
        content_type=content_type,
        is_sticker=True,
    )


async def _download(path: Path, url: str, http: aiohttp.ClientSession) -> None:
    async with _sticker_download_lock:
        if path.exists():
            return
        with path.open("wb") as fp:
            try:
                resp = await http.get(url, chunked=True)
                async for chunk in resp.content:
                    fp.write(chunk)
            except ValueError as e:
                if e.args[0] != "Chunk too big":
                    raise
                # not sure why this happens but it does sometimes
                resp = await http.get(url)
                fp.write(await resp.content.read())


async def convert(
    input_path: Path,
    output_path: Path,
    sticker_id: str,
    width: int = 256,
    height: int = 256,
) -> LegacyAttachment:
    async with _sticker_conversion_lock:
        if not output_path.exists():
            if LottieAnimation is None:
                warnings.warn(
                    "Cannot convert stickers, rlottie-python is not available."
                )
                return _attachment(sticker_id, path=input_path)

            log.debug("Converting sticker %s to video", output_path.stem)
            if input_path.suffix == ".json":
                animation = LottieAnimation.from_file(str(input_path))
            else:
                animation = LottieAnimation.from_tgs(str(input_path))
            animation.save_animation(str(output_path), width=width, height=height)

        return _attachment(sticker_id, path=output_path)


async def _main() -> None:
    # small entrypoint to easily test that it works
    import sys

    source, destination = sys.argv[1:]
    config.CONVERT_STICKERS = True
    await convert(Path(source), Path(destination), "")


_sticker_conversion_lock: asyncio.Lock = asyncio.Lock()
_sticker_download_lock: asyncio.Lock = asyncio.Lock()


log = logging.getLogger(__name__)


__all__ = ("from_url",)

if __name__ == "__main__":
    asyncio.run(_main())
