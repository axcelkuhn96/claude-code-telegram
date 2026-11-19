"""Encerrar sessões (painel e /encerrar), use_session, trabalhando agora (`working`)."""

from __future__ import annotations

import json
import os
import signal
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot import handlers
from bot.usage import Profile
from tests.test_handlers import (
    CTX,
    OWNER,
    SID,
    SID2,
    STRANGER,
    make_bot,
    sent_texts,
    upd,
)

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
PID = 424242
START = "987654"


@pytest.fixture(autouse=True)
def rapido(monkeypatch):
    monkeypatch.setattr(handlers, "SEND_BACKOFF_S", (0.0, 0.0))
    monkeypatch.setattr(handlers, "TERMINATE_WAIT_S", 0.3)


class Proc:
    """Árvore falsa de /proc + registro `sessions/<pid>.json` do Claude Code."""

    def __init__(self, tmp_path):
        self.root = tmp_path / "proc"
        self.root.mkdir()
        self.cfg = tmp_path / "c1"
        (self.cfg / "sessions").mkdir(parents=True)

    def spawn(
        self,
        pid=PID,
        sid=SID,
        cwd="/home/x/proj",
        cmdline=b"/usr/bin/claude\0--resume\0x\0",
        start=START,
    ):
        d = self.root / str(pid)
        d.mkdir()
        rest = ["S"] + ["0"] * 18 + [start]
        (d / "stat").write_text(f"{pid} (claude) " + " ".join(rest) + "\n")
        (d / "cmdline").write_bytes(cmdline)
        (self.cfg / "sessions" / f"{pid}.json").write_text(
            json.dumps(
                {
                    "pid": pid,
                    "sessionId": sid,
                    "cwd": cwd,
                    "procStart": start,
                    "startedAt": int(NOW.timestamp() * 1000),
                    "kind": "interactive",
                }
            )
        )
        return d

    def die(self, pid=PID):
        (self.root / str(pid) / "stat").unlink()


@pytest.fixture
def px(tmp_path):
    p = Proc(tmp_path)
    bot = make_bot(tmp_path, project="/home/x/proj", account="c1", session_id=SID)
    bot.profiles = lambda: [Profile("c1", p.cfg)]
    bot.proc_root = p.root
    bot.now = lambda: NOW
    return bot, p


@pytest.fixture
def kills(monkeypatch):
    sent = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(os, "killpg", lambda *a: sent.append(("killpg", *a)))
    return sent


# ---------- end_session: sessão aberta no PC ----------


async def test_encerra_sessao_do_pc_com_sigterm_e_avisa_no_chat(px, kills):
    bot, p = px
    p.spawn()
    kills_seen = kills

    def kill(pid, sig):
        kills_seen.append((pid, sig))
        p.die()  # o processo morre ao receber o SIGTERM

    os.kill = kill
    res = await bot.end_session(SID)
    assert res == {"ok": True, "outcome": "terminated"}
    assert kills_seen == [(PID, signal.SIGTERM)]
    assert sent_texts(bot)[-1] == f"⏹ sessão {SID[:8]} encerrada (pelo painel)"


async def test_sigterm_ignorado_reporta_sigterm_sent_e_nunca_sigkill(px, kills):
    bot, p = px
    p.spawn()  # segue vivo depois do SIGTERM
    res = await bot.end_session(SID)
    assert res == {"ok": True, "outcome": "sigterm_sent"}
    assert kills == [(PID, signal.SIGTERM)]  # só SIGTERM, uma vez
    assert all(sig != signal.SIGKILL for _, sig in kills)
    assert "SIGTERM enviado" in sent_texts(bot)[-1]


async def test_aceita_claude_via_node(px, kills):
    bot, p = px
    p.spawn(cmdline=b"node\0/home/u/.local/share/claude/cli.js\0--resume\0")
    assert (await bot.end_session(SID))["outcome"] == "sigterm_sent"
    assert kills == [(PID, signal.SIGTERM)]


async def test_aceita_claude_pelo_exe(px, kills):
    bot, p = px
    d = p.spawn(cmdline=b"/proc/self/exe\0")
    os.symlink("/opt/anthropic/claude-code", d / "exe")
    assert (await bot.end_session(SID))["outcome"] == "sigterm_sent"


