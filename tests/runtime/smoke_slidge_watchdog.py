"""Run the real shell watchdog against fake Docker, without touching the daemon."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def run(script):
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        binary = root / "bin"
        binary.mkdir()
        fake = binary / "docker"
        fake.write_text('''#!/bin/sh
case "$1" in
 inspect) case "$3" in
   *Running*) echo "${RUNNING:-true}";;
   *Health*) echo "${HEALTH:-healthy}";;
   *) echo '{}';;
 esac;;
 stats) echo '1% 100MiB 2';;
 restart) echo restart >> "$TEST_ROOT/restarts";;
 *) exit 19;;
esac
''')
        fake.chmod(0o700)
        logger = binary / "logger"
        logger.write_text('#!/bin/sh\necho "$*" >> "$TEST_ROOT/events"\n')
        logger.chmod(0o700)
        state = root / "state"
        env = dict(os.environ, PATH=str(binary) + ":" + os.environ["PATH"],
                   TEST_ROOT=str(root), STATE_DIR=str(state), LOCK_FILE=str(root / "lock"))

        def invoke(health="healthy", running="true"):
            subprocess.run(["sh", str(script)], env=dict(env, HEALTH=health, RUNNING=running),
                           check=True)

        def count():
            path = root / "restarts"
            return len(path.read_text().splitlines()) if path.exists() else 0

        for healthy in ("healthy", "none", "starting"):
            invoke(healthy)
        assert count() == 0
        invoke("unhealthy")
        assert count() == 1
        assert (state / "before-restart-state.json").is_file()
        invoke("unhealthy")
        assert count() == 1, "cooldown ignored"
        text = (state / "state").read_text().splitlines()
        now = int(next(line.split("=")[1] for line in text if line.startswith("last_restart=")))
        (state / "state").write_text(
            f"window_start={now - 1800}\nrestart_count=2\nlast_restart={now - 901}\n"
        )
        invoke("unhealthy")
        assert count() == 1, "hourly restart limit ignored"
        (state / "state").write_text(
            f"window_start={now - 4000}\nrestart_count=2\nlast_restart={now - 4000}\n"
        )
        invoke("none", "false")
        assert count() == 2, "stopped container not recovered after window"
        print(json.dumps({"watchdog_scenarios": 7, "result": "pass"}))


if __name__ == "__main__":
    run(Path(sys.argv[1]))
