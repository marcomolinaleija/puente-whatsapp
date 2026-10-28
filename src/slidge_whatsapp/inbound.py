"""Cancel obsolete callback waits on explicit session teardown, not on network blips."""
from __future__ import annotations

import asyncio
from functools import wraps


def install(session_class, whatsapp):
    if getattr(session_class, "_can_inbound_installed", False):
        return
    original_event = session_class.handle_event
    original_login = session_class.login
    original_logout = session_class.logout
    original_logged_out = session_class.on_wa_logged_out
    control = {whatsapp.EventQRCode, whatsapp.EventPairDeviceID, whatsapp.EventConnect,
               whatsapp.EventLoggedOut}

    def cancel_data(session):
        for task in tuple(getattr(session, "_can_inbound_tasks", ())):
            if task is not asyncio.current_task():
                task.cancel()

    @wraps(original_event)
    async def event(session, kind, ptr):
        if kind in control:
            return await original_event(session, kind, ptr)
        tasks = getattr(session, "_can_inbound_tasks", None)
        if tasks is None:
            tasks = session._can_inbound_tasks = set()
        task = asyncio.current_task()
        tasks.add(task)
        try:
            # Do not let cancellation of an old callback cancel the shared gates.
            await asyncio.shield(session.contacts.ready)
            await asyncio.shield(session.bookmarks.ready)
            return await original_event(session, kind, ptr)
        finally:
            tasks.discard(task)

    @wraps(original_login)
    async def login(session):
        cancel_data(session)
        return await original_login(session)

    @wraps(original_logout)
    async def logout(session):
        cancel_data(session)
        return await original_logout(session)

    @wraps(original_logged_out)
    async def logged_out(session, *args, **kwargs):
        cancel_data(session)
        return await original_logged_out(session, *args, **kwargs)

    session_class.handle_event = event
    session_class.login = login
    session_class.logout = logout
    session_class.on_wa_logged_out = logged_out
    session_class._can_inbound_installed = True
