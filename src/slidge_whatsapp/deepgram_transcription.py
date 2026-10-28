from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import aiohttp

DEEPGRAM_LISTEN_URL = "https://api.deepgram.com/v1/listen"
DEEPGRAM_PROJECTS_URL = "https://api.deepgram.com/v1/projects"
DEFAULT_MODEL = "nova-3"
DEFAULT_LANGUAGE = "es-419"
DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_STATUS_TIMEOUT_SECONDS = 15.0
DEFAULT_MAX_AUDIO_BYTES = 25 * 1024 * 1024
DEFAULT_MAX_DURATION_SECONDS = 15 * 60.0
DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_FFPROBE_TIMEOUT_SECONDS = 15.0
DEFAULT_DEDUP_RETENTION_DAYS = 30
DEFAULT_PROCESSING_STALE_SECONDS = 10 * 60.0


@dataclass(frozen=True)
class AudioInspection:
    accepted: bool
    reason: str
    duration_seconds: float | None


class DeepgramAPIError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _configured_api_key() -> str:
    return os.environ.get("DEEPGRAM_API_KEY", "").strip()


def _configured_owner_api_key() -> str:
    return os.environ.get("DEEPGRAM_OWNER_API_KEY", "").strip()


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on", "si", "sí"}


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return max(minimum, value)


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return max(minimum, value)


def _media_type(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().casefold()


def _mime_set(name: str) -> frozenset[str]:
    return frozenset(
        item.strip().casefold()
        for item in os.environ.get(name, "").split(",")
        if item.strip()
    )


def _state_path() -> Path:
    configured = os.environ.get("DEEPGRAM_STATE_PATH", "").strip()
    if configured:
        return Path(configured)
    slidge_home = Path(os.environ.get("SLIDGE_HOME_DIR", "/var/lib/slidge"))
    return slidge_home / "deepgram-transcription.sqlite3"


@contextmanager
def _connect_state() -> Iterator[sqlite3.Connection]:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(path, timeout=5)
    try:
        database.execute("PRAGMA journal_mode = WAL")
        database.execute(
            """
            CREATE TABLE IF NOT EXISTS account_state (
                account TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL,
                last_success_at REAL
            )
            """
        )
        database.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_audio (
                account TEXT NOT NULL,
                dedupe_key TEXT NOT NULL,
                status TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (account, dedupe_key)
            )
            """
        )
        database.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_state (
                account TEXT NOT NULL,
                chat TEXT NOT NULL,
                enabled INTEGER NOT NULL,
                PRIMARY KEY (account, chat)
            )
            """
        )
        yield database
        database.commit()
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()


def _account_key(account: str) -> str:
    return account.strip().casefold()


def _chat_key(chat: str) -> str:
    return chat.strip().casefold()


def _jid_allowed(account: str, variable: str) -> bool:
    allowed = {
        item.strip().casefold()
        for item in os.environ.get(variable, "").split(",")
        if item.strip()
    }
    return not allowed or _account_key(account) in allowed


def _global_transcription_enabled(account: str) -> bool:
    account = _account_key(account)
    with _connect_state() as database:
        row = database.execute(
            "SELECT enabled FROM account_state WHERE account = ?", (account,)
        ).fetchone()
    if row is not None:
        return bool(row[0])
    return _env_bool("DEEPGRAM_TRANSCRIPTION_ENABLED", True)


def chat_transcription_override(account: str, chat: str) -> bool | None:
    account = _account_key(account)
    chat = _chat_key(chat)
    if not chat:
        return None
    with _connect_state() as database:
        row = database.execute(
            "SELECT enabled FROM chat_state WHERE account = ? AND chat = ?",
            (account, chat),
        ).fetchone()
    return None if row is None else bool(row[0])


def transcription_enabled(account: str, chat: str = "") -> bool:
    if not _configured_api_key() or not _jid_allowed(account, "DEEPGRAM_ALLOWED_JIDS"):
        return False
    override = chat_transcription_override(account, chat)
    if override is not None:
        return override
    return _global_transcription_enabled(account)


