"""Fetch hash-pinned public codec vectors only inside the isolated test stage."""
from __future__ import annotations

import hashlib
import io
import json
import tarfile
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = Path("/src/slidge_whatsapp/voicecore/internal/voip/media/mlow/testdata")


def main() -> None:
    metadata = json.loads((HERE / "voice-upstream.json").read_text())
    request = urllib.request.Request(metadata["source_url"], headers={"User-Agent": "CAN-build"})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read(32 * 1024 * 1024)
    assert hashlib.sha256(data).hexdigest() == metadata["source_sha256"]
    expected = {}
    for line in (HERE / "voice-fixtures.sha256").read_text().splitlines():
        digest, name = line.split("  ", 1)
        marker = "src/slidge_whatsapp/voicecore/internal/voip/media/mlow/testdata/"
        if name.startswith(marker):
            expected[name[len(marker):]] = digest
    assert len(expected) == 19, "Review the public test-vector inventory"
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        prefix = archive.getmembers()[0].name.split("/", 1)[0]
        for name, digest in expected.items():
            target = (SOURCE / name).resolve()
            assert target.is_relative_to(SOURCE.resolve())
            member = archive.getmember(prefix + "/internal/voip/media/mlow/testdata/" + name)
            assert member.isfile() and not member.issym()
            body = archive.extractfile(member).read()
            assert hashlib.sha256(body).hexdigest() == digest, name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
    print("Verified 19 public codec vectors for isolated tests only")


if __name__ == "__main__":
    main()
