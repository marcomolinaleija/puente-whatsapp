from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

from slidge_whatsapp import mixins as mixins_module
from slidge_whatsapp import session as session_module
from slidge_whatsapp.deepgram_transcription import (
    build_audio_dedup_key,
    dump_public_configuration,
    format_audio_duration,
    handle_transcription_command,
    is_audio_attachment,
    transcribe_audio,
)


class UnusedHTTP:
    def post(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("Deepgram must not be called without an API key")


async def main() -> None:
    session_source = Path(session_module.__file__).read_text(encoding="utf-8")
    mixins_source = Path(mixins_module.__file__).read_text(encoding="utf-8")
    assert "Skipping duplicate audio transcription" in session_source
    assert "Audio: {format_audio_duration" in session_source
    assert "handle_transcription_command(" in mixins_source
    assert "📝" not in session_source

    audio = SimpleNamespace(content_type="audio/ogg; codecs=opus")
    image = SimpleNamespace(content_type="image/jpeg")
    assert is_audio_attachment(audio)
    assert not is_audio_attachment(image)
    assert format_audio_duration(62.1) == "1 min 2 s"
    assert build_audio_dedup_key("message", 0, b"data") == "message:0"
    assert json.loads(dump_public_configuration())["model"] == "nova-3"

    previous = os.environ.pop("DEEPGRAM_API_KEY", None)
    try:
        assert await transcribe_audio(UnusedHTTP(), b"OggS-test") is None  # type: ignore[arg-type]
        response = await handle_transcription_command(  # type: ignore[arg-type]
            UnusedHTTP(), "smoke@example.test", "/status"
        )
        assert response == "Servicio de transcripción no configurado correctamente."
    finally:
        if previous is not None:
            os.environ["DEEPGRAM_API_KEY"] = previous


asyncio.run(main())
print("optional Deepgram transcription runtime smoke: ok")
