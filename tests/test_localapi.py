from __future__ import annotations

import asyncio
import json
import os
import stat

import pytest

from bot.localapi import Asks, LocalApi, check_secret, new_secret, write_secret

SECRET = "s3cret-token"


# ---- secret ----


def test_check_secret():
    assert check_secret(SECRET, SECRET) is True
    assert check_secret("outro", SECRET) is False
    assert check_secret(None, SECRET) is False
    assert check_secret("", SECRET) is False
    assert check_secret("açaí", SECRET) is False  # não-ASCII não levanta


def test_new_secret_aleatorio():
    a, b = new_secret(), new_secret()
    assert a != b and len(a) >= 32


def test_write_secret_modos(tmp_path):
    d = tmp_path / "estado"
    write_secret(d, SECRET)
    f = d / "local.secret"
    assert f.read_text() == SECRET
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    write_secret(d, "novo")  # regrava
    assert f.read_text() == "novo"
    assert not list(d.glob("*.tmp"))


def test_write_port_0600(tmp_path):
    from bot.localapi import write_port

    p = write_port(tmp_path / "estado", 31234)
    assert p.read_text() == "31234"
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_mcp_le_porta_env_depois_arquivo_depois_padrao(tmp_path, monkeypatch):
    from bot.localapi import write_port
    from mcp_server import telegram_mcp as mcp

    monkeypatch.setenv("CLAUDE_TELEGRAM_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("BOT_LOCAL_PORT", raising=False)
    assert mcp.read_port() == mcp.DEFAULT_PORT  # sem arquivo
    write_port(tmp_path, 31234)
    assert mcp.read_port() == 31234  # arquivo
    monkeypatch.setenv("BOT_LOCAL_PORT", "29999")
    assert mcp.read_port() == 29999  # env vence
    monkeypatch.delenv("BOT_LOCAL_PORT")
    (tmp_path / "local.port").write_text("lixo")
    assert mcp.read_port() == mcp.DEFAULT_PORT  # arquivo corrompido
    (tmp_path / "local.port").write_text("99999")
    assert mcp.read_port() == mcp.DEFAULT_PORT  # fora da faixa


def test_write_secret_corrige_dir_aberto(tmp_path):
    d = tmp_path / "estado"
    d.mkdir(mode=0o755)
    os.chmod(d, 0o755)  # noqa: S103 - dir aberto de propósito: o código deve corrigir
    write_secret(d, SECRET)
    assert stat.S_IMODE(d.stat().st_mode) == 0o700


# ---- Asks ----


async def test_asks_reply_por_message_id():
    asks = Asks()
    ask_id, fut = asks.create(["sim", "não"])
    asks.bind_message(ask_id, 42)
    assert asks.answer_reply(99, "x") is False  # outra mensagem
    assert not fut.done()
    assert asks.answer_reply(42, "talvez") is True
    assert fut.result() == "talvez"
    assert asks.answer_reply(42, "de novo") is False  # já respondido


async def test_asks_botao_por_indice():
    asks = Asks()
    ask_id, fut = asks.create(["a", "b", "c"])
    asks.bind_message(ask_id, 7)
    assert asks.answer_button(ask_id, 5) is None  # índice inválido
    assert asks.answer_button(ask_id, -1) is None
    assert asks.answer_button("inexistente", 0) is None
    assert not fut.done()
    assert asks.answer_button(ask_id, 1) == "b"
    assert fut.result() == "b"
    assert asks.answer_button(ask_id, 2) is None  # dupla


async def test_asks_drop_remove():
    asks = Asks()
    ask_id, fut = asks.create([])
    asks.bind_message(ask_id, 1)
    assert asks.has_message(1) is True
    asks.drop(ask_id)
    assert asks.has_message(1) is False
    assert asks.answer_reply(1, "x") is False
    assert asks.answer_button(ask_id, 0) is None
    assert asks.pending() == 0


# ---- servidor HTTP ----


async def http(port, method="POST", path="/notify", headers=None, body=b"", raw=None):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    if raw is None:
        hs = {"Content-Length": str(len(body)), **(headers or {})}
        head = f"{method} {path} HTTP/1.1\r\nHost: x\r\n" + "".join(
            f"{k}: {v}\r\n" for k, v in hs.items()
        )
        raw = head.encode() + b"\r\n" + body
    writer.write(raw)
    await writer.drain()
    data = await reader.read()
    writer.close()
    status = int(data.split(b" ", 2)[1])
    payload = data.split(b"\r\n\r\n", 1)[1]
    return status, (json.loads(payload) if payload else None)


@pytest.fixture
async def api():
    calls = {"notify": [], "ask": []}

    async def on_notify(texto, origem):
        calls["notify"].append((texto, origem))
        return True

    async def on_ask(pergunta, opcoes, timeout_s, origem):
        calls["ask"].append((pergunta, opcoes, timeout_s, origem))
        if pergunta == "dorme":
            await asyncio.sleep(30)
        return None if pergunta == "timeout" else "sim"

    srv = LocalApi(SECRET, on_notify, on_ask, port=0)
    await srv.start()
    yield srv, calls
    await srv.stop()


def auth():
    return {"X-Bot-Secret": SECRET}


async def test_sem_secret_401(api):
    srv, calls = api
    status, _ = await http(srv.port, body=b'{"texto":"x","origem":{}}')
    assert status == 401
    assert calls["notify"] == []


async def test_secret_errado_401(api):
    srv, calls = api
    status, _ = await http(srv.port, headers={"X-Bot-Secret": "errado"}, body=b"{}")
    assert status == 401
    assert calls["notify"] == []


async def test_rota_desconhecida_404(api):
    srv, _ = api
    status, _ = await http(srv.port, path="/outra", headers=auth(), body=b"{}")
    assert status == 404


async def test_metodo_get_405(api):
    srv, _ = api
    status, _ = await http(srv.port, method="GET", headers=auth())
    assert status == 405


async def test_json_invalido_400(api):
    srv, calls = api
    status, _ = await http(srv.port, headers=auth(), body=b"{nao json")
    assert status == 400
    status, _ = await http(srv.port, headers=auth(), body=b'["lista"]')
    assert status == 400
    assert calls["notify"] == []


async def test_chunked_rejeitado(api):
    srv, _ = api
    raw = (
        b"POST /notify HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n"
        b"X-Bot-Secret: " + SECRET.encode() + b"\r\n\r\n0\r\n\r\n"
    )
    status, _ = await http(srv.port, raw=raw)
    assert status in (400, 411)


async def test_corpo_grande_413(api):
    srv, _ = api
    status, _ = await http(
        srv.port, headers={**auth(), "Content-Length": "9999999"}, raw=None, body=b""
    )
    assert status == 413


async def test_corpo_grande_com_secret_errado_recebe_401_sem_reset(api):
    srv, _ = api
    status, _ = await http(
        srv.port, headers={"X-Bot-Secret": "errado"}, body=b"x" * 100_000
    )
    assert status == 401


async def test_notify_ok(api):
    srv, calls = api
    body = json.dumps({"texto": "oi", "origem": {"cwd": "/a/b", "session_id": None}})
    status, data = await http(srv.port, headers=auth(), body=body.encode())
    assert status == 200 and data == {"ok": True}
    assert calls["notify"] == [("oi", {"cwd": "/a/b", "session_id": None})]


async def test_notify_campos_invalidos_400(api):
    srv, calls = api
    for bad in ({"texto": ""}, {"texto": 5}, {"origem": {}}):
        status, _ = await http(srv.port, headers=auth(), body=json.dumps(bad).encode())
        assert status == 400
    assert calls["notify"] == []


async def test_ask_ok(api):
    srv, calls = api
    body = json.dumps(
        {"pergunta": "vai?", "opcoes": ["sim", "não"], "timeout_s": 60, "origem": {}}
    )
    status, data = await http(srv.port, path="/ask", headers=auth(), body=body.encode())
    assert status == 200 and data == {"ok": True, "resposta": "sim"}
    assert calls["ask"][0][:3] == ("vai?", ["sim", "não"], 60)


async def test_ask_timeout_ok_false(api):
    srv, _ = api
    body = json.dumps({"pergunta": "timeout", "opcoes": [], "timeout_s": 60})
    status, data = await http(srv.port, path="/ask", headers=auth(), body=body.encode())
    assert status == 200 and data == {"ok": False}


async def test_ask_timeout_s_invalido_400(api):
    srv, _ = api
    for t in (0, -5, "x", 10**9, True):
        body = json.dumps({"pergunta": "p", "opcoes": [], "timeout_s": t})
        status, _ = await http(
            srv.port, path="/ask", headers=auth(), body=body.encode()
        )
        assert status == 400


async def test_ask_cliente_desconecta_cancela_handler():
    cancelled = asyncio.Event()

    async def on_notify(texto, origem):
        return True

    async def on_ask(pergunta, opcoes, timeout_s, origem):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    srv = LocalApi(SECRET, on_notify, on_ask, port=0)
    await srv.start()
    try:
        body = json.dumps({"pergunta": "p", "opcoes": [], "timeout_s": 60}).encode()
        reader, writer = await asyncio.open_connection("127.0.0.1", srv.port)
        writer.write(
            b"POST /ask HTTP/1.1\r\nX-Bot-Secret: "
            + SECRET.encode()
            + b"\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\n\r\n"
            + body
        )
        await writer.drain()
        await asyncio.sleep(0.2)
        writer.close()
        await asyncio.wait_for(cancelled.wait(), 3)
    finally:
        await srv.stop()


async def test_handler_que_explode_500_sem_vazar():
    async def on_notify(texto, origem):
        raise RuntimeError("detalhe interno")

    async def on_ask(*a):
        return None

    srv = LocalApi(SECRET, on_notify, on_ask, port=0)
    await srv.start()
    try:
        body = json.dumps({"texto": "x", "origem": {}}).encode()
        status, data = await http(srv.port, headers=auth(), body=body)
        assert status == 500
        assert "detalhe" not in json.dumps(data)
    finally:
        await srv.stop()


async def test_compativel_com_o_cliente_do_mcp(api, tmp_path, monkeypatch):
    from mcp_server import telegram_mcp as mcp

    srv, calls = api
    write_secret(tmp_path, SECRET)
    monkeypatch.setenv("CLAUDE_TELEGRAM_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("BOT_LOCAL_PORT", str(srv.port))
    r = await asyncio.to_thread(mcp.tool_notificar, {"texto": "oi"})
    assert r["content"][0]["text"] == "enviado"
    r = await asyncio.to_thread(
        mcp.tool_perguntar, {"pergunta": "vai?", "opcoes": ["sim"], "timeout_min": 1}
    )
    assert r["content"][0]["text"] == "sim"
    assert calls["ask"][0][2] == 60


async def test_bind_so_loopback(api):
    srv, _ = api
    hosts = {s.getsockname()[0] for s in srv._server.sockets}
    assert hosts == {"127.0.0.1"}
