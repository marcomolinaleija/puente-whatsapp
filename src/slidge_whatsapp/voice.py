"""Experimental live voice gateway. Installed into slidge_whatsapp, disabled by default.

XMPP authorizes one full resource; a short-lived, single-use token opens WSS.
Native WhatsMeow handles stay private behind a Unix socket, never in replies.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import logging
import os
import secrets
import ssl
import stat
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

from aiohttp import ClientSession, ClientTimeout, UnixConnector, WSMsgType, web

NAMESPACE = "urn:can:whatsapp:voice:1"
PROTOCOL = "can-voice-1"
TOKEN_TTL = 30
MAX_TOKENS = 1024
log = logging.getLogger(__name__)


class FlowMetrics:
    """Opt-in counts at each PCM boundary; never receives samples or identities."""

    def __init__(self, *, clock=time.monotonic, report=None):
        self.clock = clock
        self.report = report or self._log
        self.active = False

    @staticmethod
    def _log(value):
        log.info("CAN voice flow %s", json.dumps(value, separators=(",", ":")))

    def state(self, state):
        if state == "active" and not self.active:
            self.active = True
            self.started = self.last_report = self.clock()
            self.tag = secrets.token_hex(4)
            self.counts = {key: 0 for key in (
                "client_received_bytes", "native_forwarded_bytes", "native_received_bytes",
                "client_forwarded_bytes", "client_packets", "native_packets",
                "client_gap_max_ms", "native_gap_max_ms", "native_send_max_ms",
                "client_send_max_ms", "loop_delay_max_ms",
                "owner_loop_delay_max_ms",
                "capture_queue_dropped_packets", "capture_revoked_packets",
                "capture_queue_packets", "capture_delay_max_ms",
            )}
            self.last = {}
        elif state != "active" and self.active:
            self.publish(final=True)
            self.active = False

    def received(self, side, size):
        if not self.active:
            return
        now = self.clock()
        self.counts[side + "_received_bytes"] += size
        self.counts[side + "_packets"] += 1
        if side in self.last:
            key = side + "_gap_max_ms"
            self.counts[key] = max(self.counts[key], round((now - self.last[side]) * 1000))
        self.last[side] = now

    def forwarded(self, side, size, elapsed):
        if self.active:
            self.counts[side + "_forwarded_bytes"] += size
            key = side + "_send_max_ms"
            self.counts[key] = max(self.counts[key], round(elapsed * 1000))

    def tick(self, delay):
        if self.active:
            self.counts["loop_delay_max_ms"] = max(
                self.counts["loop_delay_max_ms"], round(max(0, delay) * 1000),
            )
            if self.clock() - self.last_report >= 10:
                self.publish()

    def owner_tick(self, delay):
        if self.active:
            self.counts["owner_loop_delay_max_ms"] = max(
                self.counts["owner_loop_delay_max_ms"], round(max(0, delay)*1000))

    def publish(self, *, final=False):
        if not self.active:
            return
        now = self.clock()
        self.last_report = now
        self.report({
            "tag": self.tag, "final": final, "elapsed_ms": round((now-self.started)*1000),
            **self.counts,
            **{side + "_idle_ms": round((now-self.last[side])*1000)
               if side in self.last else None for side in ("client", "native")},
        })


class CapturePacer:
    """Absorb ordered WSS bursts before the native engine's three-block queue."""

    def __init__(self, send, *, allowed=lambda: True, metrics=None,
                 clock=time.perf_counter, sleep=asyncio.sleep):
        self.send, self.allowed, self.metrics = send, allowed, metrics
        self.clock, self.sleep = clock, sleep
        self.queue = asyncio.Queue(20)  # At most 400 ms queued, plus the held block.
        self.active = False
        self.call_id = ""
        self.epoch = 0
        self.pending = False

    def _count(self, key, value, *, maximum=False):
        if self.metrics and self.metrics.active:
            counts = self.metrics.counts
            counts[key] = max(counts[key], value) if maximum else counts[key] + value

    def _gauge(self):
        if self.metrics and self.metrics.active:
            self.metrics.counts["capture_queue_packets"] = self.queue.qsize() + self.pending

    def state(self, state, call_id=""):
        active = state == "active"
        if active == self.active and (not active or call_id == self.call_id):
            return
        revoked = self.queue.qsize() + self.pending
        self._count("capture_revoked_packets", revoked)
        while not self.queue.empty():
            self.queue.get_nowait()
        self.pending = False
        self.epoch += 1
        self.active, self.call_id = active, call_id if active else ""
        self._gauge()

    def push(self, data):
        if not self.active:
            return
        if self.queue.full():
            self.queue.get_nowait()
            self._count("capture_queue_dropped_packets", 1)
        self.queue.put_nowait((self.epoch, self.clock(), data))
        self._gauge()

    async def run(self):
        epoch, deadline = -1, 0.
        while True:
            generation, received, data = await self.queue.get()
            self.pending = True
            self._gauge()
            if generation != epoch:
                epoch, deadline = generation, self.clock() + .06
            while deadline - self.clock() > 1e-9:
                await self.sleep(max(.001, deadline - self.clock()))
            # Hangup, logout and another call revoke even the block held in this wait.
            if not self.allowed():
                self.state("ended")
                return
            if self.active and generation == self.epoch:
                started = self.clock()
                self._count("capture_delay_max_ms", round((self.clock()-received)*1000),
                            maximum=True)
                await self.send(data)
                # Recover a delayed queue gradually, never by dumping a burst into
                # the native three-block queue. Normally keep the exact 20 ms rate.
                interval = .018 if self.queue.qsize() > 3 else .02
                # Account for time spent writing: a slow write must not incur an
                # additional full block of sleep after it has already finished.
                deadline = (started + interval if started - deadline >= .02
                            else deadline + interval)
            self.pending = False
            self._gauge()


