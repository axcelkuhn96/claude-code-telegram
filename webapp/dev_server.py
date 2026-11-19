#!/usr/bin/env python3
"""Servidor FALSO da API do Mini App (contrato C4) so para validar o frontend localmente.

Dados enlatados, estado em memoria. Aceita qualquer X-Tg-Init-Data, exceto o valor
"bad" (devolve 401, para testar a tela de acesso restrito). Bind somente em 127.0.0.1.
Uso: python3 webapp/dev_server.py [porta]   (padrao 28790)
"""

import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

INDEX = Path(__file__).with_name("index.html")
RUN_SECONDS = 9.0
NOW = lambda: datetime.now(timezone.utc)  # noqa: E731

LOCK = threading.Lock()
STATE = {
    "project": "/home/dev/project-b",
    "account": "claude1",
    "session_id": "a1b2c3d4-0000-4000-8000-000000000001",
}
PROJECTS = {
    "/home/dev/project-b": "project-b",
    "/home/dev/claude-telegram": "claude-telegram",
    "/home/dev/project-g": "project-g",
    "/home/dev": "~ (home)",
}
# sessoes abertas enlatadas: sid -> (projeto, working, pc, last_prompt, age_s)
OPEN_BASE = {
    "b2c3d4e5-0000-4000-8000-000000000002": (
        "/home/dev/claude-telegram",
        True,
        True,
        "Roda a migração de pagamentos em staging e me diz o que quebrou",
        45,
    ),
    "c3d4e5f6-0000-4000-8000-000000000003": (
        "/home/dev/project-g",
        True,
        False,
        "Resume o diff da branch feat/relatorio e sugere o texto do PR",
        3 * 60,
    ),
    "d4e5f6a7-0000-4000-8000-000000000004": (
        "/home/dev/project-b",
        False,
        True,
        "Revisa o PR #42 antes do deploy de sexta",
        26 * 60,
    ),
    "a1b2c3d4-0000-4000-8000-000000000001": (
        "/home/dev/project-b",
        False,
        False,
        "",
        2 * 3600,
    ),
}
NEW_META = {}  # sid -> {"project", "account"} das sessoes criadas pelo painel
SESS_BASE = [
    (
        "a1b2c3d4-0000-4000-8000-000000000001",
        "claude1",
        120,
        "Corrige o webhook que está devolvendo 502 quando o payload vem com acento no nome",
    ),
    (
        "b2c3d4e5-0000-4000-8000-000000000002",
        "work",
        3 * 60,
        "Roda a migração de pagamentos em staging e me diz o que quebrou",
    ),
    (
        "c3d4e5f6-0000-4000-8000-000000000003",
        "claude1",
        26 * 3600,
        "Resume o diff da branch feat/relatorio e sugere o texto do PR",
    ),
    ("d4e5f6a7-0000-4000-8000-000000000004", "personal", 4 * 86400, ""),
    (
        "e5f6a7b8-0000-4000-8000-000000000005",
        "work",
        2 * 86400,
        "Explica a causa do 502 com tabela, código e lista",
    ),
]
TURNS_BY_SID = {
    "a1b2c3d4-0000-4000-8000-000000000001": 95,
    "b2c3d4e5-0000-4000-8000-000000000002": 4,
    "c3d4e5f6-0000-4000-8000-000000000003": 12,
    "d4e5f6a7-0000-4000-8000-000000000004": 0,
}
EXTRA = {}  # sid -> turnos acrescentados por prompts
RUN = {}  # sid -> {start, text}; semeado abaixo (o painel nao envia prompts)
CALLS = []  # (rota, sid) de /api/session/use|end, para o teste conferir
RESULT = {}  # sid -> result dict
NEW_COUNT = [0]
LOCKED = [
    os.environ.get("DEV_LOCKED") == "1"
]  # DEV_LOCKED=1 inicia trancado; GET /api/dev/unlock destrava

LONG = (
    "Analisei o handler e encontrei a causa: o campo `nome` chega em latin-1 e o decoder assume UTF-8, "
    "então qualquer acento vira U+FFFD e a validação de assinatura falha.\n\n"
    "Correção proposta:\n```python\ndef decode(raw: bytes) -> str:\n    try:\n        return raw.decode('utf-8')\n"
    "    except UnicodeDecodeError:\n        return raw.decode('latin-1')\n```\n"
    "Adicionei dois testes cobrindo `João` e `Conceição`. "
    + "Rodei a suíte inteira e passou. "
    * 18
)


