"""No-network smoke: actual generated gopy callback, native lanes and retained payloads."""
from __future__ import annotations

import asyncio
import json

from slidge_whatsapp.generated import whatsapp
from slidge_whatsapp.session import make_sync


async def run():
    gate = asyncio.Event()
    history_started, controls, messages = set(), set(), {0: [], 1: []}
    callbacks, sessions, retained = [], [], []
    gateway = whatsapp.NewGateway()

    def make_callback(account):
        async def handler(kind, ptr):
            event = whatsapp.EventPayload(handle=ptr)
            if kind == whatsapp.EventConnect:
                controls.add(account)
            elif event.Message.IsHistory:
                history_started.add(account)
                await gate.wait()
                # The native payload must still be valid across an asynchronous wait.
                assert event.Message.ID == "history"
                retained.append(event)
            else:
                await asyncio.sleep(0)
                messages[account].append(int(event.Message.ID))
        return make_sync(handler, asyncio.get_running_loop())

    for account in range(2):
        session = gateway.NewSession(whatsapp.LinkedDevice())
        callback = make_callback(account)
        callbacks.append(callback)  # gopy's C callback borrows this Python callable.
        sessions.append(session)
        session.SetEventHandler(callback)
    async with asyncio.timeout(8):
        while (len(history_started) != 2 or len(controls) != 2
               or any(len(v) != 100 for v in messages.values())):
            await asyncio.sleep(0.01)
    assert not gate.is_set()
    assert messages == {0: list(range(100)), 1: list(range(100))}
    gate.set()
    async with asyncio.timeout(3):
        while len(retained) != 2:
            await asyncio.sleep(0.01)
    for session in sessions:
        session.Disconnect()
    await asyncio.sleep(0.1)
    print(json.dumps({"native_callback_isolation": "pass", "accounts": 2,
                      "live_messages": 200, "blocked_history_preserved": 2}))


if __name__ == "__main__":
    asyncio.run(run())
