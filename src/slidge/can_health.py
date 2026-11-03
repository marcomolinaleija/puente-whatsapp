"""Private main-loop + XMPP round-trip check, independent of WhatsApp availability."""
from __future__ import annotations

import asyncio
import os
import socket
from pathlib import Path

SOCKET = "/tmp/can-bridge-health.sock"


async def respond(gateway, _reader, writer):
    result = b"xmpp_disconnected\n"
    try:
        if getattr(gateway, "_can_archive_failed", False):
            result = b"archive_failed\n"
        elif gateway.is_connected():
            result = b"xmpp_probe_failed\n"
            await gateway["xep_0199"].ping(
                gateway.infer_real_domain(), ifrom=gateway.boundjid, timeout=5
            )
            result = b"healthy\n"
    except Exception:
        pass
    finally:
        try:
            writer.write(result)
            await writer.drain()
        except (OSError, ConnectionError):
            # A timed-out probe can close while startup/SQL still holds the loop.
            # Never let that peer disconnect reach Slidge's fatal task handler.
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, ConnectionError):
                pass


def install(gateway):
    async def start():
        # /tmp belongs to this container, not a persisted data mount.
        Path(SOCKET).unlink(missing_ok=True)
        server = await asyncio.start_unix_server(
            lambda r, w: respond(gateway, r, w), SOCKET, limit=128
        )
        os.chmod(SOCKET, 0o600)
        async with server:
            await server.serve_forever()

    gateway._can_health_task = gateway.loop.create_task(start())


def check(report=False):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(8)
            sock.connect(SOCKET)
            result = sock.recv(32)
            if report:
                print(result.decode("ascii", errors="replace").strip())
            return result == b"healthy\n"
    except OSError:
        if report:
            print("local_probe_unavailable")
        return False


if __name__ == "__main__":
    raise SystemExit(0 if check(report=True) else 1)
