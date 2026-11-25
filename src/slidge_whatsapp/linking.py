"""Account-owned WhatsApp linking transactions; no credentials in the XMPP contract."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import re
import secrets
import time
from xml.etree import ElementTree as ET

NAMESPACE = "urn:can:whatsapp:linking:1"
HINTS = "urn:xmpp:hints"
LOGIN_TIMEOUT = 190
TERMINAL = {"canceled", "expired", "connection_error", "logged_out", "needs_pairing"}
# Phases in which the native client still owns a pairing handshake.
LIVE = {"connecting", "needs_qr", "paired", "needs_pair_code"}
# E.164: at most 15 ASCII digits; WhatsMeow itself rejects fewer than 7.
PHONE = re.compile(r"\+?[0-9]{7,15}")
ROSTER_RATE_LIMIT_MIN = 300
ROSTER_RATE_LIMIT_MAX = 1800


class RosterRateLimited(RuntimeError):
    """A group-list request is cooling down; no new request was sent."""

    def __init__(self, retry_after):
        super().__init__("WhatsApp group list rate limited")
        self.retry_after = retry_after


def roster_retry_after(error, previous=0):
    if isinstance(error, RosterRateLimited):
        return error.retry_after
    # Match the native query rejection, not every error mentioning 429.
    text = str(error).lower()
    if "status 429: rate-overlimit" in text:
        return min(max(previous * 2, ROSTER_RATE_LIMIT_MIN), ROSTER_RATE_LIMIT_MAX)
    return 0


def readiness_succeeded(future):
    return (future is not None and future.done() and not future.cancelled()
            and future.exception() is None and bool(future.result()))


class LinkingError(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class LinkingSession:
    """Lives on the gateway loop, alongside exactly one native client per account."""

    def __init__(self, session, *, clock=time.monotonic):
        self.session = session
        self.clock = clock
        self.attempt = ""
        self.revision = 0
        self.phase = "needs_pairing"
        self.changed_at = self.clock()
        self.reason = ""
        self.authenticated = False
        self.qr = b""
        self.qr_deadline = 0.0
        self.qr_revision = 0
        self.ready = asyncio.Event()
        self.task = None
        self.bootstrap = None
        self.lock = asyncio.Lock()
        self.requests: dict[str, str] = {}
        self.mode = "qr"
        self.sync_error = False
        self.sync_task = None
        self.sync_retry_after = 0
        self.phone_code = ""
        self.phone_fingerprint = ""
        self.phone_task = None
        self.paired_after_cancel = False
        self.timeout_handle = None
        self.deadline = 0.0
        self.notify_pending = False

    def notify(self):
        """Wake the owner's client; the hint carries no QR, code or credentials."""
        if self.notify_pending:
            return
        xmpp = getattr(self.session, "xmpp", None)
        user = getattr(self.session, "user_jid", None)
        if xmpp is None or user is None or not hasattr(xmpp, "make_message"):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self.notify_pending = True

        def send():
            self.notify_pending = False
            try:
                # Components must stamp 'from'. Prosody discards bare-JID headlines,
                # so this is a bodyless "normal" message that XEP-0334 hints keep out
                # of MAM and offline storage.
                msg = xmpp.make_message(mto=user.bare, mfrom=xmpp.boundjid.bare,
                                        mtype="normal")
                msg.xml.append(ET.Element(f"{{{NAMESPACE}}}state", {
                    "version": "1", "attempt": self.attempt, "revision": str(self.revision),
                }))
                for hint in ("no-store", "no-permanent-store"):
                    msg.xml.append(ET.Element(f"{{{HINTS}}}{hint}"))
                msg.send()
            except Exception:
                logging.getLogger(__name__).debug("linking hint not sent", exc_info=True)

        loop.call_soon(send)

    def change(self, phase, reason=""):
        if phase != "connected" and self.sync_task is not None and not self.sync_task.done():
            self.sync_task.cancel()
        self.phase, self.reason = phase, reason
        self.changed_at = self.clock()
        self.revision += 1
        if phase not in {"needs_qr", "needs_pair_code"}:
            self.qr = b""
            self.qr_deadline = 0.0
            self.ready.clear()
            self.phone_code = self.phone_fingerprint = ""
        self._arm_timeout()
        self.notify()

    def _arm_timeout(self):
        # The deadline is owned by a timer, so a status read never mutates state.
        if self.timeout_handle is not None:
            self.timeout_handle.cancel()
            self.timeout_handle = None
        if self.authenticated or self.phase not in {"connecting", "paired"}:
            return
        self.deadline = self.changed_at + LOGIN_TIMEOUT
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self.timeout_handle = loop.call_later(LOGIN_TIMEOUT, self.expire_if_due)

    def expire_if_due(self):
        self.timeout_handle = None
        if self.authenticated or self.phase not in {"connecting", "paired"}:
            return
        remaining = self.deadline - self.clock()
        if remaining > 0:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            self.timeout_handle = loop.call_later(remaining, self.expire_if_due)
            return
        self.change("connection_error", "connection_timeout")

    def fail(self, reason):
        """Record a failure without hiding a more specific final reason."""
        if self.phase in TERMINAL and self.reason:
            return
        self.change("connection_error", reason)

    def reserve(self):
        self.attempt = secrets.token_hex(16)
        self.authenticated = False
        self.mode = "qr"
        self.sync_error = False
        self.paired_after_cancel = False
        self.sync_retry_after = 0
        self.change("connecting")

    async def _retire(self):
        """Disconnect (never Logout) and stop every task of the current attempt."""
        try:
            await asyncio.wait_for(asyncio.to_thread(self.session.whatsapp.Disconnect), 10)
        except Exception as exc:
            self.change("connection_error", "cancel_failed")
            raise LinkingError("cancel_failed") from exc
        current = asyncio.current_task()
        tasks = [t for t in (self.task, self.bootstrap, self.sync_task, self.phone_task)
                 if t and t is not current and not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.session.logged = False
        self.session.is_logging_in = False

    def request_sync(self):
        """User-triggered retry once automatic retries are exhausted."""
        if not self.authenticated:
            raise LinkingError("not_connected")
        if not self.sync_error:
            return
        self.retry_sync(delays=(0,))

    def retry_sync(self, error=None, delays=(30, 60, 120)):
        if error is not None:
            self.sync_retry_after = max(self.sync_retry_after, roster_retry_after(error))
        if (self.sync_task is not None and not self.sync_task.done()
                and not self.sync_task.cancelling()):
            return
        attempt = self.attempt

        async def retry():
            failures = 0
            while failures < len(delays):
                # Only a known rate limit has slow ongoing recovery. Other
                # errors retain the bounded attempt budget.
                delay = self.sync_retry_after or delays[failures]
                await asyncio.sleep(delay)
                if not self.authenticated or attempt != self.attempt:
                    return
                try:
                    contacts = self.session.contacts
                    if not contacts.ready.done():
                        with self.session.xmpp.store.session() as orm:
                            await contacts._fill(orm)
                        if not self.authenticated or attempt != self.attempt:
                            return
                        if not contacts.ready.done():
                            contacts.ready.set_result(True)
                    if self.session.xmpp.GROUPS:
                        groups = self.session.bookmarks
                        if not groups.ready.done():
                            await groups.fill()
                            if not self.authenticated or attempt != self.attempt:
                                return
                            if not groups.ready.done():
                                groups.ready.set_result(True)
                    if not self.authenticated or attempt != self.attempt:
                        return
                    required = [contacts.ready]
                    if self.session.xmpp.GROUPS:
                        required.append(groups.ready)
                    if not all(readiness_succeeded(f) for f in required):
                        raise RuntimeError("Roster readiness was cancelled or failed")
                    self.session.send_gateway_status("Logged in", show="chat")
                    self.sync_error = False
                    self.sync_retry_after = 0
                    self.revision += 1
                    self.notify()
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.sync_retry_after = roster_retry_after(exc, self.sync_retry_after)
                    if not self.sync_retry_after:
                        failures += 1
                    logging.getLogger(__name__).warning(
                        "WhatsApp roster sync retry failed; cooldown=%ss",
                        self.sync_retry_after, exc_info=not self.sync_retry_after,
                    )
            # Exhaustion remains visible as sync=error; never acknowledge completion.

        self.sync_task = asyncio.create_task(retry())

    def snapshot(self):
        remaining = max(0.0, self.qr_deadline - self.clock())
        sync = "pending"
        if self.authenticated:
            contacts = getattr(getattr(self.session, "contacts", None), "ready", None)
            groups = getattr(getattr(self.session, "bookmarks", None), "ready", None)
            if self.sync_error:
                sync = "error"
            elif readiness_succeeded(contacts) and readiness_succeeded(groups):
                sync = "ready"
            else:
                sync = "syncing"
        return {
            "version": "1", "attempt": self.attempt, "revision": str(self.revision),
            "phase": self.phase, "reason": self.reason,
            "authenticated": "1" if self.authenticated else "0",
            "qr_revision": str(self.qr_revision), "qr_ttl": f"{remaining:.3f}",
            "sync": sync,
        }

    async def login(self, original):
        if self.task is not None and not self.task.done():
            return await asyncio.shield(self.task)
        if self.phase != "connecting":
            self.reserve()
        attempt = self.attempt

        async def run():
            try:
                return await asyncio.wait_for(original(self.session), LOGIN_TIMEOUT)
            except asyncio.CancelledError:
                raise
            except Exception:
                if attempt == self.attempt and not self.authenticated:
                    self.fail("login_failed")
                    await asyncio.to_thread(self.session.whatsapp.Disconnect)
                raise

        self.task = asyncio.create_task(run())
        return await asyncio.shield(self.task)

    async def start(self, request_id):
        async with self.lock:
            if not request_id or len(request_id) > 128:
                raise LinkingError("invalid_request")
            if request_id in self.requests:
                if self.requests[request_id] != self.attempt:
                    raise LinkingError("stale_attempt")
                if self.authenticated or self.phase in LIVE:
                    return  # A repeated IQ adopts the attempt it created.
                # That attempt ended: its token cannot silently reopen it.
                raise LinkingError("stale_request")
            if self.authenticated:
                return
            live = bool(self.attempt and self.phase in LIVE)
            if self.attempt and not live and self.phase not in {"canceled", "needs_pairing"}:
                # A finished attempt is retired here, with the same non-destructive
                # Disconnect as cancel, so a retry never needs a separate round trip.
                await self._retire()
            active = live or any(t is not None and not t.done()
                                 for t in (self.task, self.bootstrap))
            if not active:
                self.reserve()

                async def bootstrap():
                    try:
                        await self.session.xmpp.login_wrap(self.session)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        if not self.authenticated:
                            self.fail("login_failed")
                    finally:
                        self.session.is_logging_in = False

                self.bootstrap = asyncio.create_task(bootstrap())
            self.requests[request_id] = self.attempt
            if len(self.requests) > 32:
                self.requests.pop(next(iter(self.requests)))

    async def cancel(self, attempt):
        async with self.lock:
            if not attempt or attempt != self.attempt:
                raise LinkingError("stale_attempt")
            if self.authenticated:
                raise LinkingError("already_connected")
            if self.phase == "canceled":
                return
            self.change("canceling")
            # Disconnect, never Logout: paired device keys and history survive.
            await self._retire()
            # A scan that won the race leaves a linked device; say so instead of
            # pretending nothing happened. The next start logs in with it.
            self.change("canceled", "paired_after_cancel" if self.paired_after_cancel else "")
            self.ready.set()  # Wake pending phone IQs so they reject this canceled attempt now.

    async def phone(self, attempt, phone):
        if not isinstance(phone, str) or not PHONE.fullmatch(phone):
            raise LinkingError("invalid_phone")
        if attempt != self.attempt or not attempt:
            raise LinkingError("stale_attempt")
        if self.authenticated:
            raise LinkingError("already_connected")
        if self.phase == "paired":
            raise LinkingError("already_paired")
        if self.phase not in LIVE:
            # Never wait for readiness that a finished attempt cannot produce.
            raise LinkingError("attempt_ended")
        try:
            await asyncio.wait_for(self.ready.wait(), 15)
        except TimeoutError as exc:
            raise LinkingError("not_ready") from exc
        fingerprint = hashlib.sha256(phone.encode()).hexdigest()
        async with self.lock:
            if attempt != self.attempt or self.phase in {"canceling", "canceled"}:
                raise LinkingError("stale_attempt")
            if self.authenticated:
                raise LinkingError("already_connected")
            if self.qr_deadline <= self.clock():
                raise LinkingError("expired")
            if self.phone_fingerprint and fingerprint != self.phone_fingerprint:
                raise LinkingError("cancel_required")
            if self.phone_code:
                return self.phone_code
            task = self.phone_task
            if task is None or task.done():
                async def request_code():
                    return await asyncio.wait_for(
                        asyncio.to_thread(self.session.whatsapp.PairPhone, phone), 20,
                    )
                self.phone_task = task = asyncio.create_task(request_code())
                self.phone_fingerprint = fingerprint
        try:
            code = await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A rejected number or transient PairPhone failure keeps the live QR
            # attempt; only the number binding is released so it can be retried.
            async with self.lock:
                if self.phone_task is task:
                    self.phone_task = None
                    if not self.phone_code:
                        self.phone_fingerprint = ""
            raise LinkingError("phone_pair_failed") from exc
        async with self.lock:
            if attempt != self.attempt or self.phase in {"canceling", "canceled"}:
                raise LinkingError("stale_attempt")
            self.mode = "code"
            self.change("needs_pair_code")
            self.phone_code, self.phone_fingerprint = code, fingerprint
            return code

    async def received_qr(self, value):
        if self.phase in {"canceled", "canceling"} or self.authenticated:
            return None
        try:
            data = json.loads(value)
            raw, ttl = data["code"], float(data["ttl"])
            if not isinstance(raw, str) or not 0 < ttl <= 60:
                raise ValueError
        except (ValueError, KeyError, TypeError):
            self.change("connection_error", "invalid_qr_event")
            return None
        if not self.attempt:
            self.reserve()
        attempt = self.attempt
        self.qr_deadline = self.clock() + ttl
        self.qr = b""  # The previous PNG must never be served under the new revision.
        self.ready.set()  # Native websocket is ready, independently of PNG/HTTP.
        self.phase = "needs_pair_code" if self.mode == "code" else "needs_qr"
        self.revision += 1
        self.qr_revision += 1
        qr_revision = self.qr_revision

        def render():
            import qrcode
            output = io.BytesIO()
            qrcode.make(raw).save(output, format="PNG")
            return output.getvalue()

        try:
            png = await asyncio.to_thread(render)
        except Exception:
            self.fail("qr_render_failed")
            return None
        if (attempt == self.attempt and qr_revision == self.qr_revision
                and self.qr_deadline > self.clock()):
            self.qr = png
            self.notify()
        return raw


def state(session):
    if not hasattr(session, "_can_linking"):
        session._can_linking = LinkingSession(session)
    return session._can_linking


def install_session(cls):
    """Wrap rather than replace existing contact, media, read-state and voice hooks."""
    if getattr(cls, "_can_linking_installed", False):
        return
    cls._can_linking_installed = True
    original_login, original_qr = cls.login, cls.on_wa_qr
    original_pair, original_connect = cls.on_wa_pair, cls.on_wa_connect
    original_logout, original_unlinked = cls.logout, cls.on_wa_logged_out

    async def login(self):
        return await state(self).login(original_login)

    async def qr(self, value):
        raw = await state(self).received_qr(value)
        if raw is not None:
            await original_qr(self, raw)  # Keep generic XMPP clients supported.

    async def pair(self, device_id):
        owner = state(self)
        if owner.phase in {"canceling", "canceled"}:
            owner.paired_after_cancel = True
            if owner.phase == "canceled":
                owner.change("canceled", "paired_after_cancel")
        else:
            owner.change("paired")
        await original_pair(self, device_id)  # Persist before considering authentication.

    async def connect(self, event):
        owner = state(self)
        if owner.phase in {"canceling", "canceled"}:
            return
        if event.Error or not event.JID:
            owner.authenticated = False
            reason = event.Error.removeprefix("can-link:") or "invalid_connection"
            if reason == "disconnected":
                owner.change("connecting", reason)
            elif reason == "expired":
                owner.change("expired", reason)
            else:
                owner.change("connection_error", reason if reason in {
                    "client_outdated", "stream_replaced", "pair_failed", "unsupported_pairing",
                } else "connection_failed")
            if reason == "disconnected":
                return  # WhatsMeow owns normal reconnect; do not fail a pending handshake.
            if not event.Error:
                from types import SimpleNamespace
                event = SimpleNamespace(Error="invalid_connection", JID="")
        else:
            owner.authenticated = True
            owner.change("connected")
        await original_connect(self, event)
        if owner.authenticated and owner.sync_error:
            owner.retry_sync()

    async def logout(self):
        owner = state(self)
        owner.authenticated = False
        owner.change("needs_relogin", "disconnected")
        await original_logout(self)
        current = asyncio.current_task()
        tasks = [task for task in (owner.task, owner.bootstrap, owner.sync_task,
                                  owner.phone_task)
                 if task is not None and task is not current and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def unlinked(self, event):
        owner = state(self)
        owner.authenticated = False
        owner.change("logged_out", "device_removed")
        await original_unlinked(self, event)

    cls.login, cls.on_wa_qr = login, qr
    cls.on_wa_pair, cls.on_wa_connect = pair, connect
    cls.logout, cls.on_wa_logged_out = logout, unlinked


def install(gateway):
    from slixmpp.exceptions import XMPPError
    from slixmpp.xmlstream.handler import CoroutineCallback
    from slixmpp.xmlstream.matcher import MatchXPath

    from .session import Session

    install_session(Session)
    original_login_wrap = gateway.login_wrap

    async def monitored_login(session):
        try:
            result = await original_login_wrap(session)
            if not state(session).authenticated:
                session.logged = False
            else:
                state(session).sync_error = False
            return result
        except Exception as exc:
            owner = state(session)
            if owner.authenticated:
                owner.sync_error = True
                owner.revision += 1
                owner.notify()
                logging.getLogger(__name__).warning(
                    "WhatsApp authenticated; initial roster sync failed, retrying",
                    exc_info=not roster_retry_after(exc),
                )
                owner.retry_sync(exc)
                return "WhatsApp conectado; sincronización pendiente."
            raise
        finally:
            session.is_logging_in = False

    gateway.login_wrap = monitored_login
    gateway["xep_0030"].add_feature(NAMESPACE)

    async def handle(iq):
        query = iq.xml.find(f"{{{NAMESPACE}}}query")
        if query is None or iq["type"] not in {"get", "set"}:
            return
        sender = iq["from"]
        if not sender.resource or iq["to"].bare != gateway.boundjid.bare:
            raise XMPPError("forbidden")
        gateway.raise_if_not_allowed_jid(sender)
        session = gateway.get_session_from_jid(sender)
        operation = query.get("op", "status")
        if operation != "status" and iq["type"] != "set":
            raise XMPPError("bad-request")
        code = None
        if session is None:
            if operation != "status":
                raise XMPPError("registration-required")
            attrs = {"version": "1", "phase": "needs_registration", "authenticated": "0"}
        else:
            if str(session.user_jid.bare) != str(sender.bare):
                raise XMPPError("forbidden")
            owner = state(session)
            try:
                if operation == "start":
                    await owner.start(query.get("request", ""))
                elif operation == "cancel":
                    await owner.cancel(query.get("attempt", ""))
                elif operation == "phone":
                    code = await owner.phone(query.get("attempt", ""), query.get("phone", ""))
                elif operation == "sync":
                    owner.request_sync()
                elif operation != "status":
                    raise LinkingError("unsupported_operation")
            except LinkingError as exc:
                reply = iq.reply()
                reply.xml.append(ET.Element(f"{{{NAMESPACE}}}state", {
                    **owner.snapshot(), "error": exc.reason,
                }))
                reply.send()
                return
            attrs = owner.snapshot()
        reply = iq.reply()
        payload = ET.SubElement(reply.xml, f"{{{NAMESPACE}}}state", attrs)
        if session is not None and owner.qr and float(attrs["qr_ttl"]) > 0:
            ET.SubElement(payload, f"{{{NAMESPACE}}}qr", {"mime": "image/png"}).text = (
                base64.b64encode(owner.qr).decode("ascii")
            )
        if session is not None and not code and owner.phase == "needs_pair_code" and (
                owner.phone_code and float(attrs["qr_ttl"]) > 0):
            # A lost phone reply must not strand the user: status re-delivers the
            # live code to its owner, exactly like the live QR.
            code = owner.phone_code
        if code:
            ET.SubElement(payload, f"{{{NAMESPACE}}}code").text = code
        reply.send()

    gateway.register_handler(CoroutineCallback(
        "CAN WhatsApp linking", MatchXPath(f"{{{gateway.default_ns}}}iq/{{{NAMESPACE}}}query"),
        handle,
    ))
