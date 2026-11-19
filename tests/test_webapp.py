from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from types import SimpleNamespace
from urllib.parse import quote, urlencode

import pytest

from bot import webapp
from bot.webapp import validate_init_data

TOKEN = "123456:TEST-token"
OWNER = 4242
NOW = 1_800_000_000
SID = "11111111-2222-3333-4444-555555555555"


# ---- vetor de teste gerado aqui (algoritmo da doc oficial) ----


def make_init_data(
    token: str = TOKEN,
    user_id: int | None = OWNER,
    auth_date: int = NOW,
    extra: dict | None = None,
    drop_user: bool = False,
    user_raw: str | None = None,
) -> str:
    fields: dict[str, str] = {
        "auth_date": str(auth_date),
        "query_id": "AAH-xyz_9",
        "chat_type": "private",
    }
    if not drop_user:
        fields["user"] = user_raw or json.dumps(
            {"id": user_id, "first_name": "Açaí & Cia", "username": "x"},
            ensure_ascii=False,
        )
    fields.update(extra or {})
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields, quote_via=quote)


# ---- validate_init_data (pura) ----


def test_init_data_valido():
    assert validate_init_data(make_init_data(), TOKEN, OWNER, NOW) is True


def test_init_data_hash_errado():
    raw = make_init_data()
    bad = raw.replace("hash=", "hash=0")[:-1]
    assert validate_init_data(bad, TOKEN, OWNER, NOW) is False


def test_init_data_token_errado():
    assert validate_init_data(make_init_data(), "999:outro", OWNER, NOW) is False


def test_init_data_campo_adulterado():
    raw = make_init_data().replace("chat_type=private", "chat_type=group")
    assert validate_init_data(raw, TOKEN, OWNER, NOW) is False


def test_init_data_sem_hash():
    raw = "&".join(p for p in make_init_data().split("&") if not p.startswith("hash="))
    assert validate_init_data(raw, TOKEN, OWNER, NOW) is False


def test_init_data_chave_duplicada():
    raw = make_init_data() + "&chat_type=private"
    assert validate_init_data(raw, TOKEN, OWNER, NOW) is False


def test_init_data_outro_usuario():
    assert validate_init_data(make_init_data(user_id=7), TOKEN, OWNER, NOW) is False


def test_init_data_sem_user():
    assert (
        validate_init_data(make_init_data(drop_user=True), TOKEN, OWNER, NOW) is False
    )


@pytest.mark.parametrize(
    "user_raw", ["não é json", "[1]", '{"id": "4242"}', '{"id": true}', '{"nome": 1}']
)
def test_init_data_user_invalido(user_raw):
    assert (
        validate_init_data(make_init_data(user_raw=user_raw), TOKEN, OWNER, NOW)
        is False
    )


def test_init_data_owner_none():
    assert validate_init_data(make_init_data(), TOKEN, None, NOW) is False


def test_init_data_vazio_ou_lixo():
    for raw in ("", "x", "hash=abc", "%%%"):
        assert validate_init_data(raw, TOKEN, OWNER, NOW) is False


def test_init_data_auth_date_24h():
    ok = make_init_data(auth_date=NOW - 86400)
    velho = make_init_data(auth_date=NOW - 86401)
    assert validate_init_data(ok, TOKEN, OWNER, NOW) is True
    assert validate_init_data(velho, TOKEN, OWNER, NOW) is False


def test_init_data_auth_date_futuro():
    assert (
        validate_init_data(make_init_data(auth_date=NOW + 30), TOKEN, OWNER, NOW)
        is True
    )
    assert (
        validate_init_data(make_init_data(auth_date=NOW + 3600), TOKEN, OWNER, NOW)
        is False
    )


@pytest.mark.parametrize("v", ["abc", "", "-5", "1.5"])
def test_init_data_auth_date_invalido(v):
    raw = make_init_data(extra={"auth_date": v})
    assert validate_init_data(raw, TOKEN, OWNER, NOW) is False


def test_init_data_signature_entra_no_hash():
    raw = make_init_data(extra={"signature": "sig_AbC-123"})
    assert validate_init_data(raw, TOKEN, OWNER, NOW) is True
    assert (
        validate_init_data(raw.replace("sig_AbC-123", "sig_AbC-124"), TOKEN, OWNER, NOW)
        is False
    )


# ---- servidor real com facade fake ----