def set_transcription_enabled(account: str, enabled: bool) -> None:
    account = _account_key(account)
    with _connect_state() as database:
        database.execute(
            """
            INSERT INTO account_state(account, enabled)
            VALUES (?, ?)
            ON CONFLICT(account) DO UPDATE SET enabled = excluded.enabled
            """,
            (account, int(enabled)),
        )


def set_chat_transcription_override(
    account: str,
    chat: str,
    enabled: bool | None,
) -> None:
    account = _account_key(account)
    chat = _chat_key(chat)
    if not chat:
        raise ValueError("chat must not be empty")
    with _connect_state() as database:
        if enabled is None:
            database.execute(
                "DELETE FROM chat_state WHERE account = ? AND chat = ?",
                (account, chat),
            )
            return
        database.execute(
            """
            INSERT INTO chat_state(account, chat, enabled)
            VALUES (?, ?, ?)
            ON CONFLICT(account, chat) DO UPDATE SET enabled = excluded.enabled
            """,
            (account, chat, int(enabled)),
        )


def last_success_at(account: str) -> float | None:
    account = _account_key(account)
    with _connect_state() as database:
        row = database.execute(
            "SELECT last_success_at FROM account_state WHERE account = ?", (account,)
        ).fetchone()
    if row is None or row[0] is None:
        return None
    return float(row[0])


def build_audio_dedup_key(message_id: str, index: int, data: bytes) -> str:
    if message_id:
        return f"{message_id}:{index}"
    digest = hashlib.sha256(data).hexdigest()
    return f"sha256:{digest}"


def claim_audio(account: str, dedupe_key: str) -> bool:
    account = _account_key(account)
    now = time.time()
    stale_before = now - DEFAULT_PROCESSING_STALE_SECONDS
    retention_days = _env_int(
        "DEEPGRAM_DEDUP_RETENTION_DAYS", DEFAULT_DEDUP_RETENTION_DAYS, minimum=1
    )
    retention_before = now - retention_days * 86400
    with _connect_state() as database:
        database.execute(
            "DELETE FROM processed_audio WHERE updated_at < ?", (retention_before,)
        )
        inserted = database.execute(
            """
            INSERT OR IGNORE INTO processed_audio(account, dedupe_key, status, updated_at)
            VALUES (?, ?, 'processing', ?)
            """,
            (account, dedupe_key, now),
        )
        if inserted.rowcount == 1:
            return True
        reclaimed = database.execute(
            """
            UPDATE processed_audio
            SET status = 'processing', updated_at = ?
            WHERE account = ? AND dedupe_key = ?
              AND status = 'processing' AND updated_at < ?
            """,
            (now, account, dedupe_key, stale_before),
        )
        return reclaimed.rowcount == 1


def complete_audio(account: str, dedupe_key: str) -> None:
    account = _account_key(account)
    now = time.time()
    with _connect_state() as database:
        database.execute(
            """
            UPDATE processed_audio SET status = 'completed', updated_at = ?
            WHERE account = ? AND dedupe_key = ?
            """,
            (now, account, dedupe_key),
        )
        database.execute(
            """
            INSERT INTO account_state(account, enabled, last_success_at)
            VALUES (?, ?, ?)
            ON CONFLICT(account) DO UPDATE SET last_success_at = excluded.last_success_at
            """,
            (account, int(_env_bool("DEEPGRAM_TRANSCRIPTION_ENABLED", True)), now),
        )


def release_audio(account: str, dedupe_key: str) -> None:
    account = _account_key(account)
    with _connect_state() as database:
        database.execute(
            "DELETE FROM processed_audio WHERE account = ? AND dedupe_key = ?",
            (account, dedupe_key),
        )


def is_audio_attachment(attachment: object) -> bool:
    content_type = str(getattr(attachment, "content_type", "") or "")
    return _media_type(content_type).startswith("audio/")