def secure_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "wss" or not parsed.hostname or parsed.username or parsed.password
        or parsed.query or parsed.fragment or parsed.path != "/voice/v1"
        or len(value) > 2048
    ):
        raise ValueError("A wss://host[:port]/voice/v1 URL is required")
    _ = parsed.port
    return value


@dataclass(frozen=True, slots=True)
class Grant:
    session: object
    resource: str
    device: str
    expires: float
    revoked: threading.Event = field(default_factory=threading.Event, compare=False)
    connected: threading.Event = field(default_factory=threading.Event, compare=False)


def device_id(session) -> str:
    return str(session.user.legacy_module_data.get("device_id", ""))


class Tokens:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.grants: dict[bytes, Grant] = {}
        self.leases: dict[int, Grant] = {}

    def issue(self, session, resource: str) -> str:
        now = self.clock()
        self.grants = {
            key: grant for key, grant in self.grants.items()
            if grant.expires > now
            and not (grant.session is session and grant.resource == resource)
        }
        if len(self.grants) + len(self.leases) >= MAX_TOKENS:
            raise ValueError("Voice authorization capacity reached")
        token = secrets.token_urlsafe(32)
        self.grants[hashlib.sha256(token.encode()).digest()] = Grant(
            session, resource, device_id(session), now + TOKEN_TTL,
        )
        return token

    def consume(self, token: str) -> Grant | None:
        if not token or len(token) > 128:
            return None
        grant = self.grants.pop(hashlib.sha256(token.encode()).digest(), None)
        if (
            grant is None or grant.expires <= self.clock()
            or not grant.session.logged or device_id(grant.session) != grant.device
            or not grant.device
        ):
            return None
        self.leases[id(grant)] = grant
        return grant

    def revoke(self, session) -> None:
        for grant in (*self.grants.values(), *self.leases.values()):
            if grant.session is session:
                grant.revoked.set()
        self.grants = {key: g for key, g in self.grants.items() if g.session is not session}

    def release(self, grant):
        grant.revoked.set()
        self.leases.pop(id(grant), None)

    def clear(self):
        for grant in (*self.grants.values(), *self.leases.values()):
            grant.revoked.set()
        self.grants.clear()
        self.leases.clear()


