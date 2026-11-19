"""Backend do Mini App (127.0.0.1, atrás do túnel): HTML estático + /api/* autenticado por initData do Telegram.

Parser HTTP mínimo sobre asyncio, no mesmo estilo de bot/localapi.py (sem dependência nova). Nada além de
`GET /` é servido sem initData válido (HMAC oficial + dono + auth_date <= 24 h).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import inspect
import ipaddress
import json
import logging
import math
import os
import re
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlsplit

log = logging.getLogger(__name__)

DEFAULT_PORT = 28766
INDEX_PATH = Path(__file__).resolve().parent.parent / "webapp" / "index.html"

SINGLE_HEADERS = {"content-length", "transfer-encoding", "x-tg-init-data", "host"}
MAX_HEADER_BYTES = 16 * 1024
MAX_BODY_BYTES = 64 * 1024
REQUEST_TIMEOUT_S = 5.0
DRAIN_TIMEOUT_S = 0.3
MAX_CONNECTIONS = 64
AUTH_MAX_AGE_S = 24 * 3600
AUTH_FUTURE_SKEW_S = 60
RATE_LIMIT = 120
RATE_WINDOW_S = 60.0
RATE_MAX_KEYS = 4096
HISTORY_DEFAULT_LIMIT = 30
HISTORY_MAX_LIMIT = 100
MAX_FIELD_CHARS = 4096
MAX_ACCOUNT_CHARS = 64
SESSION_SCOPES = ("open", "recent")
LAST_PROMPT_CHARS = 300
LAST_REPLY_CHARS = 800

REASONS = {
    200: "OK",
    400: "Bad Request",
    401: "Unauthorized",
    404: "Not Found",
    405: "Method Not Allowed",
    411: "Length Required",
    413: "Payload Too Large",
    423: "Locked",
    429: "Too Many Requests",
    500: "Internal Server Error",
    503: "Service Unavailable",
}

# index.html é arquivo único com JS/CSS inline (C5) -> 'unsafe-inline'; o resto só da própria origem.
CSP = (
    "default-src 'self'; "
    "script-src 'self' https://telegram.org 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "object-src 'none'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors https://web.telegram.org https://*.telegram.org"
)
SECURITY_HEADERS = (
    f"Content-Security-Policy: {CSP}\r\n"
    "X-Content-Type-Options: nosniff\r\n"
    "Referrer-Policy: no-referrer\r\n"
    "Cache-Control: no-store\r\n"
)
JSON_TYPE = "application/json; charset=utf-8"
HTML_TYPE = "text/html; charset=utf-8"

_UUID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_UINT = re.compile(r"[0-9]{1,10}")


class Facade(Protocol):
    """Superfície que o Bot (C3) expõe; formatos de retorno = respostas JSON do C4."""

    state: Any  # precisa de .owner_id (int | None)

    def is_locked(self) -> bool: ...
    def api_state(self) -> dict: ...
    async def api_accounts(self) -> list[dict]: ...
    async def select_account(self, name: str) -> str | None: ...
    def api_projects(self) -> list[dict]: ...
    def select_project(self, path: str) -> str | None: ...
    def api_sessions(self, scope: str = "recent") -> list[dict]: ...
    def select_session(self, sid: str) -> str | None: ...
    def new_session(
        self, project: str | None = None, account: str | None = None
    ) -> str: ...
    def api_history(self, sid: str, limit: int, before: int | None) -> dict: ...
    def run_feed(self, sid: str, after_seq: int) -> dict: ...
    async def end_session(self, session_id: str) -> dict: ...
    def use_session(self, session_id: str) -> dict: ...
    async def stop_active(self) -> bool: ...


# ---------- autenticação (initData) ----------


def validate_init_data(
    raw: str | None,
    bot_token: str,
    owner_id: int | None,
    now: float,
    max_age: int = AUTH_MAX_AGE_S,
) -> bool:
    """Valida `Telegram.WebApp.initData` conforme a doc oficial e confere dono e idade."""
    if not raw or owner_id is None:
        return False
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return False
    fields: dict[str, str] = {}
    for k, v in pairs:
        if k in fields:
            return False
        fields[k] = v
    received = fields.pop("hash", None)
    if not received:
        return False
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(received.encode("utf-8"), expected.encode()):
        return False
    return _owner_matches(fields.get("user"), owner_id) and _fresh(
        fields.get("auth_date"), now, max_age
    )


def _owner_matches(user_json: str | None, owner_id: int) -> bool:
    try:
        user = json.loads(user_json or "")
    except (ValueError, RecursionError):
        return False
    uid = user.get("id") if isinstance(user, dict) else None
    return isinstance(uid, int) and not isinstance(uid, bool) and uid == owner_id


def _fresh(auth_date: str | None, now: float, max_age: int) -> bool:
    if not auth_date or not auth_date.isascii() or not auth_date.isdigit():
        return False
    age = now - int(auth_date)
    return -AUTH_FUTURE_SKEW_S <= age <= max_age


# ---------- rate limit ----------


class RateLimiter:
    """Janela deslizante por chave (IP). Tabela limitada: poda chaves ociosas."""

    def __init__(
        self,
        limit: int = RATE_LIMIT,
        window_s: float = RATE_WINDOW_S,
        max_keys: int = RATE_MAX_KEYS,
    ) -> None:
        self.limit = limit
        self.window_s = window_s
        self.max_keys = max_keys
        self._hits: dict[str, deque[float]] = {}
        self._pruned_at = float("-inf")

    def retry_after(self, key: str, now: float) -> int:
        """Segundos (>= 1) até a requisição mais antiga da janela expirar."""
        q = self._hits.get(key)
        if not q:
            return 1
        return max(1, math.ceil(q[0] + self.window_s - now))

    def allow(self, key: str, now: float) -> bool:
        if key not in self._hits and len(self._hits) >= self.max_keys:
            self._prune(now)
        q = self._hits.setdefault(key, deque())
        while q and q[0] <= now - self.window_s:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True

    def _prune(self, now: float) -> None:
        if (
            now - self._pruned_at >= 1
        ):  # varredura O(n) no máx. 1x/s (chaves novas em massa)
            self._pruned_at = now
            for k in [
                k
                for k, q in self._hits.items()
                if not q or q[-1] <= now - self.window_s
            ]:
                del self._hits[k]
        while len(self._hits) >= self.max_keys:  # tudo ativo: descarta o mais antigo
            del self._hits[next(iter(self._hits))]


# ---------- servidor ----------


class HttpError(Exception):
    def __init__(self, status: int, error: str | None = None) -> None:
        self.status = status
        self.error = error


Reply = tuple[int, str, bytes] | tuple[int, str, bytes, str]  # 4º: headers extras


def _json(status: int, obj: Any) -> Reply:
    return status, JSON_TYPE, json.dumps(obj).encode()


def _bad(msg: str) -> Reply:
    return _json(400, {"ok": False, "error": msg})


def _port(raw: str | None) -> int:
    raw = os.environ.get("WEBAPP_PORT") if raw is None else raw
    if not raw:
        return DEFAULT_PORT
    try:
        port = int(raw)
    except ValueError:
        raise ValueError("WEBAPP_PORT inválida") from None
    if not 1 <= port <= 65535:
        raise ValueError("WEBAPP_PORT inválida")
    return port


async def _call(fn: Callable[..., Any], *args: Any) -> Any:
    res = fn(*args)
    return await res if inspect.isawaitable(res) else res


async def _call_kw(fn: Callable[..., Any], **kwargs: Any) -> Any:
    res = fn(**kwargs)
    return await res if inspect.isawaitable(res) else res


def _accepts(fn: Callable[..., Any], *names: str) -> bool:
    """A facade aceita todos esses kwargs? (facade antiga não; assinatura ilegível -> tenta)."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return True
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return True
    return all(n in params for n in names)