class FakeFacade:
    def __init__(self, owner_id: int | None = OWNER) -> None:
        self.state = SimpleNamespace(owner_id=owner_id)
        self.calls: list[tuple] = []
        self.errors: dict[str, str] = {}
        self.boom = False
        self.locked = False

    def is_locked(self):
        return self.locked

    def _err(self, name: str):
        if self.boom:
            raise RuntimeError("segredo-interno-123")
        return self.errors.get(name)

    def api_state(self):
        if self.boom:
            raise RuntimeError("segredo-interno-123")
        return {
            "project": "/home/x/p",
            "project_label": "p",
            "account": "claude",
            "session_id": SID,
            "running": [],
            "power": {"ac": True, "battery": 80},
        }

    async def api_accounts(self):
        return [
            {
                "name": "claude",
                "active": True,
                "usable": True,
                "severity": "ok",
                "state": "ok",
            }
        ]

    async def select_account(self, name):
        self.calls.append(("select_account", name))
        return self._err("account")

    def api_projects(self):
        return [{"path": "/home/x/p", "label": "p", "active": True}]

    def select_project(self, path):
        self.calls.append(("select_project", path))
        return self._err("project")

    def api_sessions(self):
        return [
            {
                "session_id": SID,
                "short": "11111111",
                "account": "claude",
                "mtime": 1,
                "first_prompt": "oi",
                "active": True,
                "running": False,
            }
        ]

    def select_session(self, sid):
        self.calls.append(("select_session", sid))
        return self._err("session")

    def new_session(self):
        self.calls.append(("new_session",))
        return SID

    def api_history(self, sid, limit, before):
        self.calls.append(("api_history", sid, limit, before))
        return {
            "turns": [
                {"role": "user", "text": "u" * 500, "tool": None, "ts": None},
                {"role": "assistant", "text": "a" * 1000, "tool": None, "ts": None},
                {"role": "tool", "text": "ls", "tool": "Bash", "ts": None},
            ],
            "before": 3,
        }

    def run_feed(self, sid, after_seq):
        self.calls.append(("run_feed", sid, after_seq))
        return {
            "running": True,
            "events": [
                {
                    "seq": 1,
                    "kind": "tool",
                    "tool": "Bash",
                    "detail": "ls",
                    "text": "",
                    "ts": 1,
                }
            ],
            "result": None,
        }

    async def stop_active(self):
        self.calls.append(("stop_active",))
        return True


