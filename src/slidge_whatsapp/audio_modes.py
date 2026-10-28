"""Explicit CAN audio intent; the MIME envelope is internal to the Go binding."""

AUDIO_MODE_NS = "urn:can:audio-mode:0"
AUDIO_MODE_MIME = "application/x-can-audio-mode"
MAX_AUDIO_ATTACHMENT_BYTES = 64 * 1024 * 1024


def audio_attachment_mime(thread: str | None, mime: str, *, is_sticker: bool = False) -> str:
    thread = thread or ""
    mime = mime.strip()
    base = mime.partition(";")[0].strip().lower()
    if not thread.startswith(AUDIO_MODE_NS):
        if base == AUDIO_MODE_MIME:
            raise ValueError("Internal audio MIME cannot be supplied over HTTP")
        return mime
    mode = thread.removeprefix(AUDIO_MODE_NS + ":")
    if mode not in {"audio", "document"} or is_sticker:
        raise ValueError("Invalid CAN audio mode")
    if not base.startswith("audio/") or (mode == "audio" and base != "audio/mpeg"):
        raise ValueError("CAN audio mode requires matching audio metadata")
    if any(character in mime for character in "\r\n\x00"):
        raise ValueError("Invalid audio MIME")
    return f"{AUDIO_MODE_MIME};{mode};{mime}"


def audio_attachment_caption(thread: str | None, body: str | None, url: str) -> str:
    """An XMPP URL fallback is not a WhatsApp document caption."""
    body = body or ""
    if (thread or "").startswith(AUDIO_MODE_NS) and body.strip() == url.strip():
        return ""
    return body