class MediaLoop:
    """One loop owns aiohttp resources; only coroutines/results cross threads."""

    def __init__(self):
        self.ready = concurrent.futures.Future()
        self.thread = threading.Thread(target=self._run, name="can-voice-media", daemon=True)
        self.thread.start()

    def _run(self):
        try:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
        except BaseException as exc:
            self.ready.set_exception(exc)
            return
        self.ready.set_result(self.loop)
        try:
            self.loop.run_forever()
        finally:
            pending = asyncio.all_tasks(self.loop)
            for task in pending:
                task.cancel()
            self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
            self.loop.run_until_complete(self.loop.shutdown_default_executor())
            self.loop.close()

    async def call(self, coroutine):
        try:
            loop = await asyncio.shield(asyncio.wrap_future(self.ready))
        except BaseException:
            coroutine.close()
            raise
        try:
            pending = asyncio.run_coroutine_threadsafe(coroutine, loop)
        except BaseException:
            coroutine.close()
            raise
        return await asyncio.wrap_future(pending)

    async def stop(self):
        try:
            loop = await asyncio.shield(asyncio.wrap_future(self.ready))
            loop.call_soon_threadsafe(loop.stop)
        finally:
            await asyncio.to_thread(self.thread.join, 5)
        if self.thread.is_alive():
            raise RuntimeError("Voice media worker did not stop")


class OwnerProbe:
    """Opt-in bounded code locations only: never frames' locals, args or source."""

    def __init__(self, owner_thread):
        self.owner_thread = owner_thread
        self.last_pulse = time.perf_counter()
        self.lock = threading.Lock()
        self.active = threading.Event()
        self.stopped = threading.Event()
        self.locations = {}
        self.thread = threading.Thread(target=self.run, name="can-voice-probe", daemon=True)
        self.thread.start()

    def pulse(self):
        with self.lock:
            self.last_pulse = time.perf_counter()

    def run(self):
        while not self.stopped.wait(.02):
            if not self.active.is_set():
                continue
            with self.lock:
                stale = time.perf_counter()-self.last_pulse > .08
            if not stale:
                continue
            frame = sys._current_frames().get(self.owner_thread)
            location = "unavailable"
            try:
                for _ in range(12):
                    if frame is None:
                        break
                    module = frame.f_globals.get("__name__", "")
                    if module.startswith(("slidge.", "slidge_whatsapp.", "asyncio.",
                                          "aiohttp.", "selectors")):
                        function = frame.f_code.co_name
                        if len(function) <= 64 and function.replace("_", "").isalnum():
                            location = f"{module[:100]}:{function}:{frame.f_lineno}"
                            break
                    frame = frame.f_back
            finally:
                del frame
            with self.lock:
                if location in self.locations or len(self.locations) < 16:
                    self.locations[location] = self.locations.get(location, 0)+1

    def snapshot(self):
        with self.lock:
            return [{"location": key, "samples": value}
                    for key, value in sorted(self.locations.items())]

    def close(self):
        self.active.clear()
        self.stopped.set()


