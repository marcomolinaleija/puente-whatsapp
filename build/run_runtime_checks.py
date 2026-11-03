"""Require current native reports and run every offline bridge runtime check."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> None:
    reports = {}
    required = {"TestCANWhatsAppWebBaseline", "TestCANInviteLinkIsOnlyMessageData"}
    for name in ("go.jsonl", "race.jsonl", "sticker-go.jsonl"):
        rows = [json.loads(line) for line in (ROOT / name).read_text().splitlines()]
        assert not any(row.get("Action") == "fail" for row in rows), name
        passed = {(row["Package"], row["Test"]) for row in rows
                  if row.get("Action") == "pass" and row.get("Test")}
        assert passed, name
        if name != "sticker-go.jsonl":
            root_cases = {test for package, test in passed
                          if package == "codeberg.org/slidge/slidge-whatsapp/slidge_whatsapp"}
            assert required <= root_cases, f"Missing current native regressions: {name}"
        reports[name] = {
            "passed": len(passed),
            "skipped": [{"package": row["Package"], "test": row.get("Test")}
                        for row in rows if row.get("Action") == "skip"],
        }
    scripts = sorted(ROOT.glob("smoke_*.py")) + [ROOT / "test_bridge_linking.py"]
    assert len(scripts) == 23, "Runtime inventory changed; review the release gate"
    for script in scripts:
        args = [sys.executable, "-B", str(script)]
        if script.name == "smoke_slidge_watchdog.py":
            args.append(str(ROOT / "slidge-watchdog"))
        result = subprocess.run(args, capture_output=True, text=True, timeout=180)
        if result.returncode:
            print((result.stdout + result.stderr)[-12000:])
            raise SystemExit(f"Runtime check failed: {script.name}")
        print(f"PASS {script.name}", flush=True)
    summary = {"native": reports, "runtime_checks": [p.name for p in scripts]}
    Path("/tmp/runtime-result.json").write_text(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