def turn(i, sid):
    ts = (NOW() - timedelta(minutes=(400 - i) * 7)).isoformat()
    k = i % 5
    if k == 0:
        return {
            "role": "user",
            "text": f"Mensagem {i}: confere o log do worker e me diz se tem erro novo.",
            "tool": None,
            "ts": ts,
        }
    if k == 1:
        return {
            "role": "tool",
            "text": "tail -n 200 /var/log/worker.log | grep -i error",
            "tool": "Bash",
            "ts": ts,
        }
    if k == 2:
        return {
            "role": "tool",
            "text": "/srv/app/handlers/webhook.py (412 linhas)",
            "tool": "Read",
            "ts": ts,
        }
    if k == 3:
        return {
            "role": "assistant",
            "text": LONG
            if i % 15 == 3
            else f"Resposta {i}: não achei erro novo no worker desde as 14h. Os 3 timeouts são do mesmo cliente.",
            "tool": None,
            "ts": ts,
        }
    return {
        "role": "user",
        "text": f"Ok, e o consumo de memória? ({i})\n```\nfree -m\n```",
        "tool": None,
        "ts": ts,
    }


RICH_SID = "e5f6a7b8-0000-4000-8000-000000000005"
RICH_MD = """## Causa e correção

O decoder assume **UTF-8**, mas o campo `nome` chega em *latin-1*. Veja a [documentação](https://docs.python.org/3/library/codecs.html) e ~~o palpite antigo~~.

```python
def decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")  # linha longa o bastante para forçar rolagem horizontal no bloco de código
```

### Resultado dos testes

| Caso | Antes | Depois | Observação sobre o resultado |
|:-----|:-----:|-------:|------------------------------|
| João | falha | ok | validação de assinatura passou depois do fallback |
| Conceição | falha | ok | acentos preservados em todo o payload |
| ASCII puro | ok | ok | sem regressão |

Próximos passos:

1. Rodar a suíte inteira
2. Subir em staging
   - conferir o log do worker
   - conferir o consumo de memória
3. Abrir o PR

- [x] testes escritos
- [ ] deploy

> Atenção: não rode a migração em produção antes do deploy do webhook.
> Segunda linha da citação com `código` inline.

---

Strings hostis (devem aparecer como texto, nunca virar elemento): <img src=x onerror="window.__pwn=1"> e <script>window.__pwn=2</script>
e um [link malicioso](javascript:window.__pwn=3) e [outro](data:text/html,<b>x</b>) e ![imagem](javascript:alert(1)) e [relativo](/etc/passwd).
Link normal automático: https://example.com/caminho?x=1 fim."""
RICH_LONG = "Relatório longo (mais de 40 linhas):\n\n" + "\n".join(
    f"- item {n}: texto de exemplo para forçar o recolhimento" for n in range(1, 56)
)
RICH_TURNS = [
    {
        "role": "user",
        "text": "Resume o problema do webhook com **formatação**, por favor.\n- primeiro ponto\n- segundo ponto",
        "tool": None,
    },
    {
        "role": "tool",
        "tool": "Bash",
        "text": "cd /srv/app && git diff --stat HEAD~1\n webhook.py | 12 ++++++------\n 1 file changed, 6 insertions(+), 6 deletions(-)",
    },
    {"role": "assistant", "text": RICH_MD, "tool": None},
    {"role": "assistant", "text": RICH_LONG, "tool": None},
    {"role": "user", "text": '<img src=x onerror="window.__pwn=9"> ok?', "tool": None},
]


def rich_turns():
    return [
        dict(t, ts=(NOW() - timedelta(minutes=(len(RICH_TURNS) - k) * 3)).isoformat())
        for k, t in enumerate(RICH_TURNS)
    ]


def all_turns(sid):
    if sid == RICH_SID:
        return rich_turns()
    base = [turn(i, sid) for i in range(TURNS_BY_SID.get(sid, 6))]
    return base + EXTRA.get(sid, [])


def sessions():
    out = []
    items = list(SESS_BASE) + [
        (s, NEW_META.get(s, {}).get("account", "claude1"), 5, "Nova sessão")
        for s in TURNS_BY_SID
        if s not in {b[0] for b in SESS_BASE}
    ]
    for sid, acc, age, first in items:
        out.append(
            {
                "session_id": sid,
                "short": sid[:8],
                "account": acc,
                "first_prompt": first,
                "mtime": (NOW() - timedelta(seconds=age)).timestamp(),
                "active": sid == STATE["session_id"],
                "running": sid in RUN,
            }
        )
    return out


WORKING = {sid for sid, v in OPEN_BASE.items() if v[1]}
PC_OPEN = {sid for sid, v in OPEN_BASE.items() if v[2]}
RUN["b2c3d4e5-0000-4000-8000-000000000002"] = {"start": time.time(), "text": ""}


def working_sids():
    return WORKING | set(RUN)


