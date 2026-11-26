"""CAN history v1: mark history stanzas and forward the phone's unread summary.

The client must never present WhatsApp history as new, but it must keep the
phone's real unread state. Both travel as structured XML, never as bodies.
"""
from __future__ import annotations

import logging
from functools import wraps
from xml.etree import ElementTree as ET

from slidge import can_history

NAMESPACE = can_history.NAMESPACE
HINTS = "urn:xmpp:hints"
UNREAD_SUMMARY_KIND = 100  # ReceiptCANUnreadSummary in event.go
UNREAD_PREFIX = "can-unread:1:"
MAX_UNREAD = 100_000
log = logging.getLogger(__name__)


def parse_unread_summary(receipt) -> tuple[int, bool] | None:
    """Return (count, marked) for a CAN summary receipt, or None."""
    if getattr(receipt, "Kind", None) != UNREAD_SUMMARY_KIND:
        return None
    try:
        ids = list(receipt.MessageIDs)
    except TypeError:
        return None
    if len(ids) != 1 or not str(ids[0]).startswith(UNREAD_PREFIX):
        return None
    count, _, marked = str(ids[0]).removeprefix(UNREAD_PREFIX).partition(":")
    if not count.isdigit() or marked not in {"0", "1"}:
        return None
    return min(int(count), MAX_UNREAD), marked == "1"


async def chat_jid_for(session, chat) -> str:
    """The XMPP chat JID of an existing chat; never create a group for a summary."""
    from slidge.contact.roster import ContactIsUser

    if not chat.JID:
        return ""
    if chat.IsGroup:
        muc = await session.bookmarks.by_legacy_id(chat.JID, create=False)
        return str(muc.jid.bare) if muc is not None else ""
    try:
        contact = await session.contacts.by_legacy_id(chat.JID)
    except ContactIsUser:
        return ""
    return str(contact.jid.bare)


def send_unread_summary(session, chat_jid: str, count: int, marked: bool, group: bool):
    """Bodyless, from the component bare JID; offline-deliverable, never archived."""
    xmpp = session.xmpp
    msg = xmpp.make_message(mto=session.user_jid.bare, mfrom=xmpp.boundjid.bare,
                            mtype="normal")
    msg.xml.append(ET.Element(f"{{{NAMESPACE}}}unread", {
        "chat": chat_jid, "count": str(count), "marked": "1" if marked else "0",
        "group": "1" if group else "0",
    }))
    msg.xml.append(ET.Element(f"{{{HINTS}}}no-permanent-store"))
    msg.send()


def install(session_class) -> None:
    if getattr(session_class, "_can_history_installed", False):
        return
    session_class._can_history_installed = True
    original_message = session_class.on_wa_message
    original_receipt = session_class.on_wa_receipt

    @wraps(original_message)
    async def on_wa_message(self, message):
        token = can_history.scope(bool(getattr(message, "IsHistory", False)))
        try:
            return await original_message(self, message)
        finally:
            can_history.reset(token)

    @wraps(original_receipt)
    async def on_wa_receipt(self, receipt):
        summary = parse_unread_summary(receipt)
        if summary is None:
            if getattr(receipt, "Kind", None) == UNREAD_SUMMARY_KIND:
                return  # A malformed summary is never a read or delivery receipt.
            return await original_receipt(self, receipt)
        try:
            chat_jid = await chat_jid_for(self, receipt.Chat)
        except Exception:
            log.debug("unread summary chat not resolved", exc_info=True)
            return
        if chat_jid:
            send_unread_summary(self, chat_jid, *summary, bool(receipt.Chat.IsGroup))

    session_class.on_wa_message = on_wa_message
    session_class.on_wa_receipt = on_wa_receipt