@pytest.mark.parametrize(
    "cmdline",
    [
        b"bash\0-c\0claude\0",  # o nome aparece, mas não é o executável
        b"/usr/bin/python3\0meu_claude_script.py\0",
        b"sshd\0",
        b"node\0/srv/app.js\0--name=claude-fan\0",
        b"",
    ],
)
async def test_recusa_processo_que_nao_e_o_claude(px, kills, cmdline):
    bot, p = px
    p.spawn(cmdline=cmdline)
    res = await bot.end_session(SID)
    assert res["ok"] is False and "não confere" in res["error"]
    assert kills == []
    assert sent_texts(bot) == []  # nada postado


async def test_recusa_processo_de_outro_uid(px, kills, monkeypatch):
    bot, p = px
    p.spawn()
    monkeypatch.setattr(os, "getuid", lambda: os.stat(p.root).st_uid + 1)
    assert (await bot.end_session(SID))["ok"] is False
    assert kills == []


async def test_recusa_o_proprio_bot_e_pid_1(px, kills):
    bot, p = px
    p.spawn(pid=os.getpid())
    assert (await bot.end_session(SID))["ok"] is False
    p.spawn(pid=1, sid=SID2)
    assert (await bot.end_session(SID2))["ok"] is False
    assert kills == []


async def test_recusa_se_o_pid_foi_reaproveitado_entre_a_checagem_e_o_kill(
    px, kills, monkeypatch
):
    bot, p = px
    p.spawn()
    real = handlers.sessions_mod._proc_starttime
    calls = []

    def starttime(root, pid):
        calls.append(1)
        # list_open (1x) e o t0 (2x) conferem; o recheck logo antes do kill vê outro processo
        return real(root, pid) if len(calls) <= 2 else "111"

    monkeypatch.setattr(handlers.sessions_mod, "_proc_starttime", starttime)
    res = await bot.end_session(SID)
    assert res["ok"] is False and kills == []


async def test_registro_com_procstart_diferente_nao_e_sessao_aberta(px, kills):
    bot, p = px
    p.spawn()
    reg = p.cfg / "sessions" / f"{PID}.json"
    data = json.loads(reg.read_text())
    data["procStart"] = "1"  # pid reaproveitado por outro processo
    reg.write_text(json.dumps(data))
    assert await bot.end_session(SID) == {"ok": True, "outcome": "not_running"}
    assert kills == []


async def test_sem_registro_not_running_sem_post(px, kills):
    bot, _ = px
    assert await bot.end_session(SID) == {"ok": True, "outcome": "not_running"}
    assert kills == [] and sent_texts(bot) == []