def open_sessions():
    known = {s["session_id"]: s for s in sessions()}
    out = []
    ids = list(OPEN_BASE) + [s for s in NEW_META if s not in OPEN_BASE]
    if STATE["session_id"] not in ids:
        ids.append(STATE["session_id"])
    for sid in ids:
        base = OPEN_BASE.get(sid)
        proj = (
            base[0] if base else NEW_META.get(sid, {}).get("project", STATE["project"])
        )
        working = sid in working_sids()
        pc = sid in PC_OPEN
        active = sid == STATE["session_id"]
        if not (working or pc or active) or sid not in known:
            continue
        out.append(
            {
                "session_id": sid,
                "short": sid[:8],
                "project": proj,
                "project_label": PROJECTS.get(proj, proj),
                "account": known[sid]["account"],
                "age_s": base[4] if base else 5,
                "last_prompt": (base[3] if base else "") or None,
                "first_prompt": known[sid]["first_prompt"],
                "working": working,
                "pc": pc,
                "active": active,
            }
        )
    return out


def accounts():
    def w(pct, mins):
        return {
            "percent": pct,
            "resets_at": (NOW() + timedelta(minutes=mins)).isoformat(),
        }

    data = [
        ("claude1", w(34, 140), w(61, 3 * 1440 + 200), "ok", "ok", True),
        ("work", w(78, 55), w(40, 5 * 1440), "warn", "ok", True),
        ("personal", w(100, 190), w(93, 2 * 1440 + 60), "crit", "exhausted", False),
    ]
    return [
        {
            "name": n,
            "five_hour": a,
            "seven_day": b,
            "severity": sv,
            "state": st,
            "active": n == STATE["account"],
            "usable": us,
        }
        for n, a, b, sv, st, us in data
    ]


def run_view(sid, after):
    r = RUN.get(sid)
    if not r:
        return {"running": False, "events": [], "result": RESULT.get(sid)}
    el = time.time() - r["start"]
    script = [
        ("tool", "Read", "src/handlers/webhook.py", ""),
        ("tool", "Grep", "decode( em src/", ""),
        ("text", None, "", "Achei o ponto onde o payload é decodificado."),
        ("tool", "Edit", "src/handlers/webhook.py", ""),
        ("tool", "Bash", "pytest -q tests/test_webhook.py", ""),
        ("text", None, "", "Testes passando, resumindo o resultado."),
        ("tool", "Bash", "git diff --stat", ""),
    ]
    n = min(len(script), int(el / (RUN_SECONDS / (len(script) + 1))) + 1)
    events = [
        {
            "seq": i + 1,
            "kind": k,
            "tool": t,
            "detail": d,
            "text": x,
            "ts": (NOW() - timedelta(seconds=el - i)).isoformat(),
        }
        for i, (k, t, d, x) in enumerate(script[:n])
        if i + 1 > after
    ]
    if el >= RUN_SECONDS:
        reply = "Corrigi o decoder do webhook (fallback para latin-1) e adicionei 2 testes. Suíte verde."
        res = {
            "text": reply,
            "cost_usd": 0.31,
            "duration_ms": int(RUN_SECONDS * 1000),
            "is_error": False,
        }
        EXTRA.setdefault(sid, []).append(
            {"role": "assistant", "text": reply, "tool": None, "ts": NOW().isoformat()}
        )
        RESULT[sid] = res
        del RUN[sid]
        return {"running": False, "events": events, "result": res}
    return {"running": True, "events": events, "result": None}


