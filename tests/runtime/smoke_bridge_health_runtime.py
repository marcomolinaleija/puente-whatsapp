"""Real local health socket; fake XMPP only, no WhatsApp account or external network."""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from slidge import can_health as health


class Gateway:
    boundjid = "bridge.example.invalid"
    _can_archive_failed = False

    def __init__(self, connected=True, error=None):
        self.is_connected = lambda: connected
        self.ping = AsyncMock(side_effect=error)

    def infer_real_domain(self):
        return "example.invalid"

    def __getitem__(self, name):
        assert name == "xep_0199"
        return SimpleNamespace(ping=self.ping)


async def main():
    for connected, failed, error, expected in (
        (True, False, None, b"healthy\n"),
        (False, False, None, b"xmpp_disconnected\n"),
        (True, True, None, b"archive_failed\n"),
        (True, False, TimeoutError(), b"xmpp_probe_failed\n"),
    ):
        gateway = Gateway(connected, error)
        gateway._can_archive_failed = failed
        writer = SimpleNamespace(write=Mock(), drain=AsyncMock(), close=Mock(),
                                 wait_closed=AsyncMock())
        await health.respond(gateway, None, writer)
        writer.write.assert_called_once_with(expected)
        writer.close.assert_called_once()
    # Abandoned probes must never escape into Slidge's fatal task handler.
    writer = SimpleNamespace(write=Mock(), drain=AsyncMock(side_effect=ConnectionResetError()),
                             close=Mock(), wait_closed=AsyncMock(side_effect=BrokenPipeError()))
    await health.respond(Gateway(), None, writer)
    with tempfile.TemporaryDirectory() as temp:
        socket = str(Path(temp) / "health.sock")
        with patch.object(health, "SOCKET", socket):
            assert not await asyncio.to_thread(health.check)
            gateway = Gateway()
            server = await asyncio.start_unix_server(
                lambda reader, writer: health.respond(gateway, reader, writer), socket)
            async with server:
                assert await asyncio.to_thread(health.check)
                gateway._can_archive_failed = True
                assert not await asyncio.to_thread(health.check)
    print("PASS functional health socket, failed XMPP/archive and abandoned probes")


if __name__ == "__main__":
    asyncio.run(main())
