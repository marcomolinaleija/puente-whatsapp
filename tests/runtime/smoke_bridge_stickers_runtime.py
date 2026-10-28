from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from slidge.core import config
from slidge.util.lottie import LottieAnimation, convert

LOTTIE_SMOKE = {
    "v": "5.7.4",
    "fr": 30,
    "ip": 0,
    "op": 30,
    "w": 64,
    "h": 64,
    "nm": "bridge-sticker-smoke",
    "ddd": 0,
    "assets": [],
    "layers": [
        {
            "ddd": 0,
            "ind": 1,
            "ty": 4,
            "nm": "square",
            "sr": 1,
            "ks": {
                "o": {"a": 0, "k": 100},
                "r": {"a": 0, "k": 0},
                "p": {"a": 0, "k": [32, 32, 0]},
                "a": {"a": 0, "k": [0, 0, 0]},
                "s": {"a": 0, "k": [100, 100, 100]},
            },
            "ao": 0,
            "shapes": [
                {
                    "ty": "rc",
                    "d": 1,
                    "s": {"a": 0, "k": [32, 32]},
                    "p": {"a": 0, "k": [0, 0]},
                    "r": {"a": 0, "k": 4},
                    "nm": "rectangle",
                },
                {
                    "ty": "fl",
                    "c": {"a": 0, "k": [0.2, 0.7, 0.3, 1]},
                    "o": {"a": 0, "k": 100},
                    "r": 1,
                    "nm": "fill",
                },
            ],
            "ip": 0,
            "op": 30,
            "st": 0,
            "bm": 0,
        }
    ],
}


async def main() -> None:
    assert LottieAnimation is not None, "rlottie-python is not available"
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        source = root / "sticker.json"
        destination = root / "sticker.webp"
        source.write_text(json.dumps(LOTTIE_SMOKE), encoding="utf-8")
        config.HOME_DIR = root
        config.CONVERT_STICKERS = True

        attachment = await convert(source, destination, "smoke", width=64, height=64)

        assert attachment.path == destination
        assert attachment.content_type == "image/webp"
        assert attachment.is_sticker
        payload = destination.read_bytes()
        assert payload.startswith(b"RIFF") and payload[8:12] == b"WEBP"
        assert len(payload) > 100

    print("Bridge sticker smoke test passed: Lottie converted to animated WebP.")


if __name__ == "__main__":
    asyncio.run(main())
