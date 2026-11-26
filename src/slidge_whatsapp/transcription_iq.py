"""Deepgram transcription settings and on-demand requests over XMPP.

The preferences dialog of WhatsApp CAN reads and writes exactly the state used by
the ``/transcribe`` chat commands, so both stay in sync. API keys are write-only:
they are never returned, logged or echoed; only the last four characters are.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
from pathlib import Path
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

from .deepgram_transcription import (
    DELIVERY_MODES,
    DELIVERY_PRIVATE,
    DeepgramAPIError,
    _configured_api_key,
    _jid_allowed,
    account_api_key,
    api_key_hint,
    api_key_source,
    auto_transcription_enabled,
    chat_transcription_overrides,
    delivery_mode,
    normalize_api_key,
    replace_chat_transcription_overrides,
    service_enabled,
    set_account_api_key,
    set_delivery_mode,
    set_service_enabled,
    set_transcription_enabled,
    transcription_available,
    validate_transcription_configuration,
)

NAMESPACE = "urn:can:whatsapp:transcription:0"
MAX_ON_DEMAND_BYTES = 25 * 1024 * 1024
log = logging.getLogger(__name__)


def _flag(value: str | None) -> bool | None:
    if value is None:
        return None
    value = value.strip().casefold()
    if value in {"1", "true", "on"}:
        return True
    if value in {"0", "false", "off"}:
        return False
    raise ValueError("invalid boolean")


def settings_xml(account: str) -> ET.Element:
    source = api_key_source(account)
    node = ET.Element(
        f"{{{NAMESPACE}}}settings",
        {
            "available": "1" if transcription_available(account) else "0",
            "service": "1" if service_enabled(account) else "0",
            "auto": "1" if auto_transcription_enabled(account) else "0",
            "delivery": delivery_mode(account),
            "key-source": source,
            "key-hint": api_key_hint(account_api_key(account)) if source == "account" else "",
        },
    )
    for chat, enabled in chat_transcription_overrides(account).items():
        ET.SubElement(
            node, f"{{{NAMESPACE}}}chat", {"jid": chat, "enabled": "1" if enabled else "0"}
        )
    return node


def parse_settings(node: ET.Element) -> dict:
    """Validate a settings update before touching any state."""
    update: dict = {}
    for name in ("service", "auto"):
        value = _flag(node.get(name))
        if value is not None:
            update[name] = value
    delivery = node.get("delivery")
    if delivery is not None:
        if delivery not in DELIVERY_MODES:
            raise ValueError("invalid delivery")
        update["delivery"] = delivery
    key = node.find(f"{{{NAMESPACE}}}api-key")
    if key is not None:
        if _flag(key.get("clear")):
            update["api_key"] = None
        else:
            update["api_key"] = normalize_api_key(key.text or "")
    chats = node.find(f"{{{NAMESPACE}}}chats")
    if chats is not None:
        overrides: dict[str, bool] = {}
        for chat in chats.findall(f"{{{NAMESPACE}}}chat"):
            jid = (chat.get("jid") or "").strip()
            enabled = _flag(chat.get("enabled"))
            if not jid or "@" not in jid or len(jid) > 3071 or enabled is None:
                raise ValueError("invalid chat")
            overrides[jid] = enabled
        update["chats"] = overrides
    return update


def has_key_after_update(account: str, update: dict) -> bool:
    if update.get("api_key"):
        return True
    if _configured_api_key() and _jid_allowed(account, "DEEPGRAM_ALLOWED_JIDS"):
        return True
    return "api_key" not in update and bool(account_api_key(account))


def apply_settings(account: str, update: dict) -> None:
    if "api_key" in update:
        set_account_api_key(account, update["api_key"])
    if "service" in update:
        set_service_enabled(account, update["service"])
    if "auto" in update:
        set_transcription_enabled(account, update["auto"])
    if "delivery" in update:
        set_delivery_mode(account, update["delivery"])
    if "chats" in update:
        replace_chat_transcription_overrides(account, update["chats"])


def stored_attachment_path(url: str, prefix: str | None, root: str | None) -> Path | None:
    """Map a bridge attachment URL to its file; never fetch arbitrary URLs."""
    if not url or not prefix or not root:
        return None
    prefix = prefix.rstrip("/") + "/"
    if not url.startswith(prefix):
        return None
    relative = unquote(urlsplit(url[len(prefix):]).path)
    if not relative or relative.startswith("/") or "\\" in relative:
        return None
    base = Path(root).resolve()
    candidate = (base / relative).resolve()
    if base not in candidate.parents or not candidate.is_file():
        return None
    return candidate


def _error_for_deepgram(exc: Exception):
    from slixmpp.exceptions import XMPPError

    if isinstance(exc, DeepgramAPIError):
        if exc.status in {401, 403}:
            return XMPPError("not-authorized", "Deepgram rechazó la clave API")
        if exc.status == 402:
            return XMPPError("payment-required", "La cuenta de Deepgram no tiene crédito")
        if exc.status == 413:
            return XMPPError("not-acceptable", "El audio es demasiado grande para Deepgram")
        return XMPPError("remote-server-not-found", f"Deepgram respondió {exc.status}")
    return XMPPError("remote-server-timeout", "Deepgram no respondió")


async def _settings(gateway, session, iq) -> None:
    from slixmpp.exceptions import XMPPError

    account = str(session.user_jid.bare)
    if iq["type"] == "set":
        node = iq.xml.find(f"{{{NAMESPACE}}}settings")
        try:
            update = parse_settings(node)
        except ValueError:
            raise XMPPError("bad-request", "Configuración de transcripción no válida") from None
        new_key = update.get("api_key")
        if new_key:
            valid = await validate_transcription_configuration(session.http, new_key)
            if valid is False:
                raise XMPPError("not-acceptable", "Deepgram rechazó la clave API")
        if update.get("service") and not has_key_after_update(account, update):
            raise XMPPError("conflict", "Falta una clave API para activar el servicio")
        await asyncio.to_thread(apply_settings, account, update)
    reply = iq.reply()
    reply.append(await asyncio.to_thread(settings_xml, account))
    reply.send()


async def _transcribe(gateway, session, iq) -> None:
    from slidge import global_config
    from slixmpp import JID
    from slixmpp.exceptions import XMPPError

    if iq["type"] != "set":
        raise XMPPError("bad-request")
    node = iq.xml.find(f"{{{NAMESPACE}}}transcribe")
    chat = (node.get("chat") or "").strip()
    audio_id = (node.get("id") or "").strip()
    delivery = node.get("delivery") or DELIVERY_PRIVATE
    try:
        own = bool(_flag(node.get("own") or "0"))
    except ValueError:
        raise XMPPError("bad-request") from None
    if not chat or not audio_id or len(audio_id) > 512 or delivery not in DELIVERY_MODES:
        raise XMPPError("bad-request")
    path = stored_attachment_path(
        node.get("url") or "",
        getattr(global_config, "NO_UPLOAD_URL_PREFIX", None),
        getattr(global_config, "NO_UPLOAD_PATH", None),
    )
    if path is None:
        raise XMPPError("item-not-found", "El puente ya no conserva este audio")
    if path.stat().st_size > MAX_ON_DEMAND_BYTES:
        raise XMPPError("not-acceptable", "El audio es demasiado grande")
    jid = JID(chat)
    recipient = session.bookmarks.by_jid_only_if_exists(jid) if jid.user.startswith("#") else None
    if recipient is None:
        recipient = session.contacts.by_jid_only_if_exists(jid)
    if recipient is None:
        raise XMPPError("item-not-found", "Chat desconocido")
    data = await asyncio.to_thread(path.read_bytes)
    content_type = mimetypes.guess_type(path.name)[0] or "audio/ogg"
    try:
        transcript = await session.transcribe_on_demand(
            recipient, audio_id, data, content_type, path.name, delivery, own
        )
    except XMPPError:
        raise
    except Exception as exc:
        log.warning("On-demand transcription failed: %s", type(exc).__name__)
        raise _error_for_deepgram(exc) from None
    reply = iq.reply()
    result = ET.Element(f"{{{NAMESPACE}}}transcribe", {"empty": "0" if transcript else "1"})
    ET.SubElement(result, f"{{{NAMESPACE}}}text").text = transcript
    reply.append(result)
    reply.send()


async def handle(gateway, iq) -> None:
    from slixmpp.exceptions import XMPPError

    try:
        session = gateway.get_session_from_jid(iq["from"])
        if session is None:
            raise XMPPError("forbidden")
        if iq.xml.find(f"{{{NAMESPACE}}}settings") is not None:
            if iq["type"] not in {"get", "set"}:
                raise XMPPError("bad-request")
            await _settings(gateway, session, iq)
        elif iq.xml.find(f"{{{NAMESPACE}}}transcribe") is not None:
            await _transcribe(gateway, session, iq)
        else:
            raise XMPPError("bad-request")
    except XMPPError:
        # slixmpp turns a raised XMPPError into the IQ error reply.
        raise
    except Exception:
        log.exception("Transcription IQ failed")
        raise XMPPError("internal-server-error") from None


def install(gateway) -> None:
    from slixmpp.xmlstream.handler import CoroutineCallback
    from slixmpp.xmlstream.matcher import MatchXPath

    gateway["xep_0030"].add_feature(NAMESPACE)

    async def on_iq(iq) -> None:
        await handle(gateway, iq)

    for child in ("settings", "transcribe"):
        gateway.register_handler(
            CoroutineCallback(
                f"CAN transcription {child}",
                MatchXPath(f"{{{gateway.default_ns}}}iq/{{{NAMESPACE}}}{child}"),
                on_iq,
            )
        )