class H(BaseHTTPRequestHandler):
    server_version = "dev"

    def log_message(self, fmt, *a):
        pass

    def send_json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            body = INDEX.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if not u.path.startswith("/api/"):
            self.send_json(404, {"error": "não encontrado"})
            return
        if self.headers.get("X-Tg-Init-Data") == "bad":
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if u.path == "/api/dev/unlock":  # so para teste: GET destrava
            LOCKED[0] = False
            return self.send_json(200, {"ok": True})
        if u.path == "/api/dev/calls":  # so para teste
            return self.send_json(200, CALLS)
        if LOCKED[0] and u.path != "/api/state":
            return self.send_json(423, {"error": "locked"})
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        with LOCK:
            if u.path == "/api/state":
                st = dict(STATE)
                st["locked"] = LOCKED[0]
                st["project_label"] = PROJECTS.get(st["project"], st["project"])
                st["running"] = list(RUN)
                st["working"] = len(working_sids())
                st["slots"] = {"used": len(working_sids()), "max": 3}
                st["power"] = {"ac": False, "battery": 62}
                return self.send_json(200, st)
            if u.path == "/api/accounts":
                return self.send_json(200, accounts())
            if u.path == "/api/projects":
                return self.send_json(
                    200,
                    [
                        {"path": p, "label": l, "active": p == STATE["project"]}
                        for p, l in PROJECTS.items()
                    ],
                )
            if u.path == "/api/sessions":
                if q.get("scope") == "open":
                    return self.send_json(200, open_sessions())
                return self.send_json(200, sessions())
            if u.path == "/api/history":
                turns = all_turns(q.get("session_id", ""))
                end = (
                    int(q["before"])
                    if q.get("before") not in (None, "")
                    else len(turns)
                )
                limit = max(1, min(100, int(q.get("limit", 30))))
                start = max(0, end - limit)
                return self.send_json(
                    200,
                    {"turns": turns[start:end], "before": start if start > 0 else None},
                )
            if u.path == "/api/run":
                return self.send_json(
                    200, run_view(q.get("session_id", ""), int(q.get("after", 0)))
                )
        self.send_json(404, {"error": "não encontrado"})

    def do_POST(self):
        if self.headers.get("X-Tg-Init-Data") == "bad":
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if LOCKED[0]:
            return self.send_json(423, {"error": "locked"})
        n = int(self.headers.get("Content-Length") or 0)
        if n > 65536:
            return self.send_json(400, {"error": "corpo grande demais"})
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self.send_json(400, {"error": "json inválido"})
        path = urlparse(self.path).path
        with LOCK:
            if path == "/api/account":
                if body.get("name") not in {a["name"] for a in accounts()}:
                    return self.send_json(400, {"error": "conta desconhecida"})
                STATE["account"] = body["name"]
                return self.send_json(200, {"ok": True})
            if path == "/api/project":
                if body.get("path") not in PROJECTS:
                    return self.send_json(400, {"error": "projeto desconhecido"})
                STATE["project"] = body["path"]
                return self.send_json(200, {"ok": True})
            if path == "/api/session":
                if body.get("new"):
                    proj = body.get("project")
                    acc = body.get("account")
                    if proj is not None and proj not in PROJECTS:
                        return self.send_json(400, {"error": "projeto desconhecido"})
                    if acc is not None:
                        match = [a for a in accounts() if a["name"] == acc]
                        if not match:
                            return self.send_json(400, {"error": "conta desconhecida"})
                        if not match[0]["usable"]:
                            return self.send_json(400, {"error": "conta indisponível"})
                    NEW_COUNT[0] += 1
                    sid = f"e5f6a7b8-0000-4000-8000-{NEW_COUNT[0]:012d}"
                    TURNS_BY_SID[sid] = 0
                    if proj:
                        STATE["project"] = proj
                    if acc:
                        STATE["account"] = acc
                    NEW_META[sid] = {
                        "project": STATE["project"],
                        "account": STATE["account"],
                    }
                    STATE["session_id"] = sid
                    return self.send_json(
                        200,
                        {"session_id": sid, "last_prompt": None, "last_reply": None},
                    )
                sid = body.get("session_id")
                if sid not in {s["session_id"] for s in sessions()}:
                    return self.send_json(400, {"error": "sessão desconhecida"})
                STATE["session_id"] = sid
                t = all_turns(sid)
                u = [x["text"] for x in t if x["role"] == "user"]
                a = [x["text"] for x in t if x["role"] == "assistant"]
                return self.send_json(
                    200,
                    {
                        "session_id": sid,
                        "last_prompt": u[-1] if u else None,
                        "last_reply": a[-1] if a else None,
                    },
                )
            if path in ("/api/session/use", "/api/session/end"):
                sid = body.get("session_id")
                if sid not in {s["session_id"] for s in sessions()}:
                    return self.send_json(400, {"error": "sessão desconhecida"})
                CALLS.append((path, sid))
                if path == "/api/session/use":
                    STATE["session_id"] = sid
                    return self.send_json(200, {"ok": True, "session_id": sid})
                if sid in RUN:
                    del RUN[sid]
                    outcome = "stopped"
                elif sid in WORKING or sid in PC_OPEN:
                    outcome = "terminated"
                else:
                    outcome = "not_running"
                WORKING.discard(sid)
                PC_OPEN.discard(sid)
                return self.send_json(200, {"ok": True, "outcome": outcome})
            if path == "/api/stop":
                for sid in list(RUN):
                    del RUN[sid]
                    RESULT[sid] = {
                        "text": "Parado pelo usuário",
                        "cost_usd": 0.04,
                        "duration_ms": 3000,
                        "is_error": True,
                    }
                return self.send_json(200, {"ok": True})
        self.send_json(404, {"error": "não encontrado"})


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 28790
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    print(f"dev_server em http://127.0.0.1:{port}/", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
