"""API HTTP local (127.0.0.1) usada pelo MCP: /notify e /ask. Parser HTTP mínimo sobre asyncio."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import os
import secrets
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

MAX_HEADER_BYTES = 16 * 1024
MAX_BODY_BYTES = 64 * 1024
REQUEST_TIMEOUT_S = 10.0
DRAIN_TIMEOUT_S = 0.3
MAX_TIMEOUT_S = 240 * 60  # igual ao teto do MCP (timeout_min <= 240)
PORT_FILE = "local.port"
SECRET_FILE = "local.secret"  # noqa: S105 - nome de arquivo, não é segredo
REASONS = {
    200: "OK",
    400: "Bad Request",
    401: "Unauthorized",
    404: "Not Found",
    405: "Method Not Allowed",
    411: "Length Required",
    413: "Payload Too Large",
    500: "Internal Server Error",
}

NotifyFn = Callable[[str, dict], Awaitable[bool]]
AskFn = Callable[[str, list[str], float, dict], Awaitable[str | None]]
# (caminho, legenda, origem) -> (ok, erro seguro para devolver ao chamador)
SendFileFn = Callable[[str, str | None, dict], Awaitable[tuple[bool, str | None]]]
MAX_CAPTION = 1000


# ---------- segredo ----------


def new_secret() -> str:
    return secrets.token_urlsafe(32)


def check_secret(provided: str | None, expected: str) -> bool:
    if not provided:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def write_secret(state_dir: Path, secret: str) -> Path:
    """<state_dir>/local.secret, 0600, atômico; diretório 0700."""
    return _write_private(state_dir, SECRET_FILE, secret)


def write_port(state_dir: Path, port: int) -> Path:
    """<state_dir>/local.port (porta efetiva; o MCP lê quando BOT_LOCAL_PORT não está no env)."""
    return _write_private(state_dir, PORT_FILE, str(port))


def _write_private(state_dir: Path, name: str, content: str) -> Path:
    state_dir = Path(state_dir)
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if state_dir.stat().st_mode & 0o077:
        state_dir.chmod(0o700)
    path = state_dir / name
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path


# ---------- perguntas pendentes ----------


class _Ask:
    def __init__(
        self, opcoes: list[str], fut: asyncio.Future[str], question: str = ""
    ) -> None:
        self.opcoes = opcoes
        self.fut = fut
        self.question = question
        self.message_id: int | None = None


class Asks:
    """Perguntas aguardando o dono; resolvidas por botão (índice) ou reply à mensagem."""

    def __init__(self) -> None:
        self._by_id: dict[str, _Ask] = {}
        self._by_msg: dict[int, str] = {}
        self._text_target: str | None = (
            None  # ask que escolheu "Outro": o próximo texto livre
        )

    def create(
        self, opcoes: list[str], question: str = ""
    ) -> tuple[str, asyncio.Future[str]]:
        ask_id = secrets.token_hex(4)
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._by_id[ask_id] = _Ask(opcoes, fut, question)
        return ask_id, fut

    def _open_ids(self) -> list[str]:
        return [i for i, a in self._by_id.items() if not a.fut.done()]

    def has_open(self) -> bool:
        return bool(self._open_ids())

    def option_text(self, ask_id: str, idx: int) -> str | None:
        ask = self._by_id.get(ask_id)
        if ask is None or ask.fut.done() or not 0 <= idx < len(ask.opcoes):
            return None
        return ask.opcoes[idx]

    def question_of(self, ask_id: str) -> str:
        ask = self._by_id.get(ask_id)
        return ask.question if ask else ""

    def await_text(self, ask_id: str) -> bool:
        """ "Outro…": não resolve; o próximo texto livre responde ESTA pergunta."""
        ask = self._by_id.get(ask_id)
        if ask is None or ask.fut.done():
            return False
        self._text_target = ask_id
        return True

    def answer_free_text(self, text: str) -> str | None:
        """Texto livre do dono: responde a pergunta que pediu "Outro" ou, senão, a MAIS RECENTE
        ainda aberta. Devolve o texto da pergunta respondida (None se não há pergunta aberta)."""
        open_ids = self._open_ids()
        if not open_ids:
            return None
        target = self._text_target if self._text_target in open_ids else open_ids[-1]
        self._text_target = None
        self._resolve(target, text)
        return self._by_id[target].question

    def bind_message(self, ask_id: str, message_id: int) -> None:
        ask = self._by_id.get(ask_id)
        if ask is not None:
            ask.message_id = message_id
            self._by_msg[message_id] = ask_id

    def has_message(self, message_id: int) -> bool:
        return message_id in self._by_msg

    def pending(self) -> int:
        return len(self._by_id)

    def _resolve(self, ask_id: str | None, text: str) -> bool:
        ask = self._by_id.get(ask_id) if ask_id else None
        if ask is None or ask.fut.done():
            return False
        ask.fut.set_result(text)
        return True

    def answer_button(self, ask_id: str, idx: int) -> str | None:
        ask = self._by_id.get(ask_id)
        if ask is None or not 0 <= idx < len(ask.opcoes):
            return None
        return ask.opcoes[idx] if self._resolve(ask_id, ask.opcoes[idx]) else None

    def answer_reply(self, message_id: int, text: str) -> bool:
        return self._resolve(self._by_msg.get(message_id), text)

    def drop(self, ask_id: str) -> None:
        ask = self._by_id.pop(ask_id, None)
        if ask is not None and ask.message_id is not None:
            self._by_msg.pop(ask.message_id, None)


# ---------- servidor ----------


class HttpError(Exception):
    def __init__(self, status: int) -> None:
        self.status = status


async def _eof(reader: asyncio.StreamReader) -> None:
    while await reader.read(1024):
        pass


class LocalApi:
    def __init__(
        self,
        secret: str,
        on_notify: NotifyFn,
        on_ask: AskFn,
        port: int,
        host: str = "127.0.0.1",
        on_send_file: SendFileFn | None = None,
        on_restart_when_idle: Callable[[], Awaitable[dict]] | None = None,
    ) -> None:
        self._secret = secret
        self._on_notify = on_notify
        self._on_ask = on_ask
        self._on_send_file = on_send_file
        self._on_restart = on_restart_when_idle
        self._host = host
        self.port = port
        self._server: asyncio.Server | None = None
        self._conns: set[asyncio.Task] = set()

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._conn, self._host, self.port, limit=MAX_HEADER_BYTES
        )
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
        for t in list(self._conns):
            t.cancel()
        if self._conns:
            await asyncio.gather(*self._conns, return_exceptions=True)
        if self._server is not None:
            await self._server.wait_closed()

    async def _conn(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        self._conns.add(task)
        try:
            status, obj = await self._serve(reader)
            if status is not None:
                await self._respond(writer, status, obj)
                if status != 200:
                    await self._drain(
                        reader
                    )  # evita RST: o cliente ainda pode estar enviando o corpo
        except Exception as e:  # noqa: BLE001 - conexão ruim nunca derruba o bot
            log.warning("localapi: erro de conexão (%s)", type(e).__name__)
        finally:
            self._conns.discard(task)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    @staticmethod
    async def _drain(reader: asyncio.StreamReader) -> None:
        with contextlib.suppress(TimeoutError, ConnectionError):
            async with asyncio.timeout(DRAIN_TIMEOUT_S):
                while await reader.read(65536):
                    pass

    @staticmethod
    async def _respond(writer: asyncio.StreamWriter, status: int, obj: Any) -> None:
        body = json.dumps(obj if obj is not None else {"ok": False}).encode()
        head = (
            f"HTTP/1.1 {status} {REASONS.get(status, 'Error')}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
        )
        writer.write(head.encode() + body)
        await writer.drain()

    async def _serve(self, reader: asyncio.StreamReader) -> tuple[int | None, Any]:
        try:
            method, path, headers = await asyncio.wait_for(
                self._read_head(reader), REQUEST_TIMEOUT_S
            )
            if not check_secret(headers.get("x-bot-secret"), self._secret):
                return 401, {"ok": False}
            if method != "POST":
                return 405, {"ok": False}
            if (
                path not in ("/notify", "/ask", "/send_file", "/restart-when-idle")
                or (path == "/send_file" and self._on_send_file is None)
                or (path == "/restart-when-idle" and self._on_restart is None)
            ):
                return 404, {"ok": False}
            if "transfer-encoding" in headers:
                return 411, {"ok": False}
            body = await asyncio.wait_for(
                self._read_body(reader, headers), REQUEST_TIMEOUT_S
            )
            data = self._parse(body)
            if path == "/notify":
                return await self._notify(data)
            if path == "/send_file":
                return await self._send_file(data)
            if path == "/restart-when-idle":
                return 200, await self._on_restart()
            return await self._ask(reader, data)
        except HttpError as e:
            return e.status, {"ok": False}
        except (TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            return 400, {"ok": False}
        except Exception as e:  # noqa: BLE001 - detalhe interno nunca vai ao cliente
            log.warning("localapi: erro no handler (%s)", type(e).__name__)
            return 500, {"ok": False}

    @staticmethod
    async def _read_head(reader: asyncio.StreamReader) -> tuple[str, str, dict]:
        raw = await reader.readuntil(b"\r\n\r\n")
        lines = raw.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) != 3:
            raise HttpError(400)
        headers = {}
        for line in lines[1:]:
            name, sep, value = line.partition(":")
            if sep:
                headers[name.strip().lower()] = value.strip()
        return parts[0], parts[1], headers

    @staticmethod
    async def _read_body(reader: asyncio.StreamReader, headers: dict) -> bytes:
        raw = headers.get("content-length")
        if raw is None or not raw.isascii() or not raw.isdigit():
            raise HttpError(411)
        n = int(raw)
        if n > MAX_BODY_BYTES:
            raise HttpError(413)
        return await reader.readexactly(n)

    @staticmethod
    def _parse(body: bytes) -> dict:
        try:
            data = json.loads(body.decode("utf-8"))
        except (ValueError, RecursionError):
            raise HttpError(400) from None
        if not isinstance(data, dict):
            raise HttpError(400)
        return data

    @staticmethod
    def _origem(data: dict) -> dict:
        o = data.get("origem")
        return o if isinstance(o, dict) else {}

    @staticmethod
    def _text(data: dict, key: str) -> str:
        v = data.get(key)
        if not isinstance(v, str) or not v.strip():
            raise HttpError(400)
        return v

    async def _send_file(self, data: dict) -> tuple[int, Any]:
        caminho = self._text(data, "caminho")
        legenda = data.get("legenda")
        if legenda is not None and (
            not isinstance(legenda, str) or len(legenda) > MAX_CAPTION
        ):
            raise HttpError(400)
        ok, erro = await self._on_send_file(caminho, legenda, self._origem(data))
        return 200, {"ok": True} if ok else {"ok": False, "erro": erro or "falha"}

    async def _notify(self, data: dict) -> tuple[int, Any]:
        ok = await self._on_notify(self._text(data, "texto"), self._origem(data))
        return 200, {"ok": bool(ok)}

    async def _ask(
        self, reader: asyncio.StreamReader, data: dict
    ) -> tuple[int | None, Any]:
        pergunta = self._text(data, "pergunta")
        opcoes = data.get("opcoes", [])
        if not isinstance(opcoes, list) or not all(
            isinstance(o, str) and o for o in opcoes
        ):
            raise HttpError(400)
        timeout_s = data.get("timeout_s")
        if (
            isinstance(timeout_s, bool)
            or not isinstance(timeout_s, (int, float))
            or not 1 <= timeout_s <= MAX_TIMEOUT_S
        ):
            raise HttpError(400)
        work = asyncio.ensure_future(
            self._on_ask(pergunta, opcoes, timeout_s, self._origem(data))
        )
        gone = asyncio.ensure_future(_eof(reader))
        try:
            await asyncio.wait({work, gone}, return_when=asyncio.FIRST_COMPLETED)
            if not work.done():  # cliente caiu: solta a pergunta pendente, sem resposta
                return None, None
            resposta = work.result()
        finally:
            for t in (work, gone):
                t.cancel()
            await asyncio.gather(work, gone, return_exceptions=True)
        if resposta is None:
            return 200, {"ok": False}
        return 200, {"ok": True, "resposta": resposta}