class VoiceGateway:
    def __init__(self, gateway, *, url: str, socket: Path):
        self.gateway = gateway
        self.url = secure_url(url)
        self.socket = socket
        self.tokens = Tokens()
        self.connections: dict[str, tuple[Grant, web.WebSocketResponse]] = {}
        self.runner: web.AppRunner | None = None
        self.metrics_enabled = os.environ.get("CAN_VOICE_METRICS") == "1"
        self.owner_loop = None
        self.media_loop = None
        self.validation_task = None
        self.serving = threading.Event()
        self.owner_thread = None
        self.lifecycle = asyncio.Lock()
        self.generation = 0

    async def on_owner(self, coroutine):
        if self.owner_loop is None or asyncio.get_running_loop() is self.owner_loop:
            return await coroutine
        return await asyncio.wait_for(asyncio.wrap_future(
            asyncio.run_coroutine_threadsafe(coroutine, self.owner_loop)), 5)

    async def consume(self, token):
        return self.tokens.consume(token)

    def allowed(self, grant):
        if grant.revoked.is_set():
            return False
        # Isolated media never reads mutable Slidge objects from its worker.
        return self.media_loop is not None or (
            grant.session.logged and device_id(grant.session) == grant.device)

    async def validate_leases(self):
        while True:
            revoked = False
            for grant in tuple(self.tokens.leases.values()):
                if not grant.connected.is_set() and self.tokens.clock() >= grant.expires:
                    self.tokens.release(grant)
                elif not grant.session.logged or device_id(grant.session) != grant.device:
                    grant.revoked.set()
                    revoked = True
            if revoked and self.media_loop is not None:
                await self.media_loop.call(self.close_revoked())
            await asyncio.sleep(.05)

    async def authorize(self, iq) -> None:
        from slixmpp.exceptions import XMPPError

        session = self.gateway.get_session_from_jid(iq["from"])
        if session is None or str(session.user_jid.bare) != str(iq["from"].bare):
            raise XMPPError("forbidden")
        if (
            iq["type"] != "get" or not iq["from"].resource
            or str(iq["to"]) != str(self.gateway.boundjid.bare)
            or iq.xml.find(f"{{{NAMESPACE}}}open") is None
        ):
            raise XMPPError("bad-request")
        if (not session.logged or not device_id(session) or self.runner is None
                or (self.media_loop is not None and not self.serving.is_set())):
            raise XMPPError("service-unavailable")
        try:
            token = self.tokens.issue(session, str(iq["from"]))
        except ValueError:
            raise XMPPError("resource-constraint") from None
        reply = iq.reply()
        reply.append(ET.Element(f"{{{NAMESPACE}}}open", {
            "url": self.url, "token": token, "expires-in": str(TOKEN_TTL),
            "protocol": PROTOCOL, "format": "pcm16le", "rate": "16000", "channels": "1",
        }))
        reply.send()

    async def translate(self, grant: Grant, text: str) -> str:
        command = json.loads(text)
        if not isinstance(command, dict) or set(command) - {"id", "action", "call_id", "peer"}:
            raise ValueError("Invalid command")
        if not all(isinstance(value, str) for value in command.values()):
            raise ValueError("Invalid command fields")
        if command.get("action") == "dial":
            if self.owner_loop is not None and asyncio.get_running_loop() is not self.owner_loop:
                return await self.on_owner(self.translate(grant, text))
            from slixmpp import JID

            peer = JID(command.get("peer", ""))
            if peer.resource or peer.domain != self.gateway.boundjid.domain:
                raise ValueError("Invalid contact")
            contact = grant.session.contacts.by_jid_only_if_exists(peer)
            if contact is None:
                raise ValueError("Unknown contact")
            outbound = getattr(grant.session.contacts, "_outbound_legacy_id", lambda value: value)
            command["peer"] = outbound(str(contact.legacy_id))
        return json.dumps(command, separators=(",", ":"))

    async def route_state(self, grant: Grant, text: str, cache: dict | None = None) -> str:
        if self.owner_loop is not None and asyncio.get_running_loop() is not self.owner_loop:
            # Cache only owner-resolved strings; no Slidge/cache objects cross loops.
            state = json.loads(text)
            if not isinstance(state, dict):
                raise ValueError("Invalid native voice event")
            if state.get("type") != "state":
                return text
            pn = state.pop("peer_pn", "")
            state.pop("chat_jid", None)
            if not pn:
                return json.dumps(state, separators=(",", ":"))
            if isinstance(pn, str) and cache is not None and pn in cache:
                if cache[pn]:
                    state["chat_jid"] = cache[pn]
                return json.dumps(state, separators=(",", ":"))
            routed = await self.on_owner(self.route_state(grant, text))
            if isinstance(pn, str) and cache is not None:
                cache.clear()
                cache[pn] = json.loads(routed).get("chat_jid", "")
            return routed
        from slixmpp import JID

        state = json.loads(text)
        if not isinstance(state, dict):
            raise ValueError("Invalid native voice event")
        if state.get("type") != "state":
            return text
        state.pop("chat_jid", None)
        pn = state.pop("peer_pn", "")
        if isinstance(pn, str) and pn.endswith("@s.whatsapp.net") and len(pn) <= 256:
            canonical = getattr(grant.session.contacts, "_canonical_legacy_id", lambda value: value)
            legacy = canonical(pn)
            if cache is not None and legacy in cache:
                state["chat_jid"] = cache[legacy]
                return json.dumps(state, separators=(",", ":"))
            username = await grant.session.contacts.legacy_id_to_jid_username(legacy)
            jid = JID(username + "@" + self.gateway.boundjid.domain)
            contacts = grant.session.contacts

            def known_jid():
                contact = contacts.by_jid_only_if_exists(jid)
                return str(contact.jid.bare) if contact is not None else ""

            found = await asyncio.to_thread(known_jid)
            if found:
                state["chat_jid"] = found
                if cache is not None:
                    cache.clear()
                    cache[legacy] = found
        return json.dumps(state, separators=(",", ":"))

    async def websocket(self, request: web.Request) -> web.StreamResponse:
        # Browser scripts cannot attach an Authorization header to WebSocket.
        # Refuse cookies, query tokens and cross-origin upgrades explicitly.
        if request.headers.get("Origin") or request.query_string:
            raise web.HTTPForbidden()
        if PROTOCOL not in request.headers.get("Sec-WebSocket-Protocol", "").split(", "):
            raise web.HTTPBadRequest()
        auth = request.headers.get("Authorization", "")
        grant = await self.on_owner(self.consume(auth[7:] if auth.startswith("Bearer ") else ""))
        if grant is None:
            raise web.HTTPForbidden()
        if grant.device in self.connections:
            self.release_grant(grant)
            raise web.HTTPConflict()
        ws = web.WebSocketResponse(protocols=(PROTOCOL,), max_msg_size=4096, heartbeat=15)
        metrics = FlowMetrics() if self.metrics_enabled else None
        probe = (OwnerProbe(self.owner_thread)
                 if metrics and self.owner_thread and os.environ.get("CAN_VOICE_PROFILE") == "1"
                 else None)
        owner_monitor = None
        self.connections[grant.device] = (grant, ws)
        grant.connected.set()
        try:
            async with self.native_http() as http:
                try:
                    native = await self.connect_native(http, grant)
                except Exception:
                    raise web.HTTPServiceUnavailable() from None
                async with native:
                    await ws.prepare(request)

                    async def send_capture(data):
                        started = time.monotonic()
                        await native.send_bytes(data)
                        if metrics:
                            metrics.forwarded("native", len(data), time.monotonic() - started)

                    capture = CapturePacer(
                        send_capture, metrics=metrics,
                        allowed=lambda: self.allowed(grant),
                    )

                    async def upstream():
                        async for message in ws:
                            if not self.allowed(grant):
                                return
                            if message.type == WSMsgType.BINARY:
                                if len(message.data) != 640:
                                    return
                                if metrics:
                                    metrics.received("client", len(message.data))
                                capture.push(message.data)
                            elif message.type == WSMsgType.TEXT:
                                try:
                                    data = await self.translate(grant, message.data)
                                except (ValueError, TypeError, TimeoutError):
                                    await ws.close(code=1008, message=b"Invalid voice command")
                                    return
                                if json.loads(data).get("action") in {"hangup", "reject"}:
                                    capture.state("ended")
                                await native.send_str(data)
                            else:
                                return

                    async def downstream():
                        routes = {}
                        async for message in native:
                            if not self.allowed(grant):
                                return
                            if message.type == WSMsgType.BINARY:
                                if not 0 < len(message.data) <= 3200 or len(message.data) % 2:
                                    return
                                if metrics:
                                    metrics.received("native", len(message.data))
                                started = time.monotonic()
                                await ws.send_bytes(message.data)
                                if metrics:
                                    metrics.forwarded("client", len(message.data),
                                                      time.monotonic() - started)
                            elif message.type == WSMsgType.TEXT:
                                try:
                                    # Stop queued audio before any owner-loop routing can wait.
                                    event = json.loads(message.data)
                                    if isinstance(event, dict) and event.get("type") == "state":
                                        capture.state(event.get("state"), event.get("call_id", ""))
                                    data = await self.route_state(grant, message.data, routes)
                                    event = json.loads(data)
                                    if event.get("type") == "state":
                                        state = event.get("state")
                                        if probe:
                                            if state == "active":
                                                probe.active.set()
                                            else:
                                                probe.active.clear()
                                        if metrics and state == "active":
                                            metrics.state(state)
                                        capture.state(state, event.get("call_id", ""))
                                        if metrics and state != "active":
                                            metrics.state(state)
                                except (ValueError, TypeError, TimeoutError):
                                    return
                                await ws.send_str(data)
                            else:
                                return

                    async def monitor():
                        while True:
                            before = time.monotonic()
                            await asyncio.sleep(1)
                            if probe and metrics.active:
                                metrics.counts["owner_hotspots"] = probe.snapshot()
                            metrics.tick(time.monotonic() - before - 1)

                    async def monitor_owner():
                        media = asyncio.get_running_loop()

                        async def pulse():
                            while True:
                                if probe:
                                    probe.pulse()
                                before = time.monotonic()
                                await asyncio.sleep(.02)
                                try:
                                    media.call_soon_threadsafe(
                                        metrics.owner_tick, time.monotonic()-before-.02)
                                except RuntimeError:
                                    return

                        return asyncio.run_coroutine_threadsafe(pulse(), self.owner_loop)

                    tasks = [asyncio.create_task(upstream()), asyncio.create_task(downstream()),
                             asyncio.create_task(capture.run())]
                    if metrics:
                        tasks.append(asyncio.create_task(monitor()))
                        if self.owner_loop is not None:
                            owner_monitor = await monitor_owner()
                    try:
                        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                        capture.state("ended")
                        if metrics:
                            if probe:
                                metrics.counts["owner_hotspots"] = probe.snapshot()
                            metrics.state("ended")
        finally:
            if probe:
                probe.close()
                await asyncio.to_thread(probe.thread.join, 1)
            if owner_monitor:
                owner_monitor.cancel()
            self.release_grant(grant)
            self.connections.pop(grant.device, None)
            if ws.prepared:
                await ws.close()
        return ws

    def release_grant(self, grant):
        grant.revoked.set()
        if self.owner_loop is not None and asyncio.get_running_loop() is not self.owner_loop:
            if not self.owner_loop.is_closed():
                self.owner_loop.call_soon_threadsafe(self.tokens.release, grant)
        else:
            self.tokens.release(grant)

    async def connect_native(self, http: ClientSession, grant: Grant):
        return await http.ws_connect(
            "http://localhost/session", protocols=(PROTOCOL,),
            headers={"X-CAN-Device": grant.device, "X-CAN-Resource": grant.resource},
            max_msg_size=4096, heartbeat=15,
        )

    def native_http(self) -> ClientSession:
        return ClientSession(
            connector=UnixConnector(path=str(self.socket)),
            timeout=ClientTimeout(total=None, sock_connect=3), trust_env=False,
        )

    async def revoke(self, session) -> None:
        self.tokens.revoke(session)
        if self.media_loop is not None and asyncio.get_running_loop() is self.owner_loop:
            await self.media_loop.call(self.close_revoked())
        else:
            await self.close_revoked()

    async def close_revoked(self):
        for grant, ws in tuple(self.connections.values()):
            if grant.revoked.is_set():
                await ws.close(code=1001, message=b"Session ended")

    async def start(self, host: str, port: int, tls: ssl.SSLContext, *, isolated=False) -> None:
        if isolated:
            generation = self.generation
            async with self.lifecycle:
                if generation != self.generation:
                    raise ValueError("Voice startup revoked")
                if self.media_loop is not None:
                    return
                self.owner_loop = asyncio.get_running_loop()
                self.owner_thread = threading.get_ident()
                worker = None
                try:
                    self.media_loop = worker = MediaLoop()
                    await worker.call(self.start(host, port, tls))
                    if generation != self.generation:
                        raise ValueError("Voice startup revoked")
                except BaseException:
                    try:
                        if worker:
                            try:
                                await worker.call(self.close())
                            finally:
                                await worker.stop()
                    finally:
                        self.media_loop = None
                        self.owner_loop = None
                        self.owner_thread = None
                    raise
                self.validation_task = asyncio.create_task(self.validate_leases())
            return
        app = web.Application(client_max_size=4096)
        app.router.add_get("/voice/v1", self.websocket)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        try:
            await web.TCPSite(self.runner, host, port, ssl_context=tls).start()
            self.serving.set()
        except BaseException:
            await self.runner.cleanup()
            self.runner = None
            raise

    async def close(self, _event=None) -> None:
        self.serving.clear()
        if self.media_loop is not None and asyncio.get_running_loop() is self.owner_loop:
            self.generation += 1
            self.tokens.clear()
            async with self.lifecycle:
                if self.media_loop is None:
                    return
                if self.validation_task:
                    self.validation_task.cancel()
                    await asyncio.gather(self.validation_task, return_exceptions=True)
                    self.validation_task = None
                worker = self.media_loop
                try:
                    await worker.call(self.close())
                finally:
                    await worker.stop()
                    self.media_loop = None
                    self.owner_loop = None
                    self.owner_thread = None
            return
        if self.media_loop is None:
            self.tokens.clear()
        for _, ws in tuple(self.connections.values()):
            await ws.close(code=1001)
        if self.runner:
            await self.runner.cleanup()
            self.runner = None


