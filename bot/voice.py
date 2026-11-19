"""Transcrição de voz via OpenAI (gpt-4o-mini-transcribe, fallback whisper-1)."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import re

import httpx

log = logging.getLogger(__name__)

URL = "https://api.openai.com/v1/audio/transcriptions"
MODELS = ("gpt-4o-mini-transcribe", "whisper-1")
TIMEOUT_S = 60.0
FFMPEG_TIMEOUT_S = 60.0
_MIME = {
    ".oga": "audio/ogg",
    ".ogg": "audio/ogg",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".wav": "audio/wav",
}


class VoiceError(Exception):
    """Falha de transcrição; a mensagem nunca contém a API key."""


def _safe_name(filename: str) -> str:
    name = re.sub(
        r"[^A-Za-z0-9._-]", "_", filename.replace("\\", "/").rsplit("/", 1)[-1]
    ).lstrip(".")
    return name or "audio.oga"


def _mime(name: str) -> str:
    ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return _MIME.get(ext) or mimetypes.guess_type(name)[0] or "application/octet-stream"


def _api_error(resp: httpx.Response) -> tuple[str, str, str]:
    """(message, code, param) do corpo de erro da OpenAI; vazios se não parseável."""
    try:
        err = resp.json().get("error") or {}
        return (
            str(err.get("message") or ""),
            str(err.get("code") or ""),
            str(err.get("param") or ""),
        )
    except (ValueError, AttributeError):
        return "", "", ""


def _is_model_error(resp: httpx.Response, strict: bool = False) -> bool:
    """strict: só código/param estruturados (usado no 403, onde o texto é ambíguo)."""
    msg, code, param = _api_error(resp)
    return (
        code == "model_not_found"
        or param == "model"
        or (not strict and "model" in msg.lower() and "format" not in msg.lower())
    )


def _is_format_error(resp: httpx.Response) -> bool:
    msg, code, _ = _api_error(resp)
    return code == "unsupported_format" or "format" in msg.lower()


async def _to_mp3(audio: bytes) -> bytes:
    """Converte qualquer áudio para mp3 via ffmpeg (stdin→stdout); só usado se a API recusar o formato."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-protocol_whitelist",
            "pipe",
            "-i",
            "pipe:0",
            "-f",
            "mp3",
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")},
        )
    except OSError as e:
        raise VoiceError("ffmpeg indisponível para converter o áudio") from e
    try:
        out, _ = await asyncio.wait_for(proc.communicate(audio), FFMPEG_TIMEOUT_S)
    except asyncio.TimeoutError as e:
        raise VoiceError("ffmpeg excedeu o tempo ao converter o áudio") from e
    finally:
        # timeout, cancelamento ou erro: nunca deixa o filho vivo (só o próprio ffmpeg)
        if proc.returncode is None:
            proc.kill()
            await asyncio.shield(proc.wait())
    if proc.returncode != 0 or not out:
        raise VoiceError("ffmpeg falhou ao converter o áudio")
    return out


async def _post(
    client: httpx.AsyncClient, audio: bytes, name: str, model: str, api_key: str
) -> httpx.Response:
    try:
        return await client.post(
            URL,
            headers={"Authorization": f"Bearer {api_key.strip()}"},
            data={"model": model, "language": "pt"},
            files={"file": (name, audio, _mime(name))},
            timeout=TIMEOUT_S,
        )
    except httpx.HTTPError as e:
        raise VoiceError(f"falha de rede ao transcrever ({type(e).__name__})") from None


async def transcribe(
    audio_bytes: bytes, filename: str, api_key: str, client: httpx.AsyncClient
) -> str:
    if not audio_bytes:
        raise VoiceError("áudio vazio")
    if not api_key or not api_key.strip():
        raise VoiceError("OPENAI_API_KEY ausente")
    name = _safe_name(filename)
    audio = audio_bytes
    model_idx = 0
    converted = False
    while True:
        resp = await _post(client, audio, name, MODELS[model_idx], api_key)
        if resp.status_code == 200:
            break
        # 403 também: projeto sem acesso ao modelo devolve 403 model_not_found.
        # 403 invalid_api_key (ou qualquer 401) nunca cai aqui nem retenta.
        if resp.status_code in (400, 403, 404, 422):
            if _is_model_error(
                resp, strict=resp.status_code == 403
            ) and model_idx + 1 < len(MODELS):
                log.warning(
                    "modelo %s recusado (%s); tentando %s",
                    MODELS[model_idx],
                    resp.status_code,
                    MODELS[model_idx + 1],
                )
                model_idx += 1
                continue
            if resp.status_code != 403 and _is_format_error(resp) and not converted:
                log.warning("formato %s recusado; convertendo com ffmpeg", name)
                audio, name, converted = await _to_mp3(audio), "audio.mp3", True
                continue
        msg = _api_error(resp)[0].replace(api_key, "***")[:200]
        raise VoiceError(
            f"OpenAI respondeu {resp.status_code}" + (f": {msg}" if msg else "")
        )
    try:
        text = resp.json()["text"]
    except (ValueError, KeyError, TypeError):
        raise VoiceError("resposta inválida da OpenAI") from None
    text = text.strip() if isinstance(text, str) else ""
    if not text:
        raise VoiceError("transcrição vazia")
    return text