async def request(
    port: int,
    method: str,
    path: str,
    *,
    init: str | None = None,
    body: bytes | None = None,
    headers: dict | None = None,
    content_length: bool = True,
) -> tuple[int, dict, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    h = {"Host": "x"}
    if init is not None:
        h["X-Tg-Init-Data"] = init
    h.update(headers or {})
    if body is not None and content_length:
        h["Content-Length"] = str(len(body))
    head = (
        f"{method} {path} HTTP/1.1\r\n"
        + "".join(f"{k}: {v}\r\n" for k, v in h.items())
        + "\r\n"
    )
    writer.write(head.encode() + (body or b""))
    await writer.drain()
    raw = await reader.read()
    writer.close()
    head_raw, _, payload = raw.partition(b"\r\n\r\n")
    lines = head_raw.decode("latin-1").split("\r\n")
    status = int(lines[0].split(" ")[1])
    hdrs = {}
    for line in lines[1:]:
        k, _, v = line.partition(":")
        hdrs[k.strip().lower()] = v.strip()
    return status, hdrs, payload


def jbody(obj) -> bytes:
    return json.dumps(obj).encode()


@pytest.fixture
async def app(tmp_path, monkeypatch):
    monkeypatch.delenv("WEBAPP_PORT", raising=False)
    index = tmp_path / "index.html"
    index.write_text("<!doctype html><title>painel</title>")
    monkeypatch.setattr(webapp, "INDEX_PATH", index)
    facade = FakeFacade()
    cfg = SimpleNamespace(bot_token=TOKEN)
    clock = {"t": NOW}
    server = await webapp.start(facade, cfg, port=0, now=lambda: clock["t"])
    server.facade = facade
    server.clock = clock
    yield server
    await webapp.stop(server)


def auth(**kw) -> str:
    return make_init_data(**kw)


async def get_json(app, path, init=None):
    status, _, body = await request(
        app.port, "GET", path, init=init if init is not None else auth()
    )
    return status, json.loads(body) if body else None


async def post_json(app, path, obj, init=None):
    status, _, body = await request(
        app.port,
        "POST",
        path,
        init=init if init is not None else auth(),
        body=jbody(obj),
    )
    return status, json.loads(body) if body else None


# ---- estático e headers ----


async def test_index_sem_auth_e_headers(app):
    status, h, body = await request(app.port, "GET", "/")
    assert status == 200
    assert h["content-type"].startswith("text/html")
    assert h["x-content-type-options"] == "nosniff"
    assert h["referrer-policy"] == "no-referrer"
    csp = h["content-security-policy"]
    assert "default-src 'self'" in csp and "connect-src 'self'" in csp
    assert "frame-ancestors https://web.telegram.org https://*.telegram.org" in csp
    assert "https://telegram.org" in csp
    assert "access-control-allow-origin" not in h
    assert b"<" in body


async def test_index_ausente_404(app, monkeypatch, tmp_path):
    monkeypatch.setattr(webapp, "INDEX_PATH", tmp_path / "nao-existe.html")
    status, _, _ = await request(app.port, "GET", "/")
    assert status == 404


async def test_rota_desconhecida_404(app):
    for p in ("/x", "/api", "/api/nada", "/webapp/index.html", "/../etc/passwd"):
        status, h, _ = await request(app.port, "GET", p, init=auth())
        assert status == 404, p
        assert h["x-content-type-options"] == "nosniff"


async def test_metodo_nao_suportado_405(app):
    for m in ("PUT", "DELETE", "PATCH"):
        status, _, _ = await request(app.port, m, "/api/state", init=auth(), body=b"{}")
        assert status == 405
    status, _, _ = await request(app.port, "POST", "/", body=b"{}")
    assert status == 405


# ---- auth ----


async def test_api_sem_header_401_sem_corpo(app):
    status, h, body = await request(app.port, "GET", "/api/state")
    assert status == 401 and body == b""
    assert h["content-length"] == "0"
    assert h["x-content-type-options"] == "nosniff"


async def test_api_auth_invalida_401(app):
    for init in (
        auth(user_id=7),
        auth(token="999:outro"),
        auth(auth_date=NOW - 90000),
        "lixo",
        "",
    ):
        status, _, body = await request(app.port, "GET", "/api/state", init=init)
        assert status == 401 and body == b""
    assert app.facade.calls == []


async def test_api_sem_owner_401(app):
    app.facade.state.owner_id = None
    status, _, body = await request(app.port, "GET", "/api/state", init=auth())
    assert status == 401 and body == b""


async def test_auth_date_usa_relogio_injetado(app):
    app.clock["t"] = NOW + 86401
    status, _, _ = await request(app.port, "GET", "/api/state", init=auth())
    assert status == 401


async def test_post_sem_auth_nao_executa(app):
    status, _, _ = await request(
        app.port, "POST", "/api/account", body=jbody({"name": "oi"})
    )
    assert status == 401
    assert app.facade.calls == []


# ---- rotas GET ----


async def test_get_state(app):
    status, data = await get_json(app, "/api/state")
    assert (
        status == 200
        and data["project_label"] == "p"
        and data["power"]["battery"] == 80
    )


async def test_get_accounts_projects_sessions(app):
    s, d = await get_json(app, "/api/accounts")
    assert s == 200 and d[0]["name"] == "claude"
    s, d = await get_json(app, "/api/projects")
    assert s == 200 and d[0]["path"] == "/home/x/p"
    s, d = await get_json(app, "/api/sessions")
    assert s == 200 and d[0]["session_id"] == SID


async def test_get_history_params(app):
    s, d = await get_json(app, f"/api/history?session_id={SID}&limit=10&before=7")
    assert s == 200 and d["before"] == 3 and len(d["turns"]) == 3
    assert app.facade.calls[-1] == ("api_history", SID, 10, 7)
    await get_json(app, f"/api/history?session_id={SID}")
    assert app.facade.calls[-1] == ("api_history", SID, 30, None)
    await get_json(app, f"/api/history?session_id={SID}&before=")
    assert app.facade.calls[-1] == ("api_history", SID, 30, None)


@pytest.mark.parametrize(
    "qs",
    [
        "",
        "session_id=nao-uuid",
        "session_id=../../etc/passwd",
        f"session_id={SID}&limit=0",
        f"session_id={SID}&limit=101",
        f"session_id={SID}&limit=abc",
        f"session_id={SID}&before=-1",
        f"session_id={SID}&before=x",
    ],
)
async def test_history_param_invalido_400(app, qs):
    s, d = await get_json(app, "/api/history?" + qs)
    assert s == 400 and "error" in d
    assert app.facade.calls == []


async def test_get_run(app):
    s, d = await get_json(app, f"/api/run?session_id={SID}&after=5")
    assert s == 200 and d["running"] is True and d["events"][0]["seq"] == 1
    assert app.facade.calls[-1] == ("run_feed", SID, 5)
    await get_json(app, f"/api/run?session_id={SID}")
    assert app.facade.calls[-1] == ("run_feed", SID, 0)


async def test_run_param_invalido_400(app):
    for qs in (
        "",
        "session_id=zzz",
        f"session_id={SID}&after=-1",
        f"session_id={SID}&after=a",
    ):
        s, _ = await get_json(app, "/api/run?" + qs)
        assert s == 400


async def test_get_nao_aceita_post_e_vice_versa(app):
    s, _, _ = await request(app.port, "POST", "/api/state", init=auth(), body=b"{}")
    assert s == 405
    s, _, _ = await request(app.port, "GET", "/api/account", init=auth())
    assert s == 405


# ---- rotas POST ----


async def test_post_account_project(app):
    s, d = await post_json(app, "/api/account", {"name": "work"})
    assert s == 200 and d == {"ok": True}
    s, d = await post_json(app, "/api/project", {"path": "/home/x/p"})
    assert s == 200 and d == {"ok": True}
    assert ("select_account", "work") in app.facade.calls
    assert ("select_project", "/home/x/p") in app.facade.calls


async def test_post_erro_da_facade_400(app):
    app.facade.errors["account"] = "conta em uso"
    s, d = await post_json(app, "/api/account", {"name": "work"})
    assert s == 400 and d["error"] == "conta em uso" and d["ok"] is False
    app.facade.errors["project"] = "fora de ~"
    s, d = await post_json(app, "/api/project", {"path": "/etc"})
    assert s == 400 and d["error"] == "fora de ~"


@pytest.mark.parametrize(
    "path,body",
    [
        ("/api/account", {}),
        ("/api/account", {"name": ""}),
        ("/api/account", {"name": 5}),
        ("/api/account", {"name": "a" * 5000}),
        ("/api/project", {"path": ["x"]}),
        ("/api/project", {"path": "a\x00b"}),
        ("/api/session", {}),
        ("/api/session", {"session_id": "nao-uuid"}),
        ("/api/session", {"session_id": 5}),
        ("/api/session/end", {}),
        ("/api/session/end", {"session_id": "nao-uuid"}),
        ("/api/session/end", {"session_id": 5}),
        ("/api/session/use", {}),
        ("/api/session/use", {"session_id": "nao-uuid"}),
        ("/api/session/use", {"session_id": 5}),
    ],
)
async def test_post_validacao_400(app, path, body):
    s, d = await post_json(app, path, body)
    assert s == 400 and "error" in d
    assert app.facade.calls == []


async def test_post_session_select_com_last_exchange(app):
    s, d = await post_json(app, "/api/session", {"session_id": SID})
    assert s == 200 and d["session_id"] == SID
    assert d["last_prompt"] == "u" * 300 and d["last_reply"] == "a" * 800
    assert ("select_session", SID) in app.facade.calls


async def test_post_session_new(app):
    s, d = await post_json(app, "/api/session", {"new": True})
    assert s == 200 and d == {
        "session_id": SID,
        "last_prompt": None,
        "last_reply": None,
    }
    assert ("new_session",) in app.facade.calls


async def test_post_session_erro_400(app):
    app.facade.errors["session"] = "sessão não encontrada"
    s, d = await post_json(app, "/api/session", {"session_id": SID})
    assert s == 400 and d["error"] == "sessão não encontrada"


async def test_prompt_removido_404(app):
    s, _, _ = await request(
        app.port, "POST", "/api/prompt", init=auth(), body=jbody({"text": "olá"})
    )
    assert s == 404
    s, _, _ = await request(app.port, "GET", "/api/prompt", init=auth())
    assert s == 404
    assert app.facade.calls == []
    assert not hasattr(webapp.WebApp, "_post_prompt")


class SessionOpsFacade(FakeFacade):
    def __init__(self) -> None:
        super().__init__()
        self.end_result = {"ok": True, "outcome": "stopped"}
        self.use_result = {"ok": True}

    async def end_session(self, sid):
        self.calls.append(("end_session", sid))
        if self.boom:
            raise RuntimeError("segredo-interno-123")
        return self.end_result

    def use_session(self, sid):
        self.calls.append(("use_session", sid))
        return self.use_result


@pytest.fixture
def ops(app):
    f = SessionOpsFacade()
    app._facade = f
    app.facade = f
    return f


@pytest.mark.parametrize("outcome", ["stopped", "terminated", "not_running"])
async def test_session_end(app, ops, outcome):
    ops.end_result = {"ok": True, "outcome": outcome}
    s, d = await post_json(app, "/api/session/end", {"session_id": SID.upper()})
    assert s == 200 and d == {"ok": True, "outcome": outcome}
    assert ops.calls[-1] == ("end_session", SID)


async def test_session_end_erro_da_facade_repassado(app, ops):
    ops.end_result = {"ok": False, "error": "falhou"}
    s, d = await post_json(app, "/api/session/end", {"session_id": SID})
    assert s == 200 and d == {"ok": False, "error": "falhou"}


async def test_session_use(app, ops):
    s, d = await post_json(app, "/api/session/use", {"session_id": SID})
    assert s == 200 and d == {"ok": True}
    assert ops.calls[-1] == ("use_session", SID)
    ops.use_result = {"ok": False, "error": "sessão não encontrada"}
    s, d = await post_json(app, "/api/session/use", {"session_id": SID})
    assert s == 200 and d == {"ok": False, "error": "sessão não encontrada"}


async def test_session_use_resultado_string_e_erro_400(app, ops):
    ops.use_result = "sessão inválida"
    s, d = await post_json(app, "/api/session/use", {"session_id": SID})
    assert s == 400 and d == {"ok": False, "error": "sessão inválida"}
    ops.use_result = None
    s, d = await post_json(app, "/api/session/use", {"session_id": SID})
    assert s == 200 and d == {"ok": True}


@pytest.mark.parametrize("path", ["/api/session/end", "/api/session/use"])
async def test_session_ops_facade_sem_metodo_503(app, path):
    s, d = await post_json(app, path, {"session_id": SID})
    assert s == 503 and d == {"error": "indisponivel"}
    assert app.facade.calls == []


@pytest.mark.parametrize("path", ["/api/session/end", "/api/session/use"])
async def test_session_ops_trancado_423_e_sem_auth_401(app, ops, path):
    s, _, _ = await request(app.port, "POST", path, body=jbody({"session_id": SID}))
    assert s == 401
    s, _, _ = await request(
        app.port, "POST", path, init=auth(user_id=7), body=jbody({"session_id": SID})
    )
    assert s == 401
    ops.locked = True
    s, d = await post_json(app, path, {"session_id": SID})
    assert s == 423 and d == {"error": "locked"}
    s, _ = await get_json(app, path)
    assert s == 423
    assert ops.calls == []
    ops.locked = False
    s, _ = await get_json(app, path)
    assert s == 405


async def test_session_ops_erro_interno_500_sem_vazar(app, ops):
    ops.boom = True
    s, _, body = await request(
        app.port,
        "POST",
        "/api/session/end",
        init=auth(),
        body=jbody({"session_id": SID}),
    )
    assert s == 500 and b"segredo-interno" not in body


async def test_post_stop(app):
    s, d = await post_json(app, "/api/stop", {})
    assert s == 200 and d == {"ok": True}
    assert ("stop_active",) in app.facade.calls


async def test_post_stop_sem_corpo_aceito_com_content_length_zero(app):
    s, _, body = await request(app.port, "POST", "/api/stop", init=auth(), body=b"")
    assert s == 200 and json.loads(body) == {"ok": True}


# ---- robustez ----


async def test_excecao_da_facade_500_sem_vazar(app):
    app.facade.boom = True
    s, _, body = await request(app.port, "GET", "/api/state", init=auth())
    assert s == 500 and b"segredo-interno" not in body
    s, _, body = await request(
        app.port, "POST", "/api/account", init=auth(), body=jbody({"name": "x"})
    )
    assert s == 500 and b"segredo-interno" not in body


async def test_corpo_grande_413(app):
    s, _, _ = await request(
        app.port, "POST", "/api/account", init=auth(), body=b"x" * (64 * 1024 + 1)
    )
    assert s == 413


async def test_post_sem_content_length_411(app):
    s, _, _ = await request(
        app.port, "POST", "/api/account", init=auth(), body=b"{}", content_length=False
    )
    assert s == 411


async def test_transfer_encoding_411(app):
    s, _, _ = await request(
        app.port,
        "POST",
        "/api/account",
        init=auth(),
        body=b"{}",
        headers={"Transfer-Encoding": "chunked", "Content-Length": "2"},
    )
    assert s == 411


@pytest.mark.parametrize("raw", [b"nao json", b"[1,2]", b'"x"', b"\xff\xfe"])
async def test_json_invalido_400(app, raw):
    s, _, _ = await request(app.port, "POST", "/api/account", init=auth(), body=raw)
    assert s == 400
    assert app.facade.calls == []


async def test_requisicao_malformada_nao_derruba(app):
    reader, writer = await asyncio.open_connection("127.0.0.1", app.port)
    writer.write(b"lixo\r\n\r\n")
    await writer.drain()
    await reader.read()
    writer.close()
    s, _ = await get_json(app, "/api/state")
    assert s == 200


# ---- rate limit ----


async def test_rate_limit_120_por_minuto_por_ip(app):
    for _ in range(120):
        s, _, _ = await request(
            app.port,
            "GET",
            "/api/state",
            init=auth(),
            headers={"CF-Connecting-IP": "1.2.3.4"},
        )
        assert s == 200
    app.clock["t"] += 15
    s, h, body = await request(
        app.port,
        "GET",
        "/api/state",
        init=auth(auth_date=NOW + 15),
        headers={"CF-Connecting-IP": "1.2.3.4"},
    )
    assert s == 429 and body == b""
    assert h["retry-after"] == "45"
    app.clock["t"] -= 15
    # outro IP não é afetado
    s, _, _ = await request(
        app.port,
        "GET",
        "/api/state",
        init=auth(),
        headers={"CF-Connecting-IP": "5.6.7.8"},
    )
    assert s == 200
    # janela desliza
    app.clock["t"] += 61
    s, _, _ = await request(
        app.port,
        "GET",
        "/api/state",
        init=auth(),
        headers={"CF-Connecting-IP": "1.2.3.4"},
    )
    assert s == 200


async def test_rate_limit_conta_tentativas_sem_auth(app):
    for _ in range(120):
        s, _, _ = await request(
            app.port, "GET", "/api/state", headers={"CF-Connecting-IP": "9.9.9.9"}
        )
        assert s == 401
    s, _, _ = await request(
        app.port, "GET", "/api/state", headers={"CF-Connecting-IP": "9.9.9.9"}
    )
    assert s == 429


async def test_rate_limit_vale_tambem_para_index(app):
    for _ in range(120):
        s, _, _ = await request(
            app.port, "GET", "/", headers={"CF-Connecting-IP": "7.7.7.7"}
        )
        assert s == 200
    s, h, _ = await request(
        app.port, "GET", "/", headers={"CF-Connecting-IP": "7.7.7.7"}
    )
    assert s == 429 and "retry-after" in h


async def test_rate_limit_ipv6_agrupa_por_64(app):
    for i in range(120):
        ip = f"2001:db8:1:2:{i:x}::1"
        s, _, _ = await request(
            app.port, "GET", "/api/state", init=auth(), headers={"CF-Connecting-IP": ip}
        )
        assert s == 200
    s, _, _ = await request(
        app.port,
        "GET",
        "/api/state",
        init=auth(),
        headers={"CF-Connecting-IP": "2001:db8:1:2:ffff::9"},
    )
    assert s == 429
    s, _, _ = await request(
        app.port,
        "GET",
        "/api/state",
        init=auth(),
        headers={"CF-Connecting-IP": "2001:db8:1:3::1"},
    )
    assert s == 200


async def test_cf_connecting_ip_invalido_cai_no_peer(app):
    for _ in range(120):
        s, _, _ = await request(
            app.port,
            "GET",
            "/api/state",
            init=auth(),
            headers={"CF-Connecting-IP": "lixo-1"},
        )
        assert s == 200
    # outro valor inválido não ganha balde novo: todos caem no IP do peer
    s, _, _ = await request(
        app.port,
        "GET",
        "/api/state",
        init=auth(),
        headers={"CF-Connecting-IP": "lixo-2"},
    )
    assert s == 429


async def test_index_cache_revalida_por_mtime(app, tmp_path):
    import os

    index = webapp.INDEX_PATH
    _, _, a = await request(app.port, "GET", "/")
    assert b"painel" in a
    index.write_text("<!doctype html><title>novo</title>-----")
    st = index.stat()
    os.utime(index, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    _, _, b = await request(app.port, "GET", "/")
    assert b"novo" in b


async def test_csp_form_action_none(app):
    _, h, _ = await request(app.port, "GET", "/")
    assert "form-action 'none'" in h["content-security-policy"]


# ---- cabeçalhos HTTP estritos ----


async def _raw_status(port: int, data: bytes) -> int:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(data)
    await writer.drain()
    raw = await reader.read()
    writer.close()
    return int(raw.split(b" ", 2)[1])


async def test_content_length_duplicado_400(app):
    req = b"POST /api/account HTTP/1.1\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\n{}"
    assert await _raw_status(app.port, req) == 400


async def test_linha_de_header_sem_dois_pontos_400(app):
    assert await _raw_status(app.port, b"GET / HTTP/1.1\r\nlixo\r\n\r\n") == 400


async def test_versao_http_invalida_400(app):
    assert await _raw_status(app.port, b"GET / SPDY/9\r\n\r\n") == 400


# ---- facade: sync ou async; sem is_locked -> 503 ----


class AsyncFacade(FakeFacade):
    async def is_locked(self):
        return self.locked

    async def api_history(self, sid, limit, before):
        return super().api_history(sid, limit, before)


async def test_facade_async_is_locked_e_historico(app):
    app._facade = AsyncFacade()
    s, d = await get_json(app, "/api/state")
    assert s == 200 and d["locked"] is False
    s, d = await get_json(app, f"/api/history?session_id={SID}")
    assert s == 200 and len(d["turns"]) == 3
    s, d = await post_json(app, "/api/session", {"session_id": SID})
    assert s == 200 and d["last_reply"] == "a" * 800
    app._facade.locked = True
    s, d = await get_json(app, "/api/accounts")
    assert s == 423


async def test_facade_sem_is_locked_503(app):
    class Sem(FakeFacade):
        is_locked = None

    app._facade = Sem()
    for method, path in (
        ("GET", "/api/state"),
        ("GET", "/api/accounts"),
        ("POST", "/api/stop"),
    ):
        s, _, raw = await request(
            app.port,
            method,
            path,
            init=auth(),
            body=b"{}" if method == "POST" else None,
        )
        assert s == 503 and json.loads(raw) == {"error": "indisponivel"}
    s, _, _ = await request(app.port, "GET", "/api/state")  # sem auth continua 401
    assert s == 401


def test_ratelimiter_retry_after():
    rl = webapp.RateLimiter(limit=1, window_s=60, max_keys=5)
    assert rl.retry_after("x", 0.0) == 1  # sem histórico
    assert rl.allow("x", 100.0) is True
    assert rl.allow("x", 130.5) is False
    assert rl.retry_after("x", 130.5) == 30  # ceil(100+60-130.5)
    assert rl.retry_after("x", 159.99) == 1  # nunca < 1


def test_ratelimiter_poda_ips_ociosos():
    rl = webapp.RateLimiter(limit=2, window_s=10, max_keys=5)
    for i in range(20):
        assert rl.allow(f"ip{i}", float(i * 11)) is True
    assert len(rl._hits) <= 5


# ---- porta / ciclo de vida ----


def test_port_from_env(monkeypatch):
    monkeypatch.delenv("WEBAPP_PORT", raising=False)
    assert webapp._port(None) == 28766
    monkeypatch.setenv("WEBAPP_PORT", "30001")
    assert webapp._port(None) == 30001
    monkeypatch.setenv("WEBAPP_PORT", "abc")
    with pytest.raises(ValueError):
        webapp._port(None)
    monkeypatch.setenv("WEBAPP_PORT", "70000")
    with pytest.raises(ValueError):
        webapp._port(None)


async def test_bind_apenas_loopback(app):
    host = app._server.sockets[0].getsockname()[0]
    assert host == "127.0.0.1"


async def test_stop_encerra(app):
    port = app.port
    await webapp.stop(app)
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", port)
    await webapp.stop(app)  # idempotente


# ---- trava TOTP (is_locked) ----


async def test_state_inclui_locked_false(app):
    s, d = await get_json(app, "/api/state")
    assert s == 200 and d["locked"] is False


async def test_trancado_state_responde_com_locked_true(app):
    app.facade.locked = True
    s, d = await get_json(app, "/api/state")
    assert s == 200 and d["locked"] is True and d["project_label"] == "p"


async def test_trancado_state_ainda_exige_auth(app):
    app.facade.locked = True
    s, _, body = await request(app.port, "GET", "/api/state")
    assert s == 401 and body == b""
    s, _, _ = await request(app.port, "GET", "/api/state", init=auth(user_id=7))
    assert s == 401


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/accounts"),
        ("GET", "/api/projects"),
        ("GET", "/api/sessions"),
        ("GET", f"/api/history?session_id={SID}"),
        ("GET", f"/api/run?session_id={SID}"),
        ("POST", "/api/account"),
        ("POST", "/api/project"),
        ("POST", "/api/session"),
        ("POST", "/api/session/end"),
        ("POST", "/api/session/use"),
        ("POST", "/api/stop"),
        ("POST", "/api/state"),
    ],
)
async def test_trancado_demais_rotas_423(app, method, path):
    app.facade.locked = True
    body = (
        jbody({"text": "x", "name": "x", "path": "/x", "new": True})
        if method == "POST"
        else None
    )
    s, _, raw = await request(app.port, method, path, init=auth(), body=body)
    assert s == 423 and json.loads(raw) == {"error": "locked"}
    assert app.facade.calls == []


