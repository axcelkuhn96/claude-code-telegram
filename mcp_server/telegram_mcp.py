#!/usr/bin/env python3
"""Servidor MCP stdio mínimo (stdlib only): permite a uma sessão do Claude falar com o dono via bot Telegram.

Transporte: JSON-RPC 2.0, uma mensagem JSON por linha em stdin/stdout (stderr livre p/ logs).
O bot local escuta em 127.0.0.1:$BOT_LOCAL_PORT (/notify e /ask) e exige o header X-Bot-Secret.
Entrada malformada nunca derruba o servidor.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import stat
import sys
import threading
from pathlib import Path
from typing import Any

SUPPORTED_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
DEFAULT_VERSION = "2025-06-18"
DEFAULT_PORT = 28765  # igual a bot/config.py:DEFAULT_PORT
DEFAULT_TIMEOUT_MIN = 30
MAX_TEXT = 4000
MAX_OPCOES = 8
MAX_OPCAO = 60
MAX_TIMEOUT_MIN = 240
NOTIFY_HTTP_TIMEOUT = 30
ASK_HTTP_MARGIN = 30
MAX_RESPONSE_BYTES = 1_000_000
MAX_LEGENDA = 1000
MAX_FILE_BYTES = 50 * 1024 * 1024

TOOLS = [
    {
        "name": "telegram_notificar",
        "description": (
            "Envia uma mensagem ao dono no Telegram. Use SOMENTE quando precisar chamar a atenção dele "
            "(algo travou, exige uma ação dele) ou para avisar que terminou uma tarefa longa. "
            "Não use para progresso intermediário nem para conversa casual. Não espera resposta; "
            "retorna 'enviado' ou 'bot offline'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "texto": {
                    "type": "string",
                    "description": "Mensagem curta e objetiva, em português.",
                },
            },
            "required": ["texto"],
        },
    },
    {
        "name": "telegram_perguntar",
        "description": (
            "Faz uma pergunta ao dono no Telegram e AGUARDA a resposta dele (botões ou texto/voz). "
            "Use SOMENTE quando realmente precisar da decisão dele para continuar e não puder decidir "
            "sozinho com segurança (ação destrutiva, escolha de design ambígua, credencial). "
            "Bloqueia até a resposta ou o timeout; retorna o texto escolhido, 'sem resposta em N min' "
            "ou 'bot offline'. Se houver 'bot offline', siga com o melhor julgamento."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pergunta": {
                    "type": "string",
                    "description": "Pergunta clara, com o contexto mínimo.",
                },
                "opcoes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Respostas possíveis, viram botões (até 8). Opcional.",
                },
                "timeout_min": {
                    "type": "integer",
                    "description": "Minutos de espera pela resposta (1 a 240). Padrão 30.",
                },
            },
            "required": ["pergunta"],
        },
    },
    {
        "name": "telegram_enviar_arquivo",
        "description": (
            "Envia um arquivo ao dono no Telegram (como documento). Use quando ele pedir um arquivo "
            "ou quando um artefato (relatório, print, log, planilha) for o resultado útil da tarefa e "
            "ele precisar abri-lo no celular. Não use para texto curto (use telegram_notificar) nem "
            "para arquivos sensíveis (credenciais, chaves, .env). O caminho deve ser absoluto, de um "
            "arquivo regular de até 50 MB. Retorna 'enviado', 'bot offline' ou 'erro: <motivo>'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "caminho": {
                    "type": "string",
                    "description": "Caminho absoluto do arquivo.",
                },
                "legenda": {
                    "type": "string",
                    "description": "Legenda opcional (até 1000 caracteres).",
                },
            },
            "required": ["caminho"],
        },
    },
]

_stdout_lock = threading.Lock()


class ToolError(Exception):
    """Erro de execução da tool (vira resultado isError, não erro JSON-RPC)."""


class BotOffline(Exception):
    """Sem secret, conexão recusada/perdida: o bot não está no ar."""


class BotTimeout(Exception):
    """O bot aceitou a conexão mas não respondeu dentro do timeout HTTP."""


class BotError(Exception):
    """O bot respondeu algo inaceitável (status não-2xx, corpo inválido)."""


class Cancelled(Exception):
    """O cliente cancelou a requisição (notifications/cancelled)."""


class Pending:
    """Requisição tools/call em andamento; permite abortar o HTTP pendente ao cancelar."""

    def __init__(self) -> None:
        self.cancelled = False
        self.conn: http.client.HTTPConnection | None = None
        self._lock = threading.Lock()

    def attach(self, conn: http.client.HTTPConnection) -> None:
        with self._lock:
            self.conn = conn
            if self.cancelled:
                raise Cancelled

    def cancel(self) -> None:
        with self._lock:
            self.cancelled = True
            sock = self.conn.sock if self.conn is not None else None
        if sock is not None:
            try:
                sock.shutdown(
                    socket.SHUT_RDWR
                )  # acorda o recv bloqueado da thread da tool
            except OSError:
                pass


_pending: dict[Any, Pending] = {}
_pending_lock = threading.Lock()


def log(msg: str) -> None:
    print(f"telegram_mcp: {msg}", file=sys.stderr, flush=True)


def send(msg: dict[str, Any]) -> None:
    # ensure_ascii (padrão): stdout nunca depende do locale/encoding do processo
    line = json.dumps(msg, separators=(",", ":"))
    with _stdout_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def result_msg(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def error_msg(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def text_result(text: str, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


# ---------- configuração ----------


def state_dir() -> Path:
    env = os.environ.get("CLAUDE_TELEGRAM_STATE_DIR")
    return Path(env) if env else Path.home() / ".config" / "claude-telegram"


def read_secret() -> str | None:
    try:
        secret = (state_dir() / "local.secret").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return secret or None


def read_port() -> int:
    """Ordem: env BOT_LOCAL_PORT > <state_dir>/local.port (gravado pelo bot) > padrão."""
    raw = os.environ.get("BOT_LOCAL_PORT")
    if raw is None or raw == "":
        try:
            raw = (state_dir() / "local.port").read_text(encoding="utf-8").strip()
            if not (raw.isascii() and raw.isdigit() and 1 <= int(raw) <= 65535):
                raw = ""  # arquivo ilegível/corrompido: cai no padrão
        except (OSError, UnicodeDecodeError):
            raw = ""
        if raw == "":
            return DEFAULT_PORT
    try:
        port = int(raw)
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        raise ToolError("BOT_LOCAL_PORT inválida (esperado inteiro entre 1 e 65535)")
    return port


def origem() -> dict[str, Any]:
    return {
        "cwd": os.getcwd(),
        "session_id": os.environ.get("CLAUDE_SESSION_ID") or None,
    }


# ---------- validação ----------


def require_text(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"'{key}' é obrigatório (texto não vazio)")
    if len(value) > MAX_TEXT:
        raise ToolError(f"'{key}' excede {MAX_TEXT} caracteres")
    return value


def parse_opcoes(args: dict[str, Any]) -> list[str]:
    value = args.get("opcoes")
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_OPCOES:
        raise ToolError(f"'opcoes' deve ser lista de até {MAX_OPCOES} textos")
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item) > MAX_OPCAO:
            raise ToolError(f"cada opção deve ser texto de 1 a {MAX_OPCAO} caracteres")
    return value


def parse_timeout_min(args: dict[str, Any]) -> int:
    value = args.get("timeout_min", DEFAULT_TIMEOUT_MIN)
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_TIMEOUT_MIN
    ):
        raise ToolError(f"'timeout_min' deve ser inteiro entre 1 e {MAX_TIMEOUT_MIN}")
    return value


# ---------- HTTP para o bot ----------


def post_bot(
    path: str,
    payload: dict[str, Any],
    http_timeout: float,
    pending: Pending | None = None,
) -> dict[str, Any]:
    """POST no bot local (http.client: sem proxy, sem seguir redirect; o secret nunca vai a outro destino).

    Levanta BotOffline / BotTimeout / BotError / Cancelled / ToolError.
    """
    secret = read_secret()
    if secret is None:
        raise BotOffline
    port = read_port()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=http_timeout)
    try:
        if pending is not None:
            pending.attach(conn)
        conn.request(
            "POST",
            path,
            body=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Bot-Secret": secret},
        )
        resp = conn.getresponse()
        raw = resp.read(MAX_RESPONSE_BYTES + 1)
        status = resp.status
    except Cancelled:
        raise
    except ConnectionRefusedError as e:
        raise BotOffline from e
    except TimeoutError as e:  # socket.timeout é alias de TimeoutError (3.10+)
        if pending is not None and pending.cancelled:
            raise Cancelled from e
        raise BotTimeout from e
    except http.client.HTTPException as e:  # inclui RemoteDisconnected, BadStatusLine
        if pending is not None and pending.cancelled:
            raise Cancelled from e
        raise BotError("bot respondeu de forma inválida") from e
    except OSError as e:
        if pending is not None and pending.cancelled:
            raise Cancelled from e
        raise BotOffline from e
    finally:
        conn.close()
    if pending is not None and pending.cancelled:
        raise Cancelled
    if not 200 <= status < 300:
        log(f"bot respondeu HTTP {status} em {path}")
        raise BotError(f"bot respondeu erro {status}")
    if len(raw) > MAX_RESPONSE_BYTES:
        raise BotError("bot respondeu de forma inválida (corpo grande demais)")
    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError) as e:
        raise BotError("bot respondeu de forma inválida (corpo não é JSON)") from e
    if not isinstance(body, dict):
        raise BotError("bot respondeu de forma inválida (corpo não é objeto)")
    return body


# ---------- tools ----------


def tool_notificar(
    args: dict[str, Any], pending: Pending | None = None
) -> dict[str, Any]:
    texto = require_text(args, "texto")
    try:
        body = post_bot(
            "/notify",
            {"texto": texto, "origem": origem()},
            NOTIFY_HTTP_TIMEOUT,
            pending,
        )
    except BotOffline:
        return text_result("bot offline")
    except BotTimeout:
        return text_result(
            "o bot não respondeu a tempo; a mensagem pode não ter sido enviada"
        )
    if body.get("ok") is True:
        return text_result("enviado")
    return text_result("falha ao enviar (o bot recusou a mensagem)")


def tool_perguntar(
    args: dict[str, Any], pending: Pending | None = None
) -> dict[str, Any]:
    pergunta = require_text(args, "pergunta")
    opcoes = parse_opcoes(args)
    timeout_min = parse_timeout_min(args)
    timeout_s = timeout_min * 60
    payload = {
        "pergunta": pergunta,
        "opcoes": opcoes,
        "timeout_s": timeout_s,
        "origem": origem(),
    }
    try:
        body = post_bot("/ask", payload, timeout_s + ASK_HTTP_MARGIN, pending)
    except BotOffline:
        return text_result("bot offline")
    except BotTimeout:
        return text_result(f"sem resposta em {timeout_min} min")
    resposta = body.get("resposta")
    if body.get("ok") is True and isinstance(resposta, str):
        return text_result(resposta)
    return text_result(f"sem resposta em {timeout_min} min")


def parse_arquivo(args: dict[str, Any]) -> tuple[str, str]:
    """Validação no cliente (o bot revalida): caminho absoluto, arquivo regular, ≤ 50 MB; legenda ≤ 1000."""
    caminho = args.get("caminho")
    if not isinstance(caminho, str) or not caminho.strip() or "\x00" in caminho:
        raise ToolError("erro: 'caminho' é obrigatório (texto não vazio)")
    if not os.path.isabs(caminho):
        raise ToolError("erro: 'caminho' deve ser absoluto")
    try:
        st = os.stat(caminho)
    except OSError as e:
        raise ToolError("erro: arquivo inacessível ou inexistente") from e
    if not stat.S_ISREG(st.st_mode):
        raise ToolError("erro: 'caminho' não é um arquivo regular")
    if st.st_size > MAX_FILE_BYTES:
        raise ToolError("erro: arquivo maior que 50 MB")
    legenda = args.get("legenda")
    if legenda is None:
        legenda = ""
    if not isinstance(legenda, str) or len(legenda) > MAX_LEGENDA:
        raise ToolError(
            f"erro: 'legenda' deve ser texto de até {MAX_LEGENDA} caracteres"
        )
    return caminho, legenda


def tool_enviar_arquivo(
    args: dict[str, Any], pending: Pending | None = None
) -> dict[str, Any]:
    caminho, legenda = parse_arquivo(args)
    try:
        body = post_bot(
            "/send_file",
            {"caminho": caminho, "legenda": legenda, "origem": origem()},
            NOTIFY_HTTP_TIMEOUT,
            pending,
        )
    except BotOffline:
        return text_result("bot offline")
    except BotTimeout:
        return text_result(
            "o bot não respondeu a tempo; o arquivo pode não ter sido enviado"
        )
    if body.get("ok") is True:
        return text_result("enviado")
    erro = body.get("erro")
    return text_result(
        f"erro: {erro if isinstance(erro, str) and erro else 'o bot recusou o arquivo'}",
        is_error=True,
    )


HANDLERS = {
    "telegram_notificar": tool_notificar,
    "telegram_perguntar": tool_perguntar,
    "telegram_enviar_arquivo": tool_enviar_arquivo,
}


def run_tool(req_id: Any, name: str, args: dict[str, Any], pending: Pending) -> None:
    try:
        reply = result_msg(req_id, HANDLERS[name](args, pending))
    except Cancelled:
        return  # spec: requisição cancelada não recebe resposta
    except ToolError as e:
        reply = result_msg(req_id, text_result(str(e), is_error=True))
    except BotError as e:
        reply = result_msg(req_id, text_result(str(e), is_error=True))
    except Exception as e:  # noqa: BLE001 - nunca derrubar o servidor por erro de uma tool
        log(f"erro inesperado em {name}: {type(e).__name__}")
        reply = result_msg(
            req_id, text_result(f"erro interno: {type(e).__name__}", is_error=True)
        )
    finally:
        with _pending_lock:
            _pending.pop(req_id, None)
    if not pending.cancelled:
        send(reply)


# ---------- JSON-RPC ----------


def valid_id(req_id: Any) -> bool:
    return isinstance(req_id, (str, int)) and not isinstance(req_id, bool)


def handle(msg: Any) -> None:
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        send(error_msg(None, -32600, "Invalid Request"))
        return
    method = msg.get("method")
    has_id = "id" in msg
    req_id = msg.get("id")
    params = msg.get("params")
    if not isinstance(params, dict):
        params = {}

    if not isinstance(method, str):
        if has_id and msg.get("result") is None and msg.get("error") is None:
            send(error_msg(None, -32600, "Invalid Request"))
        return  # resposta do cliente (result/error): ignorar
    if not has_id:
        if method == "notifications/cancelled":
            cancel_request(params.get("requestId"))
        return  # demais notificações não têm resposta
    if not valid_id(req_id):
        send(error_msg(None, -32600, "Invalid Request: id deve ser string ou inteiro"))
        return

    if method == "initialize":
        asked = params.get("protocolVersion")
        version = (
            asked
            if isinstance(asked, str) and asked in SUPPORTED_VERSIONS
            else DEFAULT_VERSION
        )
        send(
            result_msg(
                req_id,
                {
                    "protocolVersion": version,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "telegram-bot", "version": "1.0.0"},
                },
            )
        )
    elif method == "ping":
        send(result_msg(req_id, {}))
    elif method == "tools/list":
        send(result_msg(req_id, {"tools": TOOLS}))
    elif method == "tools/call":
        name = params.get("name")
        args = params.get("arguments")
        if not isinstance(name, str) or name not in HANDLERS:
            send(error_msg(req_id, -32602, "Tool desconhecida"))
        elif args is not None and not isinstance(args, dict):
            send(error_msg(req_id, -32602, "arguments deve ser um objeto"))
        else:
            pending = Pending()
            with _pending_lock:
                _pending[req_id] = pending
            # thread: telegram_perguntar bloqueia por minutos e ping/tools/list precisam seguir respondendo
            threading.Thread(
                target=run_tool, args=(req_id, name, args or {}, pending), daemon=True
            ).start()
    else:
        send(error_msg(req_id, -32601, "Method not found"))


def cancel_request(request_id: Any) -> None:
    if not valid_id(request_id):
        return
    with _pending_lock:
        pending = _pending.get(request_id)
    if pending is not None:
        pending.cancel()


def process_line(raw: bytes | str) -> None:
    """Processa uma linha de entrada; nunca levanta (exceto SystemExit/KeyboardInterrupt)."""
    req_id: Any = None
    try:
        line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        line = line.strip()
        if not line:
            return
        try:
            msg = json.loads(line)
        except (ValueError, RecursionError):  # inclui JSON aninhado fundo demais
            send(error_msg(None, -32700, "Parse error"))
            return
        if isinstance(msg, dict) and valid_id(msg.get("id")):
            req_id = msg["id"]
        handle(msg)
    except Exception as e:  # noqa: BLE001 - entrada malformada nunca derruba o servidor
        log(f"erro ao processar mensagem: {type(e).__name__}")
        if req_id is not None:
            send(error_msg(req_id, -32603, "Internal error"))


def main() -> int:
    for raw in sys.stdin.buffer:
        process_line(raw)
    return 0


if __name__ == "__main__":
    sys.exit(main())
