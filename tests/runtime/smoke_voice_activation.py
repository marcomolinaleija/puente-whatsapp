"""Linux-only activation contract, with real private paths and no account/network."""
import asyncio
import json
import os
import ssl
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

from aiohttp import ClientSession, WSMsgType, web
from slidge_whatsapp.voice import (
    PROTOCOL,
    CapturePacer,
    FlowMetrics,
    MediaLoop,
    VoiceGateway,
    configured_socket,
)
from slixmpp import JID


def rejected(home, path):
    try:
        configured_socket(home, path)
    except (ValueError, OSError):
        return
    raise AssertionError("Unsafe or stale voice socket accepted")


with tempfile.TemporaryDirectory() as temporary:
    home = Path(temporary).resolve()
    directory = home / "voice-runtime"
    directory.mkdir(mode=0o700)
    socket = directory / "native.sock"
    inherited = os.environ.get("CAN_VOICE_SOCKET")
    assert configured_socket(home, str(socket)) == socket
    assert os.environ.get("CAN_VOICE_SOCKET") == inherited
    rejected(home, "")
    rejected(home, "relative/native.sock")
    rejected(home, str(directory / "other.sock"))
    rejected(home / "other-home", str(socket))
    directory.chmod(0o755)
    rejected(home, str(socket))
    directory.chmod(0o700)
    socket.write_text("existing")
    rejected(home, str(socket))
    socket.unlink()
    socket.symlink_to(home / "missing")
    rejected(home, str(socket))
    socket.unlink()
    link = home / "linked"
    link.symlink_to(directory, target_is_directory=True)
    rejected(home, str(link / "native.sock"))
print("PASS inherited voice socket + private directory/stale path checks; no account/network")

reports = []
metrics = FlowMetrics(clock=lambda: 0., report=reports.append)
metrics.state("active")
metrics.received("client", 640)
metrics.forwarded("native", 640, .003)
metrics.received("native", 1920)
metrics.forwarded("client", 1920, .007)
metrics.state("ended")
assert len(reports) == 1 and reports[0]["final"]
assert reports[0]["client_received_bytes"] == reports[0]["native_forwarded_bytes"] == 640
assert reports[0]["native_received_bytes"] == reports[0]["client_forwarded_bytes"] == 1920
assert reports[0]["native_send_max_ms"] == 3
print("PASS opt-in boundary counters; no accounts, identities or stored samples")


async def paced_capture():
    sent = []
    ready = asyncio.Event()

    async def send(data):
        sent.append(data)
        ready.set()

    pacer = CapturePacer(send)
    pacer.state("active", "fixture-call")
    pacer.push(bytes([7])*640)
    task = asyncio.create_task(pacer.run())
    try:
        await asyncio.wait_for(ready.wait(), 2)
        assert sent == [bytes([7])*640]
        pacer.state("ended")
        assert pacer.queue.empty()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


asyncio.run(paced_capture())
print("PASS paced capture + revocation; synthetic PCM only")


async def isolated_unix_tls():
    # Real Linux transport with synthetic data; no external route/account/devices.
    with tempfile.TemporaryDirectory() as temporary:
        home = Path(temporary).resolve()
        directory = home / "voice-runtime"
        directory.mkdir(mode=0o700)
        socket = configured_socket(home, str(directory / "native.sock"))
        cert, key = home / "cert.pem", home / "key.pem"
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-keyout", str(key), "-out", str(cert), "-subj", "/CN=localhost",
            "-addext", "subjectAltName=IP:127.0.0.1",
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        native_frames = []

        async def backend(request):
            assert request.headers["X-CAN-Device"] == "fixture-device"
            assert request.headers["X-CAN-Resource"] == "fixture@example.invalid/windows"
            ws = web.WebSocketResponse(protocols=(PROTOCOL,))
            await ws.prepare(request)
            await ws.send_json({"type": "state", "state": "active", "call_id": "fixture"})
            async for message in ws:
                if message.type == WSMsgType.BINARY:
                    native_frames.append(message.data)
                    await ws.send_bytes(message.data)
            return ws

        app = web.Application()
        app.router.add_get("/session", backend)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.UnixSite(runner, str(socket)).start()
        voice = VoiceGateway(SimpleNamespace(boundjid=JID("whatsapp.example.invalid")),
                             url="wss://localhost/voice/v1", socket=socket)
        session = SimpleNamespace(logged=True, user=SimpleNamespace(
            legacy_module_data={"device_id": "fixture-device"}))
        io = MediaLoop()
        worker = None
        try:
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.load_cert_chain(cert, key)
            await voice.start("127.0.0.1", 0, tls, isolated=True)
            worker = voice.media_loop
            port = next(iter(voice.runner.sites))._server.sockets[0].getsockname()[1]
            token = voice.tokens.issue(session, "fixture@example.invalid/windows")

            async def client():
                async with ClientSession() as http, http.ws_connect(
                    f"wss://127.0.0.1:{port}/voice/v1", protocols=(PROTOCOL,),
                    ssl=ssl.create_default_context(cafile=str(cert)),
                    headers={"Authorization": "Bearer " + token},
                ) as ws:
                    event = await ws.receive(timeout=3)
                    assert json.loads(event.data)["state"] == "active"
                    pcm = bytes([7])*640
                    await ws.send_bytes(pcm)
                    response = await ws.receive(timeout=3)
                    assert response.type == WSMsgType.BINARY and response.data == pcm

            await io.call(client())
            assert native_frames == [bytes([7])*640]
        finally:
            await voice.close()
            await io.stop()
            await runner.cleanup()
        assert worker is not None and not worker.thread.is_alive()
        assert voice.media_loop is None and not voice.tokens.leases


asyncio.run(isolated_unix_tls())
print("PASS isolated media TLS + private Unix socket + worker cleanup; synthetic PCM only")