async def test_trancado_auth_invalida_continua_401(app):
    app.facade.locked = True
    s, _, _ = await request(app.port, "GET", "/api/accounts", init=auth(user_id=7))
    assert s == 401


async def test_destrancado_volta_a_responder(app):
    app.facade.locked = True
    s, _ = await get_json(app, "/api/accounts")
    assert s == 423
    app.facade.locked = False
    s, _ = await get_json(app, "/api/accounts")
    assert s == 200


# ---- sessões abertas (scope) e nova sessão em projeto ----


def _scoped_facade(app):
    f = app.facade

    def api_sessions(scope="recent"):
        f.calls.append(("api_sessions", scope))
        return [{"session_id": SID, "scope": scope}]

    def new_session(project=None, account=None):
        f.calls.append(("new_session", project, account))
        return SID

    f.api_sessions = api_sessions
    f.new_session = new_session
    return f


@pytest.mark.parametrize(
    "qs,expected",
    [("", "recent"), ("?scope=recent", "recent"), ("?scope=open", "open")],
)
async def test_sessions_scope_repassado(app, qs, expected):
    f = _scoped_facade(app)
    s, d = await get_json(app, f"/api/sessions{qs}")
    assert s == 200 and d[0]["scope"] == expected
    assert f.calls[-1] == ("api_sessions", expected)