async def probe_audio_duration(data: bytes) -> float | None:
    if not data:
        return None
    try:
        process = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            "pipe:0",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError):
        return None
    timeout = _env_float(
        "DEEPGRAM_FFPROBE_TIMEOUT_SECONDS", DEFAULT_FFPROBE_TIMEOUT_SECONDS, minimum=1
    )
    try:
        stdout, _stderr = await asyncio.wait_for(process.communicate(data), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        return None
    if process.returncode != 0:
        return None
    try:
        duration = float(stdout.decode("utf-8", errors="replace").strip())
    except ValueError:
        return None
    return duration if duration >= 0 else None


async def inspect_audio(data: bytes, content_type: str, filename: str = "") -> AudioInspection:
    if not data:
        return AudioInspection(False, "archivo vacío", None)
    media_type = _media_type(content_type)
    if not media_type.startswith("audio/"):
        return AudioInspection(False, "el adjunto no es audio", None)
    skipped_types = _mime_set("DEEPGRAM_SKIP_MIME_TYPES")
    if media_type in skipped_types:
        return AudioInspection(False, f"tipo de audio omitido: {media_type}", None)
    allowed_types = _mime_set("DEEPGRAM_ALLOWED_MIME_TYPES")
    if allowed_types and media_type not in allowed_types:
        return AudioInspection(False, f"tipo de audio no permitido: {media_type}", None)
    max_bytes = _env_int(
        "DEEPGRAM_MAX_AUDIO_BYTES", DEFAULT_MAX_AUDIO_BYTES, minimum=0
    )
    if max_bytes and len(data) > max_bytes:
        return AudioInspection(
            False,
            f"supera el límite de {max_bytes} bytes",
            None,
        )
    duration = await probe_audio_duration(data)
    max_duration = _env_float(
        "DEEPGRAM_MAX_DURATION_SECONDS", DEFAULT_MAX_DURATION_SECONDS, minimum=0
    )
    if duration is not None and max_duration and duration > max_duration:
        return AudioInspection(
            False,
            f"supera el límite de {format_audio_duration(max_duration)}",
            duration,
        )
    return AudioInspection(True, filename or "audio aceptado", duration)


def format_audio_duration(duration_seconds: float) -> str:
    total_seconds = max(0, int(round(duration_seconds)))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours} h {minutes} min {seconds} s"
    if minutes:
        return f"{minutes} min {seconds} s"
    return f"{seconds} s"


def _retry_delay(attempt: int, retry_after: str | None = None) -> float:
    if retry_after:
        try:
            return min(30.0, max(0.0, float(retry_after)))
        except ValueError:
            pass
    return min(8.0, 0.5 * (2**attempt))


async def transcribe_audio(
    http: aiohttp.ClientSession,
    data: bytes,
    content_type: str = "",
    filename: str = "",
) -> str | None:
    """Transcribe one audio attachment, or return None when disabled."""
    api_key = _configured_api_key()
    if not api_key or not data:
        return None

    model = os.environ.get("DEEPGRAM_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    language = (
        os.environ.get("DEEPGRAM_LANGUAGE", DEFAULT_LANGUAGE).strip()
        or DEFAULT_LANGUAGE
    )
    timeout_seconds = _env_float(
        "DEEPGRAM_TRANSCRIPTION_TIMEOUT_SECONDS",
        DEFAULT_TIMEOUT_SECONDS,
        minimum=1,
    )
    attempts = _env_int(
        "DEEPGRAM_RETRY_ATTEMPTS", DEFAULT_RETRY_ATTEMPTS, minimum=1
    )
    media_type = _media_type(content_type)
    if not media_type.startswith("audio/"):
        media_type = "application/octet-stream"

    params = {
        "model": model,
        "language": language,
        "smart_format": "true",
        "mip_opt_out": "true",
    }
    headers = {
        "Authorization": f"Token {api_key}",
        "Content-Type": media_type,
    }
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    retry_statuses = {429, 500, 502, 503, 504}
    last_error: Exception | None = None

    for attempt in range(attempts):
        retry_after: str | None = None
        try:
            async with http.post(
                DEEPGRAM_LISTEN_URL,
                params=params,
                data=data,
                headers=headers,
                timeout=timeout,
            ) as response:
                if response.status == 200:
                    payload: dict[str, Any] = await response.json()
                    try:
                        transcript = payload["results"]["channels"][0]["alternatives"][
                            0
                        ]["transcript"]
                    except (KeyError, IndexError, TypeError) as exc:
                        raise RuntimeError(
                            "Deepgram returned an unexpected response"
                        ) from exc
                    transcript = str(transcript).strip()
                    return transcript or None
                retry_after = response.headers.get("Retry-After")
                detail = (await response.text())[:500]
                error = DeepgramAPIError(
                    response.status,
                    f"Deepgram HTTP {response.status} for {filename or 'audio'}: {detail}",
                )
                if response.status not in retry_statuses:
                    raise error
                last_error = error
        except DeepgramAPIError:
            raise
        except (aiohttp.ClientError, TimeoutError) as exc:
            last_error = exc

        if attempt + 1 < attempts:
            await asyncio.sleep(_retry_delay(attempt, retry_after))

    if last_error is not None:
        raise RuntimeError(
            f"Deepgram transcription failed after {attempts} attempts: {last_error}"
        ) from last_error
    raise RuntimeError("Deepgram transcription failed")