async def test_kill_com_processo_ja_morto_ou_sem_permissao(px, monkeypatch):
    bot, p = px
    p.spawn()

    def gone(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(os, "kill", gone)
    assert (await bot.end_session(SID))["outcome"] == "terminated"

    def denied(pid, sig):
        raise PermissionError

    monkeypatch.setattr(os, "kill", denied)
    assert (await bot.end_session(SID))["ok"] is False


async def test_sessao_invalida(px, kills):
    bot, _ = px
    for bad in ("lixo", "../x", None, 5):
        assert await bot.end_session(bad) == {"ok": False, "error": "sessão inválida"}


# ---------- end_session: execução do bot ----------


async def test_execucao_do_bot_e_parada_e_avisada(px, kills):
    bot, _ = px
    run = MagicMock(stop=AsyncMock())
    bot.runs.get = lambda sid: run
    bot.runs.busy = lambda sid: True
    assert await bot.end_session(SID) == {"ok": True, "outcome": "stopped"}
    run.stop.assert_awaited_once()
    assert SID in bot._stopped and kills == []  # filho do bot: sem os.kill direto
    assert sent_texts(bot)[-1] == f"⏹ sessão {SID[:8]} encerrada (pelo painel)"


async def test_bot_rodando_tem_prioridade_sobre_o_registro(px, kills):
    """O `claude -p` do bot também aparece no registro: é parado como execução do bot."""
    bot, p = px
    p.spawn()
    run = MagicMock(stop=AsyncMock())
    bot.runs.get = lambda sid: run
    bot.runs.busy = lambda sid: True
    assert (await bot.end_session(SID))["outcome"] == "stopped"
    assert kills == []


async def test_trancado_recusa(px, kills):
    bot, p = px
    p.spawn()
    bot.totp_secret = "ABCDEFGHIJKLMNOP"
    assert await bot.end_session(SID) == {"ok": False, "error": "locked"}
    assert kills == []


# ---------- /encerrar ----------


async def test_encerrar_pede_confirmacao_e_so_age_no_toque(px, kills):
    bot, p = px
    p.spawn()
    await bot.cmd_encerrar(upd(), CTX)
    assert kills == []  # só perguntou
    sent = bot.tg.send_message.call_args
    assert f"Encerrar a sessão {SID[:8]}" in sent.args[1] and "SIGTERM" in sent.args[1]
    rows = sent.kwargs["reply_markup"].inline_keyboard[0]
    assert [b.text for b in rows] == ["⏹ Encerrar", "Cancelar"]
    assert (
        rows[0].callback_data == f"e:{SID}"
        and len(rows[0].callback_data.encode()) <= 64
    )
    await bot.on_callback(upd(data="e:x"), CTX)
    assert sent_texts(bot)[-1] == "Cancelado." and kills == []
    await bot.on_callback(upd(data=rows[0].callback_data), CTX)
    assert kills == [(PID, signal.SIGTERM)]
    assert any("(pelo chat)" in t or "SIGTERM enviado" in t for t in sent_texts(bot))


async def test_encerrar_confirmado_chat_diz_pelo_chat(px, kills):
    bot, p = px
    run = MagicMock(stop=AsyncMock())
    bot.runs.get = lambda sid: run
    bot.runs.busy = lambda sid: True
    await bot.on_callback(upd(data=f"e:{SID}"), CTX)
    assert sent_texts(bot)[-1] == f"⏹ sessão {SID[:8]} encerrada (pelo chat)"


async def test_encerrar_nao_rodando_e_recusado_e_sem_sessao(px, kills):
    bot, p = px
    await bot.on_callback(upd(data=f"e:{SID2}"), CTX)
    assert "não está rodando" in sent_texts(bot)[-1]
    p.spawn(cmdline=b"bash\0")
    await bot.on_callback(upd(data=f"e:{SID}"), CTX)
    assert sent_texts(bot)[-1].startswith("❌") and kills == []
    bot.state.session_id = None
    await bot.cmd_encerrar(upd(), CTX)
    assert "Sem sessão ativa" in sent_texts(bot)[-1]


async def test_encerrar_callbacks_invalidos_e_estranho(px, kills):
    bot, p = px
    p.spawn()
    n = len(sent_texts(bot))
    for bad in ("e:", "e:nao-e-uuid", "e:../../x", f"e:{SID}x"):
        await bot.on_callback(upd(data=bad), CTX)
    assert len(sent_texts(bot)) == n and kills == []
    await bot.cmd_encerrar(upd(user=STRANGER), CTX)
    await bot.on_callback(upd(user=STRANGER, data=f"e:{SID}"), CTX)
    assert len(sent_texts(bot)) == n and kills == []


async def test_encerrar_trancado(px, kills):
    bot, p = px
    p.spawn()
    bot.totp_secret = "ABCDEFGHIJKLMNOP"
    await bot.cmd_encerrar(upd(), CTX)
    await bot.on_callback(upd(data=f"e:{SID}"), CTX)
    assert set(sent_texts(bot)) == {"🔒 trancado — /destravar <código>"} and kills == []


# ---------- use_session ----------


async def test_use_session_de_sessao_do_pc_usa_projeto_e_conta_do_registro(
    px, monkeypatch
):
    bot, p = px
    bot.profiles = lambda: [Profile("c1", p.cfg), Profile("c2", p.cfg.parent / "c2")]
    p.spawn(sid=SID2, cwd="/home/x/outro")
    # a sessão está registrada na pasta da conta c1
    monkeypatch.setattr(
        handlers.sessions_mod, "session_file", lambda *a: None, raising=False
    )
    bot.state.project, bot.state.account = "/home/x/proj", "c2"
    assert await bot.use_session(SID2) == {"ok": True}
    assert (bot.state.project, bot.state.account, bot.state.session_id) == (
        "/home/x/outro",
        "c1",
        SID2,
    )
    assert (
        sent_texts(bot)[0]
        == f"🔵 ativa: outro · c1 · {SID2[:8]} — mande a mensagem aqui"
    )


async def test_use_session_erros(px):
    bot, _ = px
    assert await bot.use_session("../x") == {"error": "sessão inválida"}
    assert await bot.use_session(SID2) == {"error": "sessão não encontrada"}
    assert sent_texts(bot) == []


# ---------- trabalhando agora ----------


def jsonl_at(tmp_path, sid, seconds_ago):
    f = tmp_path / f"{sid}.jsonl"
    f.write_text("{}\n")
    t = (NOW - timedelta(seconds=seconds_ago)).timestamp()
    os.utime(f, (t, t))
    return f


async def test_working_pc_so_se_o_jsonl_mexeu_ha_20s_ou_menos(
    px, tmp_path, monkeypatch
):
    bot, p = px
    p.spawn(sid=SID, pid=111)
    p.spawn(sid=SID2, pid=222, cwd="/home/x/outro")
    files = {SID: jsonl_at(tmp_path, SID, 5), SID2: jsonl_at(tmp_path, SID2, 60)}
    monkeypatch.setattr(
        handlers.sessions_mod,
        "session_file",
        lambda cwd, sid, profs: files[sid],
        raising=False,
    )
    monkeypatch.setattr(
        handlers.sessions_mod,
        "last_prompt",
        lambda path, limit=60: "faz",
        raising=False,
    )
    out = {s["session_id"]: s for s in await bot.api_sessions("open")}
    assert (
        out[SID]["working"] is True
        and out[SID]["pc"] is True
        and out[SID]["age_s"] == 5
    )
    assert out[SID2]["working"] is False and out[SID2]["age_s"] == 60
    state = await bot.api_state()
    assert [w["session_id"] for w in state["working"]] == [SID]
    w = state["working"][0]
    assert w["short"] == SID[:8] and w["pc"] is True and w["project_label"] == "proj"
    assert w["since"] == (NOW - timedelta(seconds=5)).isoformat()
    json.dumps(state)


async def test_working_no_limite_de_20s(px, tmp_path, monkeypatch):
    bot, p = px
    p.spawn(sid=SID, pid=111)
    for secs, esperado in ((20, True), (21, False)):
        f = jsonl_at(tmp_path, SID, secs)
        monkeypatch.setattr(
            handlers.sessions_mod, "session_file", lambda *a, f=f: f, raising=False
        )
        monkeypatch.setattr(
            handlers.sessions_mod,
            "last_prompt",
            lambda path, limit=60: "",
            raising=False,
        )
        out = (await bot.api_sessions("open"))[0]
        assert out["working"] is esperado


async def test_working_execucao_do_bot_vale_mesmo_com_arquivo_velho(
    px, tmp_path, monkeypatch
):
    bot, p = px
    bot.runs.running = lambda: [SID2]
    bot.bot_sessions[SID2] = SimpleNamespace(
        session_id=SID2,
        account="c1",
        cwd="/home/x/bot",
        mtime=NOW - timedelta(minutes=3),
        first_prompt="p",
    )
    monkeypatch.setattr(
        handlers.sessions_mod,
        "session_file",
        lambda *a: jsonl_at(tmp_path, SID2, 600),
        raising=False,
    )
    monkeypatch.setattr(
        handlers.sessions_mod, "last_prompt", lambda path, limit=60: "p", raising=False
    )
    items = await bot.api_sessions("open")
    assert (
        items[0]["working"] is True
        and items[0]["running"] is True
        and items[0]["pc"] is False
    )
    state = await bot.api_state()
    assert (
        state["working"][0]["pc"] is False
        and state["working"][0]["project_label"] == "bot"
    )


async def test_working_vazio_sem_sessoes_abertas(px):
    bot, _ = px
    assert (await bot.api_state())["working"] == []


async def test_varredura_de_arquivos_nao_trava_o_loop(px, tmp_path, monkeypatch):
    import asyncio
    import time

    bot, p = px
    p.spawn()

    def lento(*a):
        time.sleep(0.3)
        return None

    monkeypatch.setattr(handlers.sessions_mod, "session_file", lento, raising=False)
    ticks = []

    async def ticker():
        for _ in range(5):
            await asyncio.sleep(0.02)
            ticks.append(1)

    async def varre():
        await bot.api_sessions("open")
        await bot.api_state()
        ticks.append("fim")

    await asyncio.gather(varre(), ticker())
    assert (
        ticks[:5] == [1] * 5
    )  # o ticker rodou inteiro enquanto a varredura bloqueava a thread