@pytest.mark.parametrize(
    "qs", ["?scope=all", "?scope=", "?scope=OPEN", "?scope=open%00"]
)
async def test_sessions_scope_invalido_400(app, qs):
    f = _scoped_facade(app)
    s, d = await get_json(app, f"/api/sessions{qs}")
    assert s == 400 and d["ok"] is False and d["error"]
    assert f.calls == []


async def test_sessions_scope_facade_antiga_recent_ok(app):
    s, d = await get_json(app, "/api/sessions?scope=recent")
    assert s == 200 and d[0]["session_id"] == SID
    s, d = await get_json(app, "/api/sessions")
    assert s == 200 and d[0]["session_id"] == SID


async def test_sessions_scope_facade_antiga_open_400_unsupported(app):
    s, d = await get_json(app, "/api/sessions?scope=open")
    assert s == 400 and d == {"error": "unsupported"}


async def test_sessions_scope_trancado_423(app):
    f = _scoped_facade(app)
    f.locked = True
    s, _ = await get_json(app, "/api/sessions?scope=open")
    assert s == 423 and f.calls == []


async def test_post_session_new_com_projeto_e_conta(app):
    f = _scoped_facade(app)
    s, d = await post_json(
        app, "/api/session", {"new": True, "project": "/home/x/p", "account": "claude"}
    )
    assert s == 200 and d == {
        "session_id": SID,
        "last_prompt": None,
        "last_reply": None,
    }
    assert f.calls[-1] == ("new_session", "/home/x/p", "claude")


