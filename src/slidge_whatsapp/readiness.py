"""CAN readiness split v1: which native events need the group list.

Individual chats only need the contact roster. Holding them behind a slow or
rate-limited group list (up to 30 minutes) silently stops all messaging, so
only events that touch a group wait for the bookmarks future.
"""
from __future__ import annotations


def _call_is_group(call) -> bool:
    from .session import parse_call_metadata  # Lazy: session imports this module.

    metadata = parse_call_metadata(getattr(getattr(call, "Actor", None), "LID", ""))
    return bool(metadata and metadata.get("group_jid"))


def event_requires_groups(kind, event, whatsapp) -> bool:
    if kind == whatsapp.EventContact or kind == whatsapp.EventPresence:
        return False
    if kind == whatsapp.EventMessage:
        return bool(event.Message.Chat.IsGroup)
    if kind == whatsapp.EventReceipt:
        return bool(event.Receipt.Chat.IsGroup)
    if kind == whatsapp.EventChatState:
        return bool(event.ChatState.Chat.IsGroup)
    if kind == whatsapp.EventAvatar:
        return bool(event.Avatar.IsGroup)
    if kind == whatsapp.EventCall:
        return _call_is_group(event.Call)
    # Groups and any unknown kind keep the original, conservative wait.
    return True


def stanza_requires_groups(stanza) -> bool:
    """Outgoing XMPP stanzas: WhatsApp groups live at '#<id>@component'."""
    try:
        if stanza.get_type() == "groupchat":
            return True
        local = stanza.get_to().local or ""
    except Exception:
        return True
    return local.startswith("#")