def _text_field(data: dict, key: str, limit: int) -> str | None:
    v = data.get(key)
    if not isinstance(v, str) or not v.strip() or len(v) > limit or "\x00" in v:
        return None
    return v


def _query(target: str) -> tuple[str, dict[str, str]]:
    parts = urlsplit(target)
    q: dict[str, str] = {}
    for k, v in parse_qsl(parts.query, keep_blank_values=True):
        q.setdefault(k, v)
    return parts.path, q


def _uint(q: dict[str, str], key: str, default: int | None) -> int | None | str:
    """Inteiro >= 0 da query; ausente/vazio -> default; inválido -> 'erro'."""
    v = q.get(key)
    if v is None or v == "":
        return default
    return int(v) if _UINT.fullmatch(v) else "erro"


def _session_id(v: Any) -> str | None:
    return v.lower() if isinstance(v, str) and _UUID.fullmatch(v) else None


class WebApp:
    def __init__(
        self,
        facade: Facade,
        bot_token: str,
        port: int,
        host: str = "127.0.0.1",
        now: Callable[[], float] = time.time,
    ) -> None:
        self._facade = facade
        self._token = bot_token
        self._host = host
        self._now = now
        self.port = port
        self._limiter = RateLimiter()
        self._server: asyncio.Server | None = None
        self._conns: set[asyncio.Task] = set()
        self._index_cache: tuple[tuple, bytes] | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._conn, self._host, self.port, limit=MAX_HEADER_BYTES
        )
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
        for t in list(self._conns):
            t.cancel()
        if self._conns:
            await asyncio.gather(*self._conns, return_exceptions=True)
        if server is not None:
            await server.wait_closed()

    # -- conexão --

    async def _conn(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        if len(self._conns) >= MAX_CONNECTIONS:
            writer.close()
            return
        self._conns.add(task)
        try:
            peer = writer.get_extra_info("peername")
            reply = await self._serve(reader, peer[0] if peer else "?")
            await self._respond(writer, reply)
            if reply[0] != 200:
                # evita RST: o cliente ainda pode estar enviando o corpo; FIN antes libera o cliente já
                with contextlib.suppress(Exception):
                    writer.write_eof()
                await self._drain(reader)
        except Exception as e:  # noqa: BLE001 - conexão ruim nunca derruba o bot
            log.warning("webapp: erro de conexão (%s)", type(e).__name__)
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
    async def _respond(writer: asyncio.StreamWriter, reply: Reply) -> None:
        status, ctype, body, *extra = reply
        head = (
            f"HTTP/1.1 {status} {REASONS.get(status, 'Error')}\r\n"
            f"Content-Type: {ctype}\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"{SECURITY_HEADERS}{extra[0] if extra else ''}"
            "Connection: close\r\n\r\n"
        )
        writer.write(head.encode() + body)
        await writer.drain()

    async def _serve(self, reader: asyncio.StreamReader, peer_ip: str) -> Reply:
        try:
            method, target, headers = await asyncio.wait_for(
                self._read_head(reader), REQUEST_TIMEOUT_S
            )
            path, query = _query(target)
            if path != "/" and not path.startswith("/api/"):
                return 404, JSON_TYPE, b""
            ip, now = self._client_ip(headers, peer_ip), self._now()
            if not self._limiter.allow(ip, now):
                wait = self._limiter.retry_after(ip, now)
                return 429, JSON_TYPE, b"", f"Retry-After: {wait}\r\n"
            if path == "/":
                return self._index() if method == "GET" else (405, JSON_TYPE, b"")
            if not self._authorized(headers.get("x-tg-init-data")):
                return 401, JSON_TYPE, b""
            return await self._route(reader, method, path, query, headers)
        except HttpError as e:
            return (
                _json(e.status, {"ok": False, "error": e.error})
                if e.error
                else (e.status, JSON_TYPE, b"")
            )
        except (TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            return 400, JSON_TYPE, b""
        except Exception as e:  # noqa: BLE001 - detalhe interno nunca vai ao cliente
            log.warning("webapp: erro no handler (%s)", type(e).__name__)
            return _json(500, {"ok": False, "error": "erro interno"})

    @staticmethod
    def _client_ip(headers: dict, peer_ip: str) -> str:
        for raw in (headers.get("cf-connecting-ip", ""), peer_ip):
            try:
                ip = ipaddress.ip_address(raw)
            except ValueError:
                continue
            if (
                ip.version == 6
            ):  # um /64 é "um cliente" (senão o limite se contorna trocando o sufixo)
                return str(ipaddress.ip_network((ip, 64), strict=False))
            return str(ip)
        return "?"

    def _authorized(self, init_data: str | None) -> bool:
        owner = getattr(getattr(self._facade, "state", None), "owner_id", None)
        return validate_init_data(init_data, self._token, owner, self._now())

    def _index(self) -> Reply:
        """index.html em cache, revalidado por (mtime, tamanho) a cada hit (um stat)."""
        try:
            st = INDEX_PATH.stat()
            key = (INDEX_PATH, st.st_mtime_ns, st.st_size)
            if self._index_cache is None or self._index_cache[0] != key:
                self._index_cache = (key, INDEX_PATH.read_bytes())
            return 200, HTML_TYPE, self._index_cache[1]
        except OSError:
            return 404, JSON_TYPE, b""

    @staticmethod
    async def _read_head(reader: asyncio.StreamReader) -> tuple[str, str, dict]:
        raw = await reader.readuntil(b"\r\n\r\n")
        lines = raw.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
            raise HttpError(400)
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if not line:
                continue
            name, sep, value = line.partition(":")
            name = name.strip().lower()
            if not sep or not name or name in headers and name in SINGLE_HEADERS:
                raise HttpError(400)
            headers[name] = value.strip()
        return parts[0], parts[1], headers

    @staticmethod
    async def _read_body(reader: asyncio.StreamReader, headers: dict) -> dict:
        if "transfer-encoding" in headers:
            raise HttpError(411)
        raw = headers.get("content-length")
        if raw is None or not raw.isascii() or not raw.isdigit():
            raise HttpError(411)
        n = int(raw)
        if n > MAX_BODY_BYTES:
            raise HttpError(413)
        body = await asyncio.wait_for(reader.readexactly(n), REQUEST_TIMEOUT_S)
        if not body:
            return {}
        try:
            data = json.loads(body.decode("utf-8"))
        except (ValueError, RecursionError):
            raise HttpError(400) from None
        if not isinstance(data, dict):
            raise HttpError(400)
        return data

    # -- rotas --

    async def _route(
        self,
        reader: asyncio.StreamReader,
        method: str,
        path: str,
        query: dict,
        headers: dict,
    ) -> Reply:
        # trava TOTP: trancado, só GET /api/state responde (para o front mostrar a tela de desbloqueio)
        is_locked = getattr(self._facade, "is_locked", None)
        if is_locked is None:  # falha fechada: sem a trava não há como saber
            return _json(503, {"error": "indisponivel"})
        if await _call(is_locked) and not (method == "GET" and path == "/api/state"):
            return _json(423, {"error": "locked"})
        gets: dict[str, Callable[[dict], Any]] = {
            "/api/state": lambda q: self._state(),
            "/api/accounts": lambda q: self._accounts(),
            "/api/projects": lambda q: self._projects(),
            "/api/sessions": self._sessions,
            "/api/history": self._history,
            "/api/run": self._run,
        }
        posts: dict[str, Callable[[dict], Any]] = {
            "/api/account": self._post_account,
            "/api/project": self._post_project,
            "/api/session": self._post_session,
            "/api/session/end": self._post_session_end,
            "/api/session/use": self._post_session_use,
            "/api/stop": self._post_stop,
        }
        table = gets if method == "GET" else posts if method == "POST" else {}
        if path not in gets and path not in posts:
            return 404, JSON_TYPE, b""
        handler = table.get(path)
        if handler is None:
            return 405, JSON_TYPE, b""
        arg = query if method == "GET" else await self._read_body(reader, headers)
        return await handler(arg)

    async def _state(self) -> Reply:
        state = await _call(self._facade.api_state)
        return _json(
            200, {**state, "locked": bool(await _call(self._facade.is_locked))}
        )

    async def _accounts(self) -> Reply:
        return _json(200, await _call(self._facade.api_accounts))

    async def _projects(self) -> Reply:
        return _json(200, await _call(self._facade.api_projects))

    async def _sessions(self, q: dict) -> Reply:
        scope = q.get("scope", "recent")
        if scope not in SESSION_SCOPES:
            return _bad("scope inválido")
        fn = self._facade.api_sessions
        if _accepts(fn, "scope"):
            try:
                return _json(200, await _call_kw(fn, scope=scope))
            except TypeError:  # assinatura ilegível e facade sem `scope`
                if scope != "recent":
                    return _json(400, {"error": "unsupported"})
        elif scope != "recent":
            return _json(400, {"error": "unsupported"})
        return _json(200, await _call(fn))

    async def _history(self, q: dict) -> Reply:
        sid = _session_id(q.get("session_id"))
        limit = _uint(q, "limit", HISTORY_DEFAULT_LIMIT)
        before = _uint(q, "before", None)
        if sid is None or limit == "erro" or before == "erro":
            return _bad("parâmetros inválidos")
        if not 1 <= limit <= HISTORY_MAX_LIMIT:
            return _bad("parâmetros inválidos")
        return _json(200, await _call(self._facade.api_history, sid, limit, before))

    async def _run(self, q: dict) -> Reply:
        sid = _session_id(q.get("session_id"))
        after = _uint(q, "after", 0)
        if sid is None or after == "erro":
            return _bad("parâmetros inválidos")
        return _json(200, await _call(self._facade.run_feed, sid, after))

    @staticmethod
    def _result(err: str | None) -> Reply:
        return _bad(err) if err else _json(200, {"ok": True})

    async def _post_account(self, data: dict) -> Reply:
        name = _text_field(data, "name", MAX_FIELD_CHARS)
        if name is None:
            return _bad("name inválido")
        return self._result(await _call(self._facade.select_account, name))

    async def _post_project(self, data: dict) -> Reply:
        path = _text_field(data, "path", MAX_FIELD_CHARS)
        if path is None:
            return _bad("path inválido")
        return self._result(await _call(self._facade.select_project, path))

    async def _post_session(self, data: dict) -> Reply:
        if data.get("new") is True:
            return await self._new_session(data)
        sid = _session_id(data.get("session_id"))
        if sid is None:
            return _bad("session_id inválido")
        err = await _call(self._facade.select_session, sid)
        if err:
            return _bad(err)
        last_prompt, last_reply = await self._last_exchange(sid)
        return _json(
            200,
            {"session_id": sid, "last_prompt": last_prompt, "last_reply": last_reply},
        )

    async def _new_session(self, data: dict) -> Reply:
        kwargs: dict[str, str] = {}
        for key, limit in (
            ("project", MAX_FIELD_CHARS),
            ("account", MAX_ACCOUNT_CHARS),
        ):
            if data.get(key) is None:
                continue
            v = _text_field(data, key, limit)
            if v is None:
                return _bad(f"{key} inválido")
            kwargs[key] = v
        fn = self._facade.new_session
        if kwargs and not _accepts(fn, *kwargs):
            return _bad("unsupported")
        res = await (_call_kw(fn, **kwargs) if kwargs else _call(fn))
        if isinstance(res, dict):  # facade nova pode devolver {session_id} ou {error}
            err, sid = res.get("error"), _session_id(res.get("session_id"))
            if err or sid is None:
                return _bad(str(err or "sessão inválida"))
        else:  # str: uuid = sessão criada; qualquer outra string = erro
            sid = _session_id(res)
            if sid is None:
                return _bad(res if isinstance(res, str) and res else "sessão inválida")
        return _json(200, {"session_id": sid, "last_prompt": None, "last_reply": None})

    async def _last_exchange(self, sid: str) -> tuple[str | None, str | None]:
        """Último prompt/resposta, derivados do histórico (a facade C3 não expõe last_exchange)."""
        try:
            hist = await _call(self._facade.api_history, sid, 20, None)
            turns = [t for t in hist.get("turns", []) if isinstance(t, dict)]
        except Exception as e:  # noqa: BLE001 - a troca de sessão já aconteceu; resumo é opcional
            log.warning("webapp: resumo da sessão indisponível (%s)", type(e).__name__)
            return None, None

        def last(role: str, limit: int) -> str | None:
            for t in reversed(turns):
                if t.get("role") == role and isinstance(t.get("text"), str):
                    return t["text"][:limit]
            return None

        return last("user", LAST_PROMPT_CHARS), last("assistant", LAST_REPLY_CHARS)

    async def _session_op(self, name: str, data: dict) -> Reply:
        """end/use de uma sessão por id; facade sem o método -> 503 (falha fechada)."""
        sid = _session_id(data.get("session_id"))
        if sid is None:
            return _bad("session_id inválido")
        fn = getattr(self._facade, name, None)
        if fn is None:
            return _json(503, {"error": "indisponivel"})
        res = await _call(fn, sid)
        if isinstance(res, dict):  # só chaves conhecidas vão ao cliente
            return _json(
                200, {k: res[k] for k in ("ok", "outcome", "error") if k in res}
            )
        return self._result(res if isinstance(res, str) else None)

    async def _post_session_end(self, data: dict) -> Reply:
        return await self._session_op("end_session", data)

    async def _post_session_use(self, data: dict) -> Reply:
        return await self._session_op("use_session", data)

    async def _post_stop(self, data: dict) -> Reply:
        return _json(200, {"ok": bool(await _call(self._facade.stop_active))})


async def start(
    facade: Facade,
    cfg: Any,
    *,
    host: str = "127.0.0.1",
    port: int | None = None,
    now: Callable[[], float] = time.time,
) -> WebApp:
    """Sobe o servidor. Porta: argumento > env WEBAPP_PORT > 28766 (0 = efêmera, p/ teste)."""
    app = WebApp(
        facade, cfg.bot_token, _port(None) if port is None else port, host, now
    )
    await app.start()
    return app


async def stop(server: WebApp) -> None:
    await server.stop()
