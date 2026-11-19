from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parent.parent / "mcp_server" / "telegram_mcp.py"
SECRET = "s3cr3t-test-value"


class FakeBot:
    """Servidor HTTP fake do bot, em thread, porta efêmera.

    `hold` (Event) segura a resposta até ser setado; `raw` substitui o corpo JSON;
    `headers` adiciona cabeçalhos (ex.: Location p/ redirect).
    """

    def __init__(self, status: int = 200, body: dict | None = None) -> None:
        self.requests: list[dict] = []
        self.status = status
        self.body = body if body is not None else {"ok": True}
        self.raw: bytes | None = None
        self.headers: dict[str, str] = {}
        self.hold: threading.Event | None = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                outer.requests.append(
                    {
                        "path": self.path,
                        "secret": self.headers.get("X-Bot-Secret"),
                        "body": json.loads(raw),
                    }
                )
                if outer.hold is not None:
                    outer.hold.wait(timeout=10)
                data = (
                    outer.raw
                    if outer.raw is not None
                    else json.dumps(outer.body).encode()
                )
                try:
                    self.send_response(outer.status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    for k, v in outer.headers.items():
                        self.send_header(k, v)
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass  # cliente cancelou/fechou a conexão

            def log_message(self, *args: object) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def wait_requests(self, n: int = 1, timeout: float = 5) -> None:
        end = time.monotonic() + timeout
        while len(self.requests) < n and time.monotonic() < end:
            time.sleep(0.02)
        assert len(self.requests) >= n

    def close(self) -> None:
        if self.hold is not None:
            self.hold.set()
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def bot():
    b = FakeBot()
    yield b
    b.close()


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    d = tmp_path / "state"
    d.mkdir()
    (d / "local.secret").write_text(SECRET + "\n")
    return d


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Client:
    def __init__(
        self, port: int, state_dir: Path, extra_env: dict | None = None
    ) -> None:
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_SESSION_ID",)}
        env["BOT_LOCAL_PORT"] = str(port)
        env["CLAUDE_TELEGRAM_STATE_DIR"] = str(state_dir)
        env.update(extra_env or {})
        self.proc = subprocess.Popen(  # noqa: S603 - executa o servidor MCP do próprio repo
            [sys.executable, str(SERVER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=str(state_dir),
        )
        self._id = 0

    def send_raw(self, line: str) -> None:
        assert self.proc.stdin
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def read(self) -> dict:
        assert self.proc.stdout
        line = self.proc.stdout.readline()
        assert line, "servidor fechou stdout: " + (
            self.proc.stderr.read() if self.proc.stderr else ""
        )
        return json.loads(line)

    def notify(self, method: str, params: dict | None = None) -> None:
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self.send_raw(json.dumps(msg))

    def request(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        msg: dict = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            msg["params"] = params
        self.send_raw(json.dumps(msg))
        resp = self.read()
        assert resp["id"] == self._id
        return resp

    def send_request(self, method: str, params: dict | None = None) -> int:
        """Envia sem esperar a resposta; devolve o id."""
        self._id += 1
        msg: dict = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            msg["params"] = params
        self.send_raw(json.dumps(msg))
        return self._id

    def send_bytes(self, data: bytes) -> None:
        assert self.proc.stdin
        self.proc.stdin.flush()
        self.proc.stdin.buffer.write(data)
        self.proc.stdin.buffer.flush()

    def call(self, name: str, arguments: dict) -> dict:
        return self.request("tools/call", {"name": name, "arguments": arguments})

    def close(self) -> int:
        assert self.proc.stdin
        self.proc.stdin.close()
        try:
            return self.proc.wait(timeout=5)
        finally:
            for f in (self.proc.stdout, self.proc.stderr):
                if f:
                    f.close()


@pytest.fixture
def make_client(state_dir):
    clients: list[Client] = []

    def _make(
        port: int, extra_env: dict | None = None, sdir: Path | None = None
    ) -> Client:
        c = Client(port, sdir or state_dir, extra_env)
        clients.append(c)
        return c

    yield _make
    for c in clients:
        if c.proc.poll() is None:
            c.proc.kill()
            c.proc.wait()
        for f in (c.proc.stdin, c.proc.stdout, c.proc.stderr):
            if f and not f.closed:
                f.close()


def text_of(resp: dict) -> str:
    result = resp["result"]
    assert result["content"][0]["type"] == "text"
    return result["content"][0]["text"]


# ---------- protocolo ----------


def test_initialize_ecoa_versao_suportada(bot, make_client):
    c = make_client(bot.port)
    r = c.request(
        "initialize",
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"},
        },
    )
    assert r["jsonrpc"] == "2.0"
    assert r["result"]["protocolVersion"] == "2025-03-26"
    assert "tools" in r["result"]["capabilities"]
    assert r["result"]["serverInfo"]["name"]


def test_initialize_versao_desconhecida_cai_para_padrao(bot, make_client):
    c = make_client(bot.port)
    r = c.request("initialize", {"protocolVersion": "1999-01-01"})
    assert r["result"]["protocolVersion"] == "2025-06-18"


def test_notificacao_initialized_nao_responde_e_ping_funciona(bot, make_client):
    c = make_client(bot.port)
    c.notify("notifications/initialized")
    r = c.request("ping")  # se initialized tivesse respondido, o id não bateria
    assert r["result"] == {}


def test_tools_list(bot, make_client):
    c = make_client(bot.port)
    tools = c.request("tools/list")["result"]["tools"]
    by_name = {t["name"]: t for t in tools}
    assert set(by_name) == {
        "telegram_notificar",
        "telegram_perguntar",
        "telegram_enviar_arquivo",
    }
    arq = by_name["telegram_enviar_arquivo"]["inputSchema"]
    assert arq["required"] == ["caminho"]
    assert set(arq["properties"]) == {"caminho", "legenda"}
    assert by_name["telegram_notificar"]["inputSchema"]["required"] == ["texto"]
    props = by_name["telegram_perguntar"]["inputSchema"]["properties"]
    assert set(props) == {"pergunta", "opcoes", "timeout_min"}
    for t in tools:
        assert t["inputSchema"]["type"] == "object"
        assert len(t["description"]) > 40  # diz QUANDO usar


def test_metodo_desconhecido(bot, make_client):
    c = make_client(bot.port)
    r = c.request("foo/bar")
    assert r["error"]["code"] == -32601


def test_notificacao_desconhecida_e_ignorada(bot, make_client):
    c = make_client(bot.port)
    c.notify("foo/bar")
    assert c.request("ping")["result"] == {}


def test_json_invalido(bot, make_client):
    c = make_client(bot.port)
    c.send_raw("{nao e json")
    r = c.read()
    assert r["error"]["code"] == -32700
    assert r["id"] is None
    assert c.request("ping")["result"] == {}


def test_tool_desconhecida(bot, make_client):
    c = make_client(bot.port)
    r = c.call("nao_existe", {})
    assert r["error"]["code"] == -32602


def test_eof_encerra_processo(bot, make_client):
    c = make_client(bot.port)
    assert c.close() == 0


# ---------- telegram_enviar_arquivo ----------


def test_enviar_arquivo_ok(bot, make_client, tmp_path, state_dir):
    f = tmp_path / "relatorio.txt"
    f.write_text("conteudo")
    c = make_client(bot.port, {"CLAUDE_SESSION_ID": "sid-9"})
    r = c.call("telegram_enviar_arquivo", {"caminho": str(f), "legenda": "o relatório"})
    assert text_of(r) == "enviado"
    assert r["result"]["isError"] is False
    (req,) = bot.requests
    assert req["path"] == "/send_file"
    assert req["secret"] == SECRET
    assert req["body"]["caminho"] == str(f)
    assert req["body"]["legenda"] == "o relatório"
    assert req["body"]["origem"] == {"cwd": str(state_dir), "session_id": "sid-9"}


def test_enviar_arquivo_sem_legenda(bot, make_client, tmp_path):
    f = tmp_path / "a.log"
    f.write_text("x")
    c = make_client(bot.port)
    assert text_of(c.call("telegram_enviar_arquivo", {"caminho": str(f)})) == "enviado"
    assert bot.requests[0]["body"]["legenda"] == ""


def test_enviar_arquivo_erro_do_bot(bot, make_client, tmp_path):
    f = tmp_path / "a.log"
    f.write_text("x")
    bot.body = {"ok": False, "erro": "caminho negado"}
    c = make_client(bot.port)
    r = c.call("telegram_enviar_arquivo", {"caminho": str(f)})
    assert text_of(r) == "erro: caminho negado"
    assert r["result"]["isError"] is True


def test_enviar_arquivo_erro_do_bot_sem_mensagem(bot, make_client, tmp_path):
    f = tmp_path / "a.log"
    f.write_text("x")
    bot.body = {"ok": False}
    c = make_client(bot.port)
    r = c.call("telegram_enviar_arquivo", {"caminho": str(f)})
    assert text_of(r).startswith("erro: ")
    assert r["result"]["isError"] is True


def test_enviar_arquivo_bot_offline(state_dir, make_client, tmp_path):
    f = tmp_path / "a.log"
    f.write_text("x")
    c = make_client(free_port())
    r = c.call("telegram_enviar_arquivo", {"caminho": str(f)})
    assert text_of(r) == "bot offline"
    assert r["result"]["isError"] is False


def test_enviar_arquivo_http_500(bot, make_client, tmp_path):
    f = tmp_path / "a.log"
    f.write_text("x")
    bot.status = 500
    c = make_client(bot.port)
    r = c.call("telegram_enviar_arquivo", {"caminho": str(f)})
    assert text_of(r) == "bot respondeu erro 500"
    assert r["result"]["isError"] is True


def test_enviar_arquivo_invalidos_nao_chamam_o_bot(bot, make_client, tmp_path):
    c = make_client(bot.port)
    d = tmp_path / "dir"
    d.mkdir()
    ok = tmp_path / "ok.txt"
    ok.write_text("x")
    casos = [
        {},
        {"caminho": ""},
        {"caminho": 5},
        {"caminho": ["/etc/hostname"]},
        {"caminho": "relativo/arquivo.txt"},
        {"caminho": "~/arquivo.txt"},
        {"caminho": str(tmp_path / "nao_existe.txt")},
        {"caminho": str(d)},  # diretório
        {"caminho": str(ok) + "\x00"},  # NUL
        {"caminho": str(ok), "legenda": 5},
        {"caminho": str(ok), "legenda": "x" * 1001},
    ]
    for args in casos:
        r = c.call("telegram_enviar_arquivo", args)
        assert r["result"]["isError"] is True, args
        assert text_of(r).startswith("erro:"), args
    assert bot.requests == []


def test_enviar_arquivo_maior_que_50mb(bot, make_client, tmp_path):
    grande = tmp_path / "grande.bin"
    with grande.open("wb") as fh:
        fh.truncate(50 * 1024 * 1024 + 1)  # esparso: não ocupa disco
    limite = tmp_path / "limite.bin"
    with limite.open("wb") as fh:
        fh.truncate(50 * 1024 * 1024)
    c = make_client(bot.port)
    r = c.call("telegram_enviar_arquivo", {"caminho": str(grande)})
    assert r["result"]["isError"] is True
    assert "50 MB" in text_of(r)
    assert bot.requests == []
    assert (
        text_of(c.call("telegram_enviar_arquivo", {"caminho": str(limite)}))
        == "enviado"
    )


def test_enviar_arquivo_legenda_1000_ok(bot, make_client, tmp_path):
    f = tmp_path / "a.log"
    f.write_text("x")
    c = make_client(bot.port)
    r = c.call("telegram_enviar_arquivo", {"caminho": str(f), "legenda": "y" * 1000})
    assert text_of(r) == "enviado"


# ---------- telegram_notificar ----------


def test_notificar_ok(bot, make_client, state_dir):
    c = make_client(bot.port)
    r = c.call("telegram_notificar", {"texto": "terminei a migração"})
    assert text_of(r) == "enviado"
    assert r["result"]["isError"] is False
    (req,) = bot.requests
    assert req["path"] == "/notify"
    assert req["secret"] == SECRET
    assert req["body"]["texto"] == "terminei a migração"
    assert req["body"]["origem"] == {"cwd": str(state_dir), "session_id": None}


def test_origem_propaga_session_id(bot, make_client):
    c = make_client(bot.port, {"CLAUDE_SESSION_ID": "abc-123"})
    c.call("telegram_notificar", {"texto": "oi"})
    assert bot.requests[0]["body"]["origem"]["session_id"] == "abc-123"


def test_notificar_bot_diz_nao_ok(bot, make_client):
    bot.body = {"ok": False}
    c = make_client(bot.port)
    r = c.call("telegram_notificar", {"texto": "x"})
    assert "enviado" not in text_of(r).split()
    assert r["result"]["isError"] is False


# ---------- telegram_perguntar ----------


def test_perguntar_ok_com_opcoes(bot, make_client):
    bot.body = {"ok": True, "resposta": "sim"}
    c = make_client(bot.port)
    r = c.call("telegram_perguntar", {"pergunta": "aplico?", "opcoes": ["sim", "não"]})
    assert text_of(r) == "sim"
    (req,) = bot.requests
    assert req["path"] == "/ask"
    assert req["secret"] == SECRET
    assert req["body"]["pergunta"] == "aplico?"
    assert req["body"]["opcoes"] == ["sim", "não"]
    assert req["body"]["timeout_s"] == 1800
    assert set(req["body"]["origem"]) == {"cwd", "session_id"}


def test_perguntar_timeout_custom_e_sem_opcoes(bot, make_client):
    bot.body = {"ok": True, "resposta": "ok"}
    c = make_client(bot.port)
    c.call("telegram_perguntar", {"pergunta": "?", "timeout_min": 5})
    body = bot.requests[0]["body"]
    assert body["timeout_s"] == 300
    assert body["opcoes"] == []


def test_perguntar_sem_resposta(bot, make_client):
    bot.body = {"ok": False}
    c = make_client(bot.port)
    r = c.call("telegram_perguntar", {"pergunta": "?", "timeout_min": 7})
    assert text_of(r) == "sem resposta em 7 min"
    assert r["result"]["isError"] is False


# ---------- bot offline / secret ----------


def test_bot_offline(state_dir, make_client):
    c = make_client(free_port())
    r = c.call("telegram_notificar", {"texto": "x"})
    assert text_of(r) == "bot offline"
    assert r["result"]["isError"] is False
    r = c.call("telegram_perguntar", {"pergunta": "?"})
    assert text_of(r) == "bot offline"


def test_secret_ausente(bot, make_client, tmp_path):
    vazio = tmp_path / "vazio"
    vazio.mkdir()
    c = make_client(bot.port, sdir=vazio)
    r = c.call("telegram_notificar", {"texto": "x"})
    assert text_of(r) == "bot offline"
    assert bot.requests == []


def test_secret_recusado_nao_vaza(bot, make_client):
    bot.status = 401
    bot.body = {"ok": False}
    c = make_client(bot.port)
    r = c.call("telegram_notificar", {"texto": "x"})
    assert text_of(r) == "bot respondeu erro 401"
    assert r["result"]["isError"] is True
    assert SECRET not in json.dumps(r)


def test_http_500(bot, make_client):
    bot.status = 500
    c = make_client(bot.port)
    r = c.call("telegram_perguntar", {"pergunta": "?"})
    assert text_of(r) == "bot respondeu erro 500"
    assert r["result"]["isError"] is True


@pytest.mark.parametrize("raw", [b"nao e json", b"[1, 2]", b"\xff\xfe", b""])
def test_corpo_invalido(bot, make_client, raw):
    bot.raw = raw
    c = make_client(bot.port)
    r = c.call("telegram_notificar", {"texto": "x"})
    assert r["result"]["isError"] is True
    assert "inválida" in text_of(r)


def test_nao_segue_redirect_nem_reenvia_secret(bot, make_client):
    destino = FakeBot()
    try:
        bot.status = 302
        bot.headers = {"Location": f"http://127.0.0.1:{destino.port}/notify"}
        c = make_client(bot.port)
        r = c.call("telegram_notificar", {"texto": "x"})
        assert text_of(r) == "bot respondeu erro 302"
        assert destino.requests == []
    finally:
        destino.close()


def test_resposta_utf8_sem_depender_do_locale(bot, make_client):
    bot.body = {"ok": True, "resposta": "não é possível — ✓"}
    c = make_client(bot.port, {"LC_ALL": "C", "LANG": "C", "PYTHONIOENCODING": "ascii"})
    r = c.call("telegram_perguntar", {"pergunta": "áé?"})
    assert text_of(r) == "não é possível — ✓"
    assert bot.requests[0]["body"]["pergunta"] == "áé?"


# ---------- concorrência e cancelamento ----------


def test_ping_e_tools_list_respondem_com_ask_pendente(bot, make_client):
    bot.body = {"ok": True, "resposta": "feito"}
    bot.hold = threading.Event()
    c = make_client(bot.port)
    ask_id = c.send_request(
        "tools/call", {"name": "telegram_perguntar", "arguments": {"pergunta": "?"}}
    )
    bot.wait_requests(1)
    assert c.request("ping")["result"] == {}
    assert len(c.request("tools/list")["result"]["tools"]) == 3
    bot.hold.set()
    r = c.read()
    assert r["id"] == ask_id
    assert text_of(r) == "feito"


def test_cancelled_aborta_ask_e_nao_responde(bot, make_client):
    bot.hold = threading.Event()
    c = make_client(bot.port)
    ask_id = c.send_request(
        "tools/call", {"name": "telegram_perguntar", "arguments": {"pergunta": "?"}}
    )
    bot.wait_requests(1)
    c.notify(
        "notifications/cancelled",
        {"requestId": ask_id, "reason": "usuário interrompeu"},
    )
    time.sleep(0.3)
    assert c.request("ping")["result"] == {}  # nenhuma resposta do ask antes
    bot.hold.set()
    time.sleep(0.3)
    assert c.request("ping")["result"] == {}  # nem depois que o bot responderia


def test_cancelled_de_id_inexistente_ou_invalido_e_ignorado(bot, make_client):
    c = make_client(bot.port)
    c.notify("notifications/cancelled", {"requestId": 999})
    c.notify("notifications/cancelled", {"requestId": [1]})
    c.notify("notifications/cancelled")
    assert c.request("ping")["result"] == {}


# ---------- entrada malformada nunca derruba o servidor ----------


@pytest.mark.parametrize("name", [["telegram_notificar"], {"a": 1}, 5, None, True])
def test_tool_name_nao_string(bot, make_client, name):
    c = make_client(bot.port)
    r = c.request("tools/call", {"name": name, "arguments": {}})
    assert r["error"]["code"] == -32602
    assert c.request("ping")["result"] == {}


@pytest.mark.parametrize(
    "line",
    [
        "[]",
        "5",
        '"x"',
        "null",
        "[1,2,3]",
        '{"id":1}',
        '{"jsonrpc":"1.0","id":1,"method":"ping"}',
    ],
)
def test_mensagem_nao_dict_ou_invalida(bot, make_client, line):
    c = make_client(bot.port)
    c.send_raw(line)
    r = c.read()
    assert r["error"]["code"] == -32600
    assert c.request("ping")["result"] == {}


@pytest.mark.parametrize("bad_id", [[1], {"a": 1}, 1.5, True])
def test_id_invalido(bot, make_client, bad_id):
    c = make_client(bot.port)
    c.send_raw(json.dumps({"jsonrpc": "2.0", "id": bad_id, "method": "ping"}))
    assert c.read()["error"]["code"] == -32600
    assert c.request("ping")["result"] == {}


def test_id_string_e_aceito(bot, make_client):
    c = make_client(bot.port)
    c.send_raw(json.dumps({"jsonrpc": "2.0", "id": "abc", "method": "ping"}))
    assert c.read() == {"jsonrpc": "2.0", "id": "abc", "result": {}}


@pytest.mark.parametrize("params", [[], "x", 5, None])
def test_params_nao_dict(bot, make_client, params):
    c = make_client(bot.port)
    c.send_raw(
        json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
        )
    )
    assert c.read()["error"]["code"] == -32602
    c.send_raw(
        json.dumps(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": params}
        )
    )
    assert len(c.read()["result"]["tools"]) == 3


@pytest.mark.parametrize("arguments", [[], "x", 5])
def test_arguments_nao_dict(bot, make_client, arguments):
    c = make_client(bot.port)
    r = c.request("tools/call", {"name": "telegram_notificar", "arguments": arguments})
    assert r["error"]["code"] == -32602
    assert c.request("ping")["result"] == {}


def test_json_aninhado_fundo_demais(bot, make_client):
    c = make_client(bot.port)
    c.send_raw("[" * 200000)
    assert c.read()["error"]["code"] == -32700
    assert c.request("ping")["result"] == {}


def test_bytes_invalidos_no_stdin(bot, make_client):
    c = make_client(bot.port)
    c.send_bytes(b'\xff\xfe\x80{"jsonrpc":"2.0"\n')
    assert c.read()["error"]["code"] == -32700
    c.send_bytes(
        '{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"telegram_notificar","arguments":{"texto":"a\udcff"}}}\n'.encode(
            "utf-8", "surrogatepass"
        )
    )
    assert c.read()["id"] == 7  # surrogate inválido em utf-8 vira U+FFFD, sem derrubar
    assert c.request("ping")["result"] == {}


def test_erro_inesperado_vira_32603_e_loop_segue(monkeypatch, capsys):
    from mcp_server import telegram_mcp as mod

    def boom(msg):
        raise RuntimeError("falha interna")

    monkeypatch.setattr(mod, "handle", boom)
    mod.process_line('{"jsonrpc":"2.0","id":3,"method":"ping"}')
    out = json.loads(capsys.readouterr().out)
    assert out["id"] == 3
    assert out["error"]["code"] == -32603
    mod.process_line("")  # linha vazia: nada
    assert capsys.readouterr().out == ""


# ---------- timeouts HTTP (in-process, constantes encurtadas) ----------


def test_ask_read_timeout_vira_sem_resposta(bot, state_dir, monkeypatch):
    from mcp_server import telegram_mcp as mod

    bot.hold = threading.Event()
    monkeypatch.setenv("CLAUDE_TELEGRAM_STATE_DIR", str(state_dir))
    monkeypatch.setenv("BOT_LOCAL_PORT", str(bot.port))
    monkeypatch.setattr(mod, "ASK_HTTP_MARGIN", -59.7)  # 60 s - 59.7 = 0.3 s
    res = mod.tool_perguntar({"pergunta": "?", "timeout_min": 1})
    assert res == {
        "content": [{"type": "text", "text": "sem resposta em 1 min"}],
        "isError": False,
    }


def test_notify_read_timeout(bot, state_dir, monkeypatch):
    from mcp_server import telegram_mcp as mod

    bot.hold = threading.Event()
    monkeypatch.setenv("CLAUDE_TELEGRAM_STATE_DIR", str(state_dir))
    monkeypatch.setenv("BOT_LOCAL_PORT", str(bot.port))
    monkeypatch.setattr(mod, "NOTIFY_HTTP_TIMEOUT", 0.3)
    res = mod.tool_notificar({"texto": "x"})
    assert "não respondeu a tempo" in res["content"][0]["text"]
    assert res["isError"] is False


# ---------- scripts/register-mcp.sh (com `claude` falso; nada real é tocado) ----------

REGISTER = Path(__file__).resolve().parent.parent / "scripts" / "register-mcp.sh"
FAKE_CLAUDE = """#!/usr/bin/env bash
key="${CLAUDE_CONFIG_DIR:-DEFAULT}"; key="${key//\\//_}"
st="$FAKE_STATE/$key"
echo "[${CLAUDE_CONFIG_DIR-UNSET}] $*" >> "$FAKE_STATE/calls.log"
case "$1 $2" in
  "mcp get") [ -f "$st" ] && cat "$st" || exit 1 ;;
  "mcp add") shift 2; echo "$*" > "$st" ;;
  "mcp remove") rm -f "$st" ;;
  *) exit 2 ;;
esac
"""


@pytest.fixture
def reg_env(tmp_path):
    home = tmp_path / "home"
    for d in (
        ".claude",
        ".claude-work",
        ".claude-personal",
    ):  # client ausente de propósito
        (home / d).mkdir(parents=True)
    fake_state = tmp_path / "fake_state"
    fake_state.mkdir()
    fake = tmp_path / "claude"
    fake.write_text(FAKE_CLAUDE)
    fake.chmod(0o755)
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "CLAUDE_BIN": str(fake),
        "FAKE_STATE": str(fake_state),
        "CLAUDE_CONFIG_DIR": "/nao/deve/vazar",
    }
    return env, fake_state


def run_register(env):
    return subprocess.run(  # noqa: S603 - script do próprio repo com claude falso
        ["/usr/bin/env", "bash", str(REGISTER)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_register_idempotente_e_conta_padrao_sem_config_dir(reg_env):
    env, st = reg_env
    r = run_register(env)
    assert r.returncode == 0, r.stderr
    calls = (st / "calls.log").read_text().splitlines()
    adds = [c for c in calls if " mcp add " in c]
    assert len(adds) == 3  # 3 pastas existentes; client pulada
    assert "pulando" in r.stdout
    assert any(
        c.startswith("[UNSET] mcp add --scope user telegram-bot") for c in adds
    )  # ~/.claude sem CLAUDE_CONFIG_DIR
    assert all("/nao/deve/vazar" not in c for c in calls)
    assert sum(1 for c in adds if "[UNSET]" not in c) == 2
    r2 = run_register(env)
    assert r2.returncode == 0
    calls2 = (st / "calls.log").read_text().splitlines()
    assert len([c for c in calls2 if " mcp add " in c]) == 3  # nada novo
    assert not any(" mcp remove " in c for c in calls2)
    assert r2.stdout.count("já registrado") == 3


def test_register_refaz_se_caminho_diferente(reg_env):
    env, st = reg_env
    assert run_register(env).returncode == 0
    (st / "DEFAULT").write_text("telegram-bot -- /outro/python /outro/script.py\n")
    r = run_register(env)
    assert r.returncode == 0
    calls = (st / "calls.log").read_text().splitlines()
    assert (
        sum(
            1
            for c in calls
            if c.startswith("[UNSET] mcp remove --scope user telegram-bot")
        )
        == 1
    )
    assert "/outro/python" not in (st / "DEFAULT").read_text()
    assert r.stdout.count("atualizado") == 1


# ---------- validação de argumentos ----------


@pytest.mark.parametrize(
    "tool,args",
    [
        ("telegram_notificar", {}),
        ("telegram_notificar", {"texto": ""}),
        ("telegram_notificar", {"texto": "   "}),
        ("telegram_notificar", {"texto": 5}),
        ("telegram_notificar", {"texto": "x" * 4001}),
        ("telegram_perguntar", {"pergunta": ""}),
        ("telegram_perguntar", {"pergunta": "?", "opcoes": "sim"}),
        ("telegram_perguntar", {"pergunta": "?", "opcoes": [1, 2]}),
        ("telegram_perguntar", {"pergunta": "?", "opcoes": [""]}),
        ("telegram_perguntar", {"pergunta": "?", "opcoes": ["a"] * 9}),
        ("telegram_perguntar", {"pergunta": "?", "timeout_min": 0}),
        ("telegram_perguntar", {"pergunta": "?", "timeout_min": 241}),
        ("telegram_perguntar", {"pergunta": "?", "timeout_min": "5"}),
        ("telegram_perguntar", {"pergunta": "?", "timeout_min": True}),
    ],
)
def test_argumentos_invalidos(bot, make_client, tool, args):
    c = make_client(bot.port)
    r = c.call(tool, args)
    assert r["result"]["isError"] is True
    assert r["result"]["content"][0]["type"] == "text"
    assert bot.requests == []


def test_porta_invalida(bot, make_client):
    c = make_client(bot.port, {"BOT_LOCAL_PORT": "abc"})
    r = c.call("telegram_notificar", {"texto": "x"})
    assert r["result"]["isError"] is True
    assert "BOT_LOCAL_PORT" in text_of(r)