async def test_post_session_new_so_projeto(app):
    f = _scoped_facade(app)
    s, _ = await post_json(app, "/api/session", {"new": True, "project": "/home/x/p"})
    assert s == 200 and f.calls[-1] == ("new_session", "/home/x/p", None)


async def test_post_session_new_sem_args_facade_nova(app):
    f = _scoped_facade(app)
    s, _ = await post_json(app, "/api/session", {"new": True})
    assert s == 200 and f.calls[-1] == ("new_session", None, None)


async def test_post_session_new_facade_antiga_sem_args_ok(app):
    s, d = await post_json(app, "/api/session", {"new": True, "project": None})
    assert s == 200 and d["session_id"] == SID
    assert app.facade.calls[-1] == ("new_session",)


async def test_post_session_new_facade_antiga_com_projeto_unsupported(app):
    s, d = await post_json(app, "/api/session", {"new": True, "project": "/x"})
    assert s == 400 and d == {"ok": False, "error": "unsupported"}
    assert app.facade.calls == []


@pytest.mark.parametrize(
    "body",
    [
        {"new": True, "project": 5},
        {"new": True, "project": ""},
        {"new": True, "project": "   "},
        {"new": True, "project": "a" * 4097},
        {"new": True, "project": "a\x00b"},
        {"new": True, "project": ["x"]},
        {"new": True, "account": 5},
        {"new": True, "account": "a" * 65},
        {"new": True, "account": ""},
    ],
)
async def test_post_session_new_validacao_400(app, body):
    f = _scoped_facade(app)
    s, d = await post_json(app, "/api/session", body)
    assert s == 400 and d["ok"] is False and d["error"]
    assert f.calls == []


