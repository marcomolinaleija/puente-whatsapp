"""Transcription preferences: per-account keys, IQ settings and command sync."""

from __future__ import annotations

import asyncio
import os
import stat
import tempfile
from pathlib import Path
from xml.etree import ElementTree as ET

ACCOUNT = "smoke@example.test"
OTHER = "other@example.test"
CHAT = "+10000000001@whatsapp.example.test"
GROUP = "#120000000000000001@whatsapp.example.test"


class UnusedHTTP:
    def post(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("Deepgram must not be called")

    def get(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("Deepgram must not be called")


async def main(root: Path) -> None:
    os.environ["DEEPGRAM_STATE_PATH"] = str(root / "state" / "deepgram.sqlite3")
    os.environ.pop("DEEPGRAM_CREDENTIALS_PATH", None)
    for name in ("DEEPGRAM_API_KEY", "DEEPGRAM_ALLOWED_JIDS", "DEEPGRAM_COMMAND_JIDS"):
        os.environ.pop(name, None)

    from slidge_whatsapp import deepgram_transcription as dg
    from slidge_whatsapp import transcription_iq as tiq

    ns = tiq.NAMESPACE
    http = UnusedHTTP()

    # Reading state never creates the private credentials file.
    assert dg.account_api_key(ACCOUNT) == ""
    assert not (root / "state" / "deepgram-credentials.sqlite3").exists()

    # Without any key nothing is available and commands cannot switch it on.
    assert not dg.transcription_available(ACCOUNT)
    assert not dg.transcription_enabled(ACCOUNT, CHAT)
    response = await dg.handle_transcription_command(http, ACCOUNT, "/transcribe on")
    assert response and response.startswith("No se puede activar"), response

    # Own key: stored privately, never returned, enables the account.
    dg.set_account_api_key(ACCOUNT, "  dg-smoke-key-0123456789abcd  ")
    credentials = root / "state" / "deepgram-credentials.sqlite3"
    assert credentials.is_file()
    if os.name == "posix":
        assert stat.S_IMODE(credentials.stat().st_mode) == 0o600
    assert dg.account_api_key(ACCOUNT) == "dg-smoke-key-0123456789abcd"
    assert dg.account_api_key(OTHER) == ""
    assert dg.effective_api_key(OTHER) == ""
    assert dg.api_key_source(ACCOUNT) == "account"
    settings = tiq.settings_xml(ACCOUNT)
    raw = ET.tostring(settings, encoding="unicode")
    assert "0123456789abcd" not in raw and settings.get("key-hint") == "abcd", raw
    assert settings.get("available") == "1"
    assert dg.transcription_enabled(ACCOUNT, CHAT)  # legacy default: auto on

    # Commands change the same state the dialog reads.
    assert (await dg.handle_transcription_command(http, ACCOUNT, "/transcribe off")) == (
        "Transcripción desactivada."
    )
    await dg.handle_transcription_command(http, ACCOUNT, "/transcribe reply")
    await dg.handle_transcription_command(http, ACCOUNT, "/transcribe here on", chat=CHAT)
    settings = tiq.settings_xml(ACCOUNT)
    assert settings.get("auto") == "0" and settings.get("delivery") == "reply"
    chats = {c.get("jid"): c.get("enabled") for c in settings.findall(f"{{{ns}}}chat")}
    assert chats == {CHAT.casefold(): "1"}, chats
    assert dg.transcription_enabled(ACCOUNT, CHAT)
    assert not dg.transcription_enabled(ACCOUNT, GROUP)

    # The dialog's update replaces exceptions and is seen by the commands.
    update = ET.Element(
        f"{{{ns}}}settings", {"service": "1", "auto": "1", "delivery": "private"}
    )
    chats_node = ET.SubElement(update, f"{{{ns}}}chats")
    ET.SubElement(chats_node, f"{{{ns}}}chat", {"jid": GROUP, "enabled": "0"})
    tiq.apply_settings(ACCOUNT, tiq.parse_settings(update))
    assert dg.transcription_enabled(ACCOUNT, CHAT)
    assert not dg.transcription_enabled(ACCOUNT, GROUP)
    here = await dg.handle_transcription_command(http, ACCOUNT, "/transcribe here", chat=GROUP)
    assert "excepción" in here and "desactivada" in here, here
    status = await dg.handle_transcription_command(http, ACCOUNT, "/transcribe status")
    assert "clave propia" in status and "privadas" in status, status

    # Service switch overrides automatic and per-chat settings.
    await dg.handle_transcription_command(http, ACCOUNT, "/transcribe service off")
    assert not dg.transcription_available(ACCOUNT)
    assert not dg.transcription_enabled(ACCOUNT, CHAT)
    assert tiq.settings_xml(ACCOUNT).get("service") == "0"

    # Invalid input never reaches the state.
    for bad in (
        {"delivery": "everyone"},
        {"service": "maybe"},
    ):
        try:
            tiq.parse_settings(ET.Element(f"{{{ns}}}settings", bad))
        except ValueError:
            pass
        else:
            raise AssertionError(bad)
    bad_key = ET.Element(f"{{{ns}}}settings")
    ET.SubElement(bad_key, f"{{{ns}}}api-key").text = "has space inside"
    try:
        tiq.parse_settings(bad_key)
    except ValueError:
        pass
    else:
        raise AssertionError("key with spaces accepted")

    # Clearing the key removes it; the server key still requires the allow-list.
    clear = ET.Element(f"{{{ns}}}settings")
    ET.SubElement(clear, f"{{{ns}}}api-key", {"clear": "1"})
    tiq.apply_settings(ACCOUNT, tiq.parse_settings(clear))
    assert dg.api_key_source(ACCOUNT) == "none"
    os.environ["DEEPGRAM_API_KEY"] = "server-key-0123456789"
    os.environ["DEEPGRAM_ALLOWED_JIDS"] = OTHER
    assert dg.api_key_source(ACCOUNT) == "none"
    assert dg.api_key_source(OTHER) == "server"
    assert not tiq.has_key_after_update(ACCOUNT, {"service": True})
    assert tiq.has_key_after_update(OTHER, {"service": True})

    # On-demand audio only maps into the attachment root, never outside it.
    uploads = root / "uploads"
    (uploads / "abc").mkdir(parents=True)
    audio = uploads / "abc" / "nota de voz.oga"
    audio.write_bytes(b"OggS")
    (root / "secret.txt").write_text("x")
    prefix = "http://files.example.test/att"
    assert tiq.stored_attachment_path(
        f"{prefix}/abc/nota%20de%20voz.oga", prefix, str(uploads)
    ) == audio.resolve()
    for url in (
        f"{prefix}/../secret.txt",
        f"{prefix}/abc/../../secret.txt",
        "http://evil.example.test/att/abc/nota%20de%20voz.oga",
        f"{prefix}/abc/missing.oga",
    ):
        assert tiq.stored_attachment_path(url, prefix, str(uploads)) is None, url
    assert tiq.stored_attachment_path(f"{prefix}/abc/x", None, None) is None


with tempfile.TemporaryDirectory() as directory:
    asyncio.run(main(Path(directory)))
print("Deepgram transcription settings runtime smoke: ok")