async def _get_json(
    http: aiohttp.ClientSession,
    url: str,
    api_key: str,
) -> dict[str, Any]:
    timeout = aiohttp.ClientTimeout(
        total=_env_float(
            "DEEPGRAM_STATUS_TIMEOUT_SECONDS",
            DEFAULT_STATUS_TIMEOUT_SECONDS,
            minimum=1,
        )
    )
    headers = {"Authorization": f"Token {api_key}"}
    async with http.get(url, headers=headers, timeout=timeout) as response:
        if response.status != 200:
            detail = (await response.text())[:300]
            raise DeepgramAPIError(
                response.status, f"Deepgram HTTP {response.status}: {detail}"
            )
        payload = await response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Deepgram returned an unexpected response")
    return payload


async def validate_transcription_configuration(
    http: aiohttp.ClientSession,
) -> bool | None:
    api_key = _configured_api_key()
    if not api_key:
        return False
    try:
        await _get_json(http, DEEPGRAM_PROJECTS_URL, api_key)
    except DeepgramAPIError as exc:
        if exc.status in {401, 403}:
            return False
        return None
    except (aiohttp.ClientError, TimeoutError, RuntimeError):
        return None
    return True


async def get_credit_balances(
    http: aiohttp.ClientSession,
) -> dict[str, Decimal]:
    owner_api_key = _configured_owner_api_key()
    if not owner_api_key:
        raise RuntimeError("falta DEEPGRAM_OWNER_API_KEY")
    project_id = os.environ.get("DEEPGRAM_PROJECT_ID", "").strip()
    if not project_id:
        projects_payload = await _get_json(http, DEEPGRAM_PROJECTS_URL, owner_api_key)
        projects = projects_payload.get("projects")
        if not isinstance(projects, list) or not projects:
            raise RuntimeError("Deepgram no devolvió ningún proyecto")
        first_project = projects[0]
        if not isinstance(first_project, dict):
            raise RuntimeError("Deepgram devolvió un proyecto inválido")
        project_id = str(first_project.get("project_id", "")).strip()
        if not project_id:
            raise RuntimeError("Deepgram no devolvió el ID del proyecto")

    payload = await _get_json(
        http,
        f"{DEEPGRAM_PROJECTS_URL}/{project_id}/balances",
        owner_api_key,
    )
    raw_balances = payload.get("balances")
    if not isinstance(raw_balances, list):
        raise RuntimeError("Deepgram devolvió un saldo inválido")
    balances: dict[str, Decimal] = {}
    for item in raw_balances:
        if not isinstance(item, dict):
            continue
        units = str(item.get("units", "")).strip().casefold() or "credit"
        try:
            amount = Decimal(str(item.get("amount", "0")))
        except InvalidOperation:
            continue
        balances[units] = balances.get(units, Decimal("0")) + amount
    if not balances:
        raise RuntimeError("Deepgram no devolvió crédito disponible")
    return balances


def format_credit_balances(balances: dict[str, Decimal]) -> str:
    formatted: list[str] = []
    for units, amount in sorted(balances.items()):
        rounded = amount.quantize(Decimal("0.01"))
        if units == "usd":
            formatted.append(f"${rounded:,.2f} USD")
        else:
            formatted.append(f"{rounded:,.2f} {units.upper()}")
    return ", ".join(formatted)