def configured_socket(home: Path, value: str) -> Path:
    """Use a private path inherited before gopy loads Go's environment snapshot."""
    socket = Path(value)
    if not value or not socket.is_absolute() or socket.name != "native.sock":
        raise ValueError("An inherited absolute voice socket is required")
    directory = socket.parent
    info = directory.lstat()
    if (
        directory.parent != home or directory.resolve(strict=True) != directory
        or not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700
        or info.st_uid != os.getuid()
        or socket.exists() or socket.is_symlink()
    ):
        raise ValueError("An empty private voice directory under bridge home is required")
    return socket


async def revoke(session) -> None:
    voice = getattr(session.xmpp, "_can_voice", None)
    if voice:
        await voice.revoke(session)


def install(gateway) -> None:
    if os.environ.get("CAN_VOICE_ENABLE") != "1":
        return
    from slidge import global_config
    from slixmpp.xmlstream.handler import CoroutineCallback
    from slixmpp.xmlstream.matcher import MatchXPath

    try:
        url = secure_url(os.environ["CAN_VOICE_URL"])
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.minimum_version = ssl.TLSVersion.TLSv1_2
        tls.load_cert_chain(os.environ["CAN_VOICE_CERT"], os.environ["CAN_VOICE_KEY"])
        home = Path(global_config.HOME_DIR).resolve(strict=True)
        socket = configured_socket(home, os.environ["CAN_VOICE_SOCKET"])
    except (KeyError, OSError, ValueError):
        log.warning("Experimental voice disabled: invalid configuration")
        return
    voice = gateway._can_voice = VoiceGateway(gateway, url=url, socket=socket)

    async def start(_event):
        if voice.runner is not None:
            return
        try:
            await voice.start(
                os.environ.get("CAN_VOICE_BIND", "127.0.0.1"),
                int(os.environ.get("CAN_VOICE_PORT", "8766")), tls,
                isolated=True,
            )
        except (OSError, ValueError, RuntimeError):
            log.warning("Experimental voice server unavailable; messaging remains active")
            return
        if not voice.serving.is_set():
            return
        gateway["xep_0030"].add_feature(NAMESPACE)

    gateway.add_event_handler("session_start", start)
    gateway.add_event_handler("disconnected", voice.close)
    gateway.register_handler(CoroutineCallback(
        "CAN experimental voice", MatchXPath(f"{{{gateway.default_ns}}}iq/{{{NAMESPACE}}}open"),
        voice.authorize,
    ))