async def test_post_session_new_limites_aceitos(app):
    f = _scoped_facade(app)
    s, _ = await post_json(
        app, "/api/session", {"new": True, "project": "a" * 4096, "account": "b" * 64}
    )
    assert s == 200 and f.calls[-1] == ("new_session", "a" * 4096, "b" * 64)


async def test_post_session_new_resultado_string_nao_uuid_e_erro(app):
    f = _scoped_facade(app)
    f.new_session = lambda project=None, account=None: "projeto inválido"
    s, d = await post_json(app, "/api/session", {"new": True, "project": "/x"})
    assert s == 400 and d == {"ok": False, "error": "projeto inválido"}


async def test_post_session_new_resultado_dict(app):
    f = _scoped_facade(app)
    f.new_session = lambda project=None, account=None: {"session_id": SID.upper()}
    s, d = await post_json(app, "/api/session", {"new": True, "project": "/x"})
    assert s == 200 and d["session_id"] == SID
    f.new_session = lambda project=None, account=None: {"error": "sem conta"}
    s, d = await post_json(app, "/api/session", {"new": True, "project": "/x"})
    assert s == 400 and d == {"ok": False, "error": "sem conta"}


async def test_post_session_new_async_facade(app):
    f = _scoped_facade(app)

    async def new_session(project=None, account=None):
        return SID

    f.new_session = new_session
    s, d = await post_json(app, "/api/session", {"new": True, "project": "/x"})
    assert s == 200 and d["session_id"] == SID


async def test_state_repassa_working_e_slots(app):
    base = app.facade.api_state

    def api_state():
        return {**base(), "working": [SID], "slots": {"max": 3, "used": 1}}

    app.facade.api_state = api_state
    s, d = await get_json(app, "/api/state")
    assert s == 200 and d["working"] == [SID] and d["slots"] == {"max": 3, "used": 1}
    assert d["locked"] is False
