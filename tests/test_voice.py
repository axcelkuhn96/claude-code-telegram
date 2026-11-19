from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from bot import voice
from bot.voice import VoiceError, transcribe

KEY = "sk-" + "teste" * 4  # fictícia, baixa entropia
URL = "https://api.openai.com/v1/audio/transcriptions"


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _err(status: int, message: str, code: str | None = None, param: str | None = None):
    return httpx.Response(
        status,
        json={
            "error": {
                "message": message,
                "type": "invalid_request_error",
                "code": code,
                "param": param,
            }
        },
    )


def _field(request: httpx.Request, name: str) -> str:
    body = request.content.decode("latin-1")
    marker = f'name="{name}"'
    assert marker in body, f"campo {name} ausente"
    return body.split(marker, 1)[1].split("\r\n\r\n", 1)[1].split("\r\n", 1)[0]


async def test_ok_envia_oga_direto_e_devolve_texto():
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"text": "  olá mundo \n"})

    async with _client(handler) as c:
        out = await transcribe(b"OggS-fake", "voice.oga", KEY, c)

    assert out == "olá mundo"
    assert len(seen) == 1
    req = seen[0]
    assert str(req.url) == URL
    assert req.headers["authorization"] == f"Bearer {KEY}"
    assert req.headers["content-type"].startswith("multipart/form-data")
    assert _field(req, "model") == "gpt-4o-mini-transcribe"
    assert _field(req, "language") == "pt"
    body = req.content.decode("latin-1")
    assert 'filename="voice.oga"' in body
    assert "Content-Type: audio/ogg" in body
    assert "OggS-fake" in body


async def test_erro_de_modelo_cai_para_whisper():
    models: list[str] = []

    def handler(request):
        models.append(_field(request, "model"))
        if len(models) == 1:
            return _err(
                404,
                "The model `gpt-4o-mini-transcribe` does not exist",
                "model_not_found",
                "model",
            )
        return httpx.Response(200, json={"text": "fallback ok"})

    async with _client(handler) as c:
        out = await transcribe(b"x", "a.ogg", KEY, c)

    assert out == "fallback ok"
    assert models == ["gpt-4o-mini-transcribe", "whisper-1"]


async def test_erro_de_modelo_nos_dois_levanta():
    calls = []

    def handler(request):
        calls.append(1)
        return _err(400, "invalid model", "model_not_found", "model")

    async with _client(handler) as c:
        with pytest.raises(VoiceError):
            await transcribe(b"x", "a.ogg", KEY, c)
    assert len(calls) == 2


async def test_401_nao_retenta_e_nao_vaza_key():
    calls = []

    def handler(request):
        calls.append(1)
        return _err(401, f"Incorrect API key provided: {KEY}", "invalid_api_key")

    async with _client(handler) as c:
        with pytest.raises(VoiceError) as ei:
            await transcribe(b"x", "a.ogg", KEY, c)
    assert len(calls) == 1
    assert KEY not in str(ei.value)
    assert "401" in str(ei.value)


async def test_formato_recusado_converte_via_ffmpeg_e_retenta(monkeypatch):
    names: list[str] = []

    def handler(request):
        names.append(
            request.content.decode("latin-1").split('filename="', 1)[1].split('"', 1)[0]
        )
        if len(names) == 1:
            return _err(400, "Invalid file format. Supported formats: ['flac', 'mp3']")
        return httpx.Response(200, json={"text": "convertido"})

    async def fake_convert(audio: bytes) -> bytes:
        assert audio == b"raw"
        return b"MP3DATA"

    monkeypatch.setattr(voice, "_to_mp3", fake_convert)
    async with _client(handler) as c:
        out = await transcribe(b"raw", "voice.oga", KEY, c)
    assert out == "convertido"
    assert names == ["voice.oga", "audio.mp3"]


async def test_formato_recusado_e_ffmpeg_falha(monkeypatch):
    async def boom(audio: bytes) -> bytes:
        raise VoiceError("ffmpeg indisponível")

    monkeypatch.setattr(voice, "_to_mp3", boom)

    def handler(request):
        return _err(400, "Invalid file format.")

    async with _client(handler) as c:
        with pytest.raises(VoiceError, match="ffmpeg"):
            await transcribe(b"raw", "voice.oga", KEY, c)


async def test_5xx_levanta():
    def handler(request):
        return httpx.Response(503, text="unavailable")

    async with _client(handler) as c:
        with pytest.raises(VoiceError, match="503"):
            await transcribe(b"x", "a.ogg", KEY, c)


async def test_erro_de_rede_levanta_sem_key():
    def handler(request):
        raise httpx.ConnectError("sem rede", request=request)

    async with _client(handler) as c:
        with pytest.raises(VoiceError) as ei:
            await transcribe(b"x", "a.ogg", KEY, c)
    assert KEY not in str(ei.value)


@pytest.mark.parametrize(
    "audio,key",
    [(b"", KEY), (b"x", ""), (b"x", "   ")],
)
async def test_valida_entrada(audio, key):
    def handler(request):  # pragma: no cover - não deve ser chamado
        raise AssertionError("rede não deveria ser chamada")

    async with _client(handler) as c:
        with pytest.raises(VoiceError):
            await transcribe(audio, "a.ogg", key, c)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"text": "   "}),
        httpx.Response(200, json={}),
        httpx.Response(200, text="não é json"),
        httpx.Response(200, json=["lista"]),
    ],
    ids=["vazio", "sem-text", "nao-json", "nao-objeto"],
)
async def test_resposta_invalida_levanta(response):
    def handler(request):
        return response

    async with _client(handler) as c:
        with pytest.raises(VoiceError):
            await transcribe(b"x", "a.ogg", KEY, c)


