"""Show only failed offline Go cases; never hide a failure behind unrelated package output."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def check(path, status):
    rows = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    failures = {(r.get("Package"), r.get("Test")) for r in rows if r.get("Action") == "fail"}
    if int(status) or failures:
        output = "".join(
            r.get("Output", "") for r in rows
            if (r.get("Package"), r.get("Test")) in failures
        )
        print(output[-10000:])
        raise SystemExit(1)
    if not any(r.get("Action") == "pass" and r.get("Test") for r in rows):
        raise SystemExit("No executed Go cases; refusing false success")


if __name__ == "__main__":
    check(sys.argv[1], sys.argv[2])
