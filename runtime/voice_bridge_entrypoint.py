"""Inherit the private socket before loading gopy/Go's environment snapshot."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def prepare_runtime(home: Path) -> str:
    home = home.resolve(strict=True)
    if not home.is_dir():
        raise ValueError("Bridge home must be a directory")
    # Never unlink a stale socket or reuse another process's directory.
    directory = Path(tempfile.mkdtemp(prefix="voice-runtime-", dir=home))
    directory.chmod(0o700)
    return str(directory / "native.sock")


def main() -> None:
    args = sys.argv[1:]
    if os.environ.get("CAN_VOICE_ENABLE") == "1":
        # An explicit wrapper home is checked against the actual Slidge option.
        home = os.environ.get("CAN_VOICE_HOME", "/var/lib/slidge")
        for i, value in enumerate(args):
            if value == "--home-dir":
                home = args[i + 1]
            elif value.startswith("--home-dir="):
                home = value.split("=", 1)[1]
        os.environ["CAN_VOICE_SOCKET"] = prepare_runtime(Path(home))
    os.execv("/venv/bin/slidge-whatsapp", ["slidge-whatsapp", *args])


if __name__ == "__main__":
    main()