async def test_filename_sanitizado():
    seen = []

    def handler(request):
        seen.append(request.content.decode("latin-1"))
        return httpx.Response(200, json={"text": "ok"})

    async with _client(handler) as c:
        await transcribe(b"x", '../../etc/pa"ss\r\nwd.oga', KEY, c)
    body = seen[0]
    assert "../" not in body.split("filename=", 1)[1].split("\r\n", 1)[0]
    assert body.count("\r\nContent-Disposition") == 3  # file, model, language


async def test_403_modelo_sem_acesso_cai_para_whisper():
    models: list[str] = []

    def handler(request):
        models.append(_field(request, "model"))
        if len(models) == 1:
            return _err(
                403,
                "Project does not have access to model `gpt-4o-mini-transcribe`",
                "model_not_found",
            )
        return httpx.Response(200, json={"text": "via whisper"})

    async with _client(handler) as c:
        out = await transcribe(b"x", "a.ogg", KEY, c)
    assert out == "via whisper"
    assert models == ["gpt-4o-mini-transcribe", "whisper-1"]


async def test_403_invalid_api_key_nao_retenta():
    calls = []

    def handler(request):
        calls.append(1)
        return _err(403, "Incorrect API key for this model endpoint", "invalid_api_key")

    async with _client(handler) as c:
        with pytest.raises(VoiceError, match="403"):
            await transcribe(b"x", "a.ogg", KEY, c)
    assert len(calls) == 1


async def test_429_levanta_sem_retry():
    calls = []

    def handler(request):
        calls.append(1)
        return _err(429, "Rate limit reached", "rate_limit_exceeded")

    async with _client(handler) as c:
        with pytest.raises(VoiceError, match="429"):
            await transcribe(b"x", "a.ogg", KEY, c)
    assert len(calls) == 1


async def test_read_timeout_levanta_sem_key():
    def handler(request):
        raise httpx.ReadTimeout("lento", request=request)

    async with _client(handler) as c:
        with pytest.raises(VoiceError, match="ReadTimeout") as ei:
            await transcribe(b"x", "a.ogg", KEY, c)
    assert KEY not in str(ei.value)


async def test_key_com_espacos_vai_stripada_no_header():
    seen = []

    def handler(request):
        seen.append(request.headers["authorization"])
        return httpx.Response(200, json={"text": "ok"})

    async with _client(handler) as c:
        await transcribe(b"x", "a.ogg", f"  {KEY}\n", c)
    assert seen == [f"Bearer {KEY}"]


async def test_ffmpeg_ausente(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("PATH", str(tmp_path))  # diretório sem ffmpeg
    with pytest.raises(VoiceError, match="indisponível"):
        await voice._to_mp3(b"raw")


async def test_ffmpeg_timeout_mata_o_filho(monkeypatch, tmp_path: Path):
    fake = tmp_path / "ffmpeg"
    fake.write_text("#!/bin/sh\nexec sleep 37\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    monkeypatch.setattr(voice, "FFMPEG_TIMEOUT_S", 0.3)
    with pytest.raises(VoiceError, match="excedeu"):
        await voice._to_mp3(b"raw")
    # o `sleep` exec'd era o próprio filho: não pode sobrar nenhum
    assert not _sleep_vivo("37")


def _sleep_vivo(secs: str) -> bool:
    cmd = ["pgrep", "-f", f"^sleep {secs}$"]
    return subprocess.run(cmd, capture_output=True, check=False).returncode == 0  # noqa: S603


async def test_ffmpeg_cancelado_nao_deixa_filho(monkeypatch, tmp_path: Path):
    import asyncio

    fake = tmp_path / "ffmpeg"
    fake.write_text("#!/bin/sh\nexec sleep 38\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    task = asyncio.create_task(voice._to_mp3(b"raw"))
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not _sleep_vivo("38")


needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg ausente"
)


def _ffmpeg_make(path: Path, *extra: str) -> Path:
    cmd = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=2",
        *extra,
        str(path),
    ]
    subprocess.run(cmd, check=True)  # noqa: S603 - argumentos fixos de teste
    return path


@needs_ffmpeg
async def test_to_mp3_real_ffmpeg(tmp_path: Path):
    wav = _ffmpeg_make(tmp_path / "s.wav")
    out = await voice._to_mp3(wav.read_bytes())
    assert out[:3] == b"ID3" or out[0] == 0xFF
    with pytest.raises(VoiceError):
        await voice._to_mp3(b"lixo nao-audio")


async def _live_transcribe(path: Path) -> None:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        pytest.skip("OPENAI_API_KEY ausente")
    async with httpx.AsyncClient() as c:
        try:
            out = await transcribe(path.read_bytes(), path.name, key, c)
        except VoiceError as e:
            # áudio sem fala pode vir vazio; auth/rede/formato inválidos não.
            assert "401" not in str(e) and "403" not in str(e), str(e)
            assert "transcrição vazia" in str(e), str(e)
        else:
            assert isinstance(out, str)


@pytest.mark.live
@needs_ffmpeg
async def test_live_wav_sem_erro_de_auth(tmp_path: Path):
    await _live_transcribe(_ffmpeg_make(tmp_path / "sine.wav"))


@pytest.mark.live
@needs_ffmpeg
async def test_live_oga_opus_como_o_telegram(tmp_path: Path):
    await _live_transcribe(
        _ffmpeg_make(tmp_path / "voice.oga", "-c:a", "libopus", "-f", "ogg")
    )