def _command_authorized(account: str) -> bool:
    return _jid_allowed(account, "DEEPGRAM_COMMAND_JIDS")


async def handle_transcription_command(
    http: aiohttp.ClientSession,
    account: str,
    body: str,
    chat: str = "",
) -> str | None:
    parts = body.strip().casefold().split()
    if not parts or parts[0] not in {"/transcribe", "/stats", "/status"}:
        return None
    if not _command_authorized(account):
        return "Comando de transcripción no autorizado para esta cuenta."

    command = parts[0]
    if command == "/transcribe":
        if len(parts) == 2 and parts[1] in {"on", "off", "of"}:
            enabled = parts[1] == "on"
            if enabled and not _configured_api_key():
                return (
                    "No se puede activar: el servicio de transcripción no está "
                    "configurado."
                )
            set_transcription_enabled(account, enabled)
            return f"Transcripción {'activada' if enabled else 'desactivada'}."

        if len(parts) >= 2 and parts[1] == "here":
            if not chat:
                return "Este comando debe usarse dentro de un chat o grupo."
            if len(parts) == 2:
                override = chat_transcription_override(account, chat)
                effective = "activada" if transcription_enabled(account, chat) else "desactivada"
                if override is None:
                    return (
                        "Este chat hereda el ajuste global. "
                        f"Estado efectivo: {effective}."
                    )
                return f"Este chat tiene una excepción: transcripción {effective}."
            if len(parts) == 3 and parts[2] in {"on", "off", "of", "default"}:
                if parts[2] == "default":
                    set_chat_transcription_override(account, chat, None)
                    effective = (
                        "activada" if transcription_enabled(account, chat) else "desactivada"
                    )
                    return (
                        "Este chat vuelve a usar el ajuste global. "
                        f"Estado efectivo: {effective}."
                    )
                enabled = parts[2] == "on"
                if enabled and not _configured_api_key():
                    return (
                        "No se puede activar: el servicio de transcripción no está "
                        "configurado."
                    )
                set_chat_transcription_override(account, chat, enabled)
                return (
                    f"Transcripción {'activada' if enabled else 'desactivada'} "
                    "para este chat."
                )

        return (
            "Uso: /transcribe on|off o "
            "/transcribe here [on|off|default]."
        )

    if command == "/status":
        if not _configured_api_key():
            return "Servicio de transcripción no configurado correctamente."
        valid = await validate_transcription_configuration(http)
        if valid is False:
            return "Servicio de transcripción no configurado correctamente."
        state = "activado" if transcription_enabled(account) else "desactivado"
        if valid is None and last_success_at(account) is None:
            return (
                "Servicio de transcripción configurado, pero Deepgram no pudo "
                f"comprobarse en este momento. Estado: {state}."
            )
        return f"Servicio de transcripción configurado correctamente. Estado: {state}."

    try:
        balances = await get_credit_balances(http)
    except Exception as exc:
        return f"No se pudo consultar el crédito de Deepgram: {exc}."
    return f"Crédito disponible: {format_credit_balances(balances)}."


def dump_public_configuration() -> str:
    """Return non-secret configuration for diagnostics and image smoke tests."""
    payload = {
        "model": os.environ.get("DEEPGRAM_MODEL", DEFAULT_MODEL).strip()
        or DEFAULT_MODEL,
        "language": os.environ.get("DEEPGRAM_LANGUAGE", DEFAULT_LANGUAGE).strip()
        or DEFAULT_LANGUAGE,
        "max_audio_bytes": _env_int(
            "DEEPGRAM_MAX_AUDIO_BYTES", DEFAULT_MAX_AUDIO_BYTES, minimum=0
        ),
        "max_duration_seconds": _env_float(
            "DEEPGRAM_MAX_DURATION_SECONDS", DEFAULT_MAX_DURATION_SECONDS, minimum=0
        ),
        "retry_attempts": _env_int(
            "DEEPGRAM_RETRY_ATTEMPTS", DEFAULT_RETRY_ATTEMPTS, minimum=1
        ),
    }
    return json.dumps(payload, sort_keys=True)
