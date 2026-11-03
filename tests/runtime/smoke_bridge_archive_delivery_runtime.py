"""Exercise actual participant delivery/SQLite locks, readers and private health socket offline."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import socket
import sqlite3
import sys
import threading
from copy import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, "/opt/group-echo-tools")
from slidge.group.participant import LegacyParticipant
from slixmpp import JID, ClientXMPP, Message, Presence
from smoke_bridge_group_echo_runtime import group_fixture, marker, messages


class Participant(SimpleNamespace):
    _send = LegacyParticipant._send
    def _LegacyParticipant__add_nick_element(self, *args):
        pass

    def _LegacyParticipant__send_presence_if_needed(self, *args):
        pass

    if hasattr(LegacyParticipant, "_can_archive_and_send"):
        _can_archive_and_send = LegacyParticipant._can_archive_and_send


async def drain(group):
    tasks = list(getattr(group.archive, "_can_pending_deliveries", ()))
    if tasks:
        await asyncio.gather(*tasks)
    await asyncio.sleep(0)


async def delivery(baseline, persistent=False):
    with group_fixture() as (path, engine, store, group, _):
        owner = threading.get_ident()
        gateway = SimpleNamespace(loop=asyncio.get_running_loop(), log=logging.getLogger("fixture"))
        participant = Participant(
            muc=group, session=group.session, xmpp=gateway, occupant_id="fixture-occupant",
            is_user=False, role="participant", affiliation="member", contact=None,
            is_system=True, stored=SimpleNamespace(occupant_id="fixture-occupant"),
        )
        # The inherited archive preparer needs only these owner-loop values.
        group.is_anonymous = True
        sent, released = [], []
        reader = sqlite3.connect(path)
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM mam").fetchall()

        def release():
            reader.close()
            released.append(True)

        def send(stanza):
            assert threading.get_ident() == owner
            assert messages(store), "sent before archive commit"
            sent.append(copy(stanza))

        timer = None if persistent else asyncio.get_running_loop().call_later(.025, release)
        failed = False
        try:
            with patch.object(Message, "send", send):
                stanza = marker()
                stanza.set_from("group@bridge.invalid/fixture")
                stanza["stanza_id"]["id"] = "first"
                try:
                    assert participant._send(stanza, legacy_msg_id="legacy-first") is stanza
                    # Caller mutations cannot alter the queued archive or delivery.
                    stanza["body"] = "mutated after send"
                    await drain(group)
                except Exception as exc:
                    assert "database is locked" in str(exc), type(exc).__name__
                    failed = True
        finally:
            if timer:
                timer.cancel()
            reader.close()
        if baseline:
            assert failed and not released and not sent
            return
        assert failed == persistent
        if persistent:
            assert gateway._can_archive_failed and not sent and not messages(store)
        else:
            assert released and len(sent) == 2 and len(messages(store)) == 1
            assert all(m["body"] != "mutated after send" for m in sent)
            assert messages(store)[0][3] == "legacy-first"
        assert engine.pool.checkedout() == 0


async def ordering_and_reader():
    from slidge.can_archive_delivery import delivery_scope

    with group_fixture() as (_, engine, store, group, _):
        group.is_anonymous = True
        gateway = SimpleNamespace(loop=asyncio.get_running_loop(), log=logging.getLogger("fixture"))
        actor = Participant(muc=group, session=group.session, xmpp=gateway,
                            occupant_id="fixture", is_user=False)
        sent = []
        with patch.object(Message, "send", lambda m: sent.append(m["stanza_id"]["id"])):
            @delivery_scope
            async def native_callback():
                for name in ("incoming", "receipt", "history"):
                    msg = marker()
                    msg["stanza_id"]["id"] = name
                    actor._send(msg, archive_only=name == "history", legacy_msg_id=name)
                # Calling echo immediately must not overtake the already queued sends.
                await group.echo(marker(), "own-echo")

            await native_callback()
            assert not getattr(group.archive, "_can_pending_deliveries", ())
            assert sent == ["incoming", "incoming", "receipt", "receipt", "own-echo", "own-echo"]
            assert len(messages(store)) == 4
            # Replay is room/id scoped and does not create another archive row.
            msg = marker()
            msg["stanza_id"]["id"] = "incoming"
            actor._send(msg, archive_only=True, legacy_msg_id="incoming")
            await drain(group)
            assert len(messages(store)) == 4
            # A paused MAM consumer must not pin a SQL connection or read transaction.
            iterator = group.archive.get_all(last_page_n=2)
            assert next(iterator)
            assert engine.pool.checkedout() == 0, "reader stays open across yield"
            await group.archive.add_async(msg)
            iterator.close()
            assert engine.pool.checkedout() == 0
        # Targeted history/presence keeps the old synchronous, non-archive route.
        actor._LegacyParticipant__send_presence_if_needed = lambda *args: None
        with patch.object(Message, "send"), patch.object(Presence, "send"):
            actor._send(marker(), full_jid=JID("fixture@example.invalid/resource"))
            actor._send(Presence(), full_jid=JID("fixture@example.invalid/resource"))
        assert not getattr(group.archive, "_can_pending_deliveries", ())


async def health(expect_probe_failure=False):
    from slidge.can_health import SOCKET, check, install

    gateway = SimpleNamespace(
        loop=asyncio.get_running_loop(), is_connected=lambda: True,
        infer_real_domain=lambda: JID("example.invalid"), boundjid=JID("bridge.example.invalid")
    )

    class Gateway(SimpleNamespace):
        def __getitem__(self, key):
            return self.plugin

    gateway = Gateway(**vars(gateway), plugin=SimpleNamespace(ping=AsyncMock()))
    install(gateway)
    try:
        await asyncio.sleep(.025)
        assert await asyncio.to_thread(check)
        gateway.plugin.ping.assert_awaited_once()
        gateway._can_archive_failed = True
        assert not await asyncio.to_thread(check)
        gateway._can_archive_failed = False
        gateway.is_connected = lambda: False
        assert not await asyncio.to_thread(check)
        gateway.is_connected = lambda: True
        gateway.plugin.ping.side_effect = TimeoutError()
        assert not await asyncio.to_thread(check)
        # A Docker probe that times out during startup must not crash Slidge
        # when the owner loop resumes and tries to answer its closed socket.
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        errors = []
        loop.set_exception_handler(lambda _loop, context: errors.append(context))
        try:
            async def delayed_ping(*args, **kwargs):
                await asyncio.sleep(.1)

            gateway.plugin.ping.side_effect = delayed_ping

            def abandoned_probe():
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                    sock.settimeout(.01)
                    sock.connect(SOCKET)
                    try:
                        sock.recv(32)
                    except TimeoutError:
                        pass

            await asyncio.to_thread(abandoned_probe)
            await asyncio.sleep(.2)
            assert bool(errors) == expect_probe_failure, (
                "closed probe failure did not match regression expectation"
            )
        finally:
            loop.set_exception_handler(previous)
    finally:
        gateway._can_health_task.cancel()
        await asyncio.gather(gateway._can_health_task, return_exceptions=True)


async def run(baseline, expect_probe_failure=False):
    client = ClientXMPP("fixture@example.invalid/offline", "unused-fixture-password")
    for name in ("xep_0045", "xep_0308", "xep_0333", "xep_0334", "xep_0359", "xep_0421"):
        client.register_plugin(name)
    if expect_probe_failure:
        await health(True)
        print(json.dumps({"abandoned_probe": "baseline_reproduced"}))
        return
    await delivery(baseline)
    if not baseline:
        await delivery(False, persistent=True)
        await ordering_and_reader()
        await health()
    print(json.dumps({"archive_delivery": "baseline_reproduced" if baseline else "pass"}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expect-blocking", action="store_true")
    parser.add_argument("--expect-probe-failure", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.expect_blocking, args.expect_probe_failure))
