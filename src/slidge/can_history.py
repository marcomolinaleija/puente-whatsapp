"""Mark stanzas derived from WhatsApp history so clients never present them as new."""
from __future__ import annotations

from contextvars import ContextVar
from xml.etree import ElementTree as ET

NAMESPACE = "urn:can:whatsapp:history:0"
_active: ContextVar[bool] = ContextVar("can_history", default=False)


def active() -> bool:
    return _active.get()


def scope(flag: bool):
    """Return a token for ``reset``; nested calls keep their own value."""
    return _active.set(bool(flag))


def reset(token) -> None:
    _active.reset(token)


def mark(msg) -> None:
    """Add the marker once; it carries no content and no identifiers."""
    xml = getattr(msg, "xml", None)
    if xml is None or xml.find(f"{{{NAMESPACE}}}history") is not None:
        return
    xml.append(ET.Element(f"{{{NAMESPACE}}}history"))
