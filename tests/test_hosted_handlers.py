"""Sessões hospedadas nos handlers: tmux falso + .jsonl sintético (modelado nos reais)."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from bot import handlers, rc
from bot.sessions import encode_cwd
from bot.usage import Profile
from tests.test_handlers import CTX, OWNER, make_bot, sent_texts, upd
from tests.test_hosted import PANE_ASK, PANE_BUSY, PANE_READY, URL

SID = "2964a863-5ebb-4e2c-9004-2f3ce15a55f9"
NAME = "cc-2964a863"


@pytest.fixture(autouse=True)
def rapido(monkeypatch):
    monkeypatch.setattr(handlers, "SEND_BACKOFF_S", (0.0, 0.0))
    for name, value in {
        "HOST_POLL_S": 0.01,
        "HOST_IDLE_S": 0.05,
        "HOST_PROBE_EVERY_S": 0.02,
        "HOST_END_IDLE_S": 0.1,
        "HOST_ALIVE_EVERY_S": 0.02,
        "HOST_READY_S": 1.0,
        "HOST_READY_POLL_S": 0.01,
        "HOST_BIND_TIMEOUT_S": 5.0,
        "HOST_KEY_SETTLE_S": 0.0,
        "HOST_TYPE_PAUSE_S": 0.0,
        "HOST_INTERRUPT_SETTLE_S": 0.0,
        "PROGRESS_INTERVAL_S": 0.0,
    }.items():
        monkeypatch.setattr(handlers, name, value)


def j_user(text, kind="human"):
    return {
        "type": "user",
        "isSidechain": False,
        "message": {"role": "user", "content": text},
        "origin": {"kind": kind},
        "promptSource": "typed",
    }


def j_assistant(*blocks, stop="end_turn"):
    return {
        "type": "assistant",
        "isSidechain": False,
        "message": {"stop_reason": stop, "content": list(blocks)},
    }


def j_text(t):
    return {"type": "text", "text": t}


def j_tool(**inp):
    return {"type": "tool_use", "id": "t1", "name": "Bash", "input": inp}


END = {"type": "system", "subtype": "stop_hook_summary", "isSidechain": False}
BRIDGE = {
    "type": "system",
    "subtype": "bridge_status",
    "url": URL,
    "isSidechain": False,
}


class FakeTmux:
    def __init__(self, jsonl: Path):
        self.sessions: dict[str, dict] = {}
        self.pane = PANE_READY
        self.calls: list[tuple] = []
        self.jsonl = jsonl
        self.on_submit = None
        self.buffer = ""
        self.start_lines = [BRIDGE]
        self.start_ok = True
        self.pane_after_submit = None

    def append(self, lines):
        self.jsonl.parent.mkdir(parents=True, exist_ok=True)
        with open(self.jsonl, "a", encoding="utf-8") as f:
            for d in lines:
                f.write(json.dumps(d) + "\n")

    def start(self, name, project, env, argv):
        self.calls.append(("start", name, project, dict(env), list(argv)))
        if not self.start_ok:
            return False, "falhou"
        self.sessions[name] = dict(env)
        self.append(self.start_lines)
        return True, ""

    def exists(self, name):
        return name in self.sessions

    def names(self):
        return list(self.sessions)

    def env(self, name):
        return self.sessions.get(name, {})

    def capture(self, name):
        return self.pane

    def paste(self, name, text):
        self.calls.append(("paste", text))
        self.buffer += text
        return True

    def keys(self, name, *keys, literal=False):
        self.calls.append(("keys", keys, literal))
        if keys == ("Enter",) and self.buffer:
            text, self.buffer = self.buffer, ""
            if self.pane_after_submit is not None:
                self.pane = self.pane_after_submit
            if self.on_submit is not None:
                self.append(self.on_submit(text))
        return True

    def kill(self, name):
        self.calls.append(("kill", name))
        self.sessions.pop(name, None)
        return True

    def keys_sent(self):
        return [c[1] for c in self.calls if c[0] == "keys"]


def setup(tmp_path, monkeypatch, trusted=True, account="personal"):
    monkeypatch.setenv("RC_ACCOUNTS", "personal")
    project = tmp_path / "cbs-lab"
    project.mkdir()
    cfg3 = tmp_path / "cfg3"
    cfg3.mkdir()
    (tmp_path / "c1").mkdir()
    (cfg3 / ".claude.json").write_text(
        json.dumps({"projects": {str(project): {"hasTrustDialogAccepted": trusted}}})
    )
    bot = make_bot(tmp_path, account=account, project=str(project), session_id=SID)
    bot.profiles = lambda: [
        Profile("personal", cfg3),
        Profile("claude", tmp_path / "c1"),
    ]
    jsonl = cfg3 / "projects" / encode_cwd(str(project)) / f"{SID}.jsonl"
    fake = FakeTmux(jsonl)
    bot.tmux = fake
    return bot, fake, str(project)


async def until(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.01)
    return False


async def close(bot):
    await bot.shutdown()


def reply_ok(text):
    return [
        {"type": "file-history-snapshot"},
        j_user(text),
        j_assistant(j_tool(command="ls"), stop="tool_use"),
        {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "content": "x"}]},
        },
        j_assistant(j_text("OK")),
        END,
        {"type": "system", "subtype": "turn_duration", "durationMs": 1200},
    ]


# ---------- fluxo principal ----------


async def test_prompt_sobe_o_tmux_cola_e_a_resposta_volta(tmp_path, monkeypatch):
    bot, fake, project = setup(tmp_path, monkeypatch)
    fake.on_submit = reply_ok
    await asyncio.wait_for(bot.run_prompt(OWNER, "responda só OK"), 5)
    start = next(c for c in fake.calls if c[0] == "start")
    _, name, cwd, env, argv = start
    assert name == NAME and cwd == project
    assert argv[1:] == [
        "--dangerously-skip-permissions",
        f"--remote-control={handlers.hosted_mod.rc_session_name(project)}",
        "--session-id",
        SID,
        "--mcp-config",
        str(handlers.MCP_CONFIG),
    ]
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "cfg3")
    assert env["CLAUDE_SESSION_ID"] == SID
    assert env["CLAUDE_TG_PROJECT"] == project and env["CLAUDE_TG_ACCOUNT"] == "personal"
    assert not any("TOKEN" in k or "SECRET" in k or "OPENAI" in k for k in env)
    assert ("paste", "responda só OK") in fake.calls
    assert ("Enter",) in fake.keys_sent()
    texts = sent_texts(bot)
    assert any(t.startswith("OK") for t in texts)
    assert any("sessão também no claude.ai" in t and URL in t for t in texts)
    edits = [c.args[0] for c in bot.tg.edit_message_text.call_args_list]
    assert any("Bash: ls" in e for e in edits)
    assert bot.state.interrupted == []
    assert not bot.runs.busy(SID) and SID in bot.hosts
    await close(bot)


async def test_segunda_mensagem_reusa_o_tmux_e_retoma(tmp_path, monkeypatch):
    bot, fake, _ = setup(tmp_path, monkeypatch)
    fake.on_submit = reply_ok
    await asyncio.wait_for(bot.run_prompt(OWNER, "um"), 5)
    await asyncio.wait_for(bot.run_prompt(OWNER, "dois"), 5)
    assert [c[0] for c in fake.calls].count("start") == 1
    assert sum(1 for t in sent_texts(bot) if t.startswith("OK")) == 2
    await close(bot)


async def test_sessao_existente_usa_resume(tmp_path, monkeypatch):
    bot, fake, _ = setup(tmp_path, monkeypatch)
    fake.append([j_user("antigo"), j_assistant(j_text("velho")), END])
    fake.on_submit = reply_ok
    await asyncio.wait_for(bot.run_prompt(OWNER, "oi"), 5)
    argv = next(c for c in fake.calls if c[0] == "start")[4]
    assert "--resume" in argv and "--session-id" not in argv
    assert not any(
        "velho" in t for t in sent_texts(bot)
    )  # só o que veio depois do offset
    await close(bot)


async def test_ocupada_enquanto_o_turno_corre(tmp_path, monkeypatch):
    bot, fake, _ = setup(tmp_path, monkeypatch)
    fake.on_submit = lambda text: [
        j_user(text),
        j_assistant(j_tool(command="sleep 9"), stop="tool_use"),
    ]
    fake.pane_after_submit = PANE_BUSY
    task = asyncio.create_task(bot.run_prompt(OWNER, "longo"))
    assert await until(
        lambda: bot.runs.busy(SID) and SID in bot.hosts and bot.hosts[SID].turn_ours
    )
    await bot.run_prompt(OWNER, "outro")
    assert "⏳ Sessão ocupada; use /parar para cancelar." in sent_texts(bot)
    fake.append([j_assistant(j_text("pronto")), END])
    await asyncio.wait_for(task, 5)
    assert any(t.startswith("pronto") for t in sent_texts(bot))
    await close(bot)


async def test_nao_confiado_avisa_e_nao_sobe(tmp_path, monkeypatch):
    bot, fake, _ = setup(tmp_path, monkeypatch, trusted=False)
    await bot.run_prompt(OWNER, "oi")
    assert fake.calls == []
    assert any("projeto não confiado na personal" in t for t in sent_texts(bot))
    await close(bot)


async def test_tmux_falhou_avisa(tmp_path, monkeypatch):
    bot, fake, _ = setup(tmp_path, monkeypatch)
    fake.start_ok = False
    await bot.run_prompt(OWNER, "oi")
    assert "❌ Falha ao iniciar a sessão hospedada (tmux)." in sent_texts(bot)
    assert SID not in bot.hosts
    await close(bot)


async def test_tui_que_nao_fica_pronto_avisa(tmp_path, monkeypatch):
    monkeypatch.setattr(handlers, "HOST_READY_S", 0.05)
    bot, fake, _ = setup(tmp_path, monkeypatch)
    fake.pane = "carregando…"
    await bot.run_prompt(OWNER, "oi")
    assert any("não ficou pronta" in t for t in sent_texts(bot))
    assert not any(c[0] == "paste" for c in fake.calls)
    await close(bot)


async def test_conta_sem_rc_continua_no_claude_p(tmp_path, monkeypatch):
    bot, fake, _ = setup(tmp_path, monkeypatch, account="claude")
    launched = []

    async def launch(sid, args, cwd, env=None):
        launched.append(args)
        raise OSError("fake")

    bot.runs.launch = launch
    await bot.run_prompt(OWNER, "oi")
    assert launched and launched[0][1] == "-p"
    assert fake.calls == []
    await close(bot)


async def test_trocar_para_conta_sem_rc_encerra_o_tmux(tmp_path, monkeypatch):
    bot, fake, _ = setup(tmp_path, monkeypatch)
    fake.on_submit = reply_ok
    await asyncio.wait_for(bot.run_prompt(OWNER, "oi"), 5)
    bot.state.account = "claude"

    async def launch(sid, args, cwd, env=None):
        raise OSError("fake")

    bot.runs.launch = launch
    await bot.run_prompt(OWNER, "agora no -p")
    assert ("kill", NAME) in fake.calls and SID not in bot.hosts
    await close(bot)


# ---------- o outro lado: claude.ai ----------


async def test_mensagem_digitada_no_claude_ai_vai_ao_telegram(tmp_path, monkeypatch):
    bot, fake, _ = setup(tmp_path, monkeypatch)
    fake.on_submit = reply_ok
    await asyncio.wait_for(bot.run_prompt(OWNER, "oi"), 5)
    fake.append(
        [
            j_user("pergunta feita no navegador"),
            j_assistant(j_text("resposta para o navegador")),
            END,
        ]
    )
    assert await until(
        lambda: any("resposta para o navegador" in t for t in sent_texts(bot))
    )
    texts = sent_texts(bot)
    assert "🛰 (via claude.ai) 👤 pergunta feita no navegador" in texts
    assert any(t.startswith("🛰 resposta para o navegador") for t in texts)
    await close(bot)


async def test_notificacao_de_tarefa_nao_vira_prompt_mas_a_resposta_vai(
    tmp_path, monkeypatch
):
    bot, fake, _ = setup(tmp_path, monkeypatch)
    fake.on_submit = reply_ok
    await asyncio.wait_for(bot.run_prompt(OWNER, "oi"), 5)
    fake.append(
        [
            j_user(
                "<task-notification><task-id>b1</task-id>", kind="task-notification"
            ),
            j_assistant(j_text("tarefa em segundo plano acabou")),
            END,
        ]
    )
    assert await until(
        lambda: any("tarefa em segundo plano" in t for t in sent_texts(bot))
    )
    assert not any("via claude.ai" in t for t in sent_texts(bot))
    await close(bot)


async def test_sessao_nao_ativa_leva_prefixo(tmp_path, monkeypatch):
    bot, fake, _ = setup(tmp_path, monkeypatch)
    fake.on_submit = reply_ok
    await asyncio.wait_for(bot.run_prompt(OWNER, "oi"), 5)
    bot.state.session_id = "33333333-3333-4333-8333-333333333333"
    fake.append([j_user("de lá"), j_assistant(j_text("resp")), END])
    assert await until(
        lambda: any(
            "resp" in t and "[cbs-lab · 2964a863]" in t for t in sent_texts(bot)
        )
    )
    await close(bot)


# ---------- diálogos do TUI ----------


async def _turno_com_dialogo(tmp_path, monkeypatch):
    bot, fake, _ = setup(tmp_path, monkeypatch)
    fake.on_submit = lambda text: [
        j_user(text),
        j_assistant(
            {"type": "tool_use", "id": "t9", "name": "AskUserQuestion", "input": {}},
            stop="tool_use",
        ),
    ]
    task = asyncio.create_task(bot.run_prompt(OWNER, "pergunte uma cor"))
    assert await until(lambda: SID in bot.hosts and bot.hosts[SID].turn_ours)
    fake.pane = PANE_ASK
    assert await until(lambda: bot.hosts[SID].dialog is not None)
    return bot, fake, task


def dialog_markup(bot):
    for c in bot.tg.send_message.call_args_list:
        if "o terminal pergunta" in c.args[1]:
            return c.kwargs["reply_markup"].inline_keyboard
    return None


async def test_dialogo_vira_mensagem_com_botoes_uma_vez(tmp_path, monkeypatch):
    bot, _, task = await _turno_com_dialogo(tmp_path, monkeypatch)
    await asyncio.sleep(0.2)  # várias sondagens: nada de spam
    msgs = [t for t in sent_texts(bot) if "o terminal pergunta" in t]
    assert len(msgs) == 1
    assert "Qual cor você escolhe?" in msgs[0] and "1. Vermelho" in msgs[0]
    kb = dialog_markup(bot)
    labels = [b.text for row in kb for b in row]
    assert labels == ["1", "2", "3", "4", "5", "↵ Enter", "⎋ Esc"]
    for row in kb:
        for b in row:
            assert len(b.callback_data.encode()) <= 64
    task.cancel()
    await close(bot)


async def test_botao_manda_o_digito_e_sem_enter_quando_o_dialogo_some(
    tmp_path, monkeypatch
):
    bot, fake, task = await _turno_com_dialogo(tmp_path, monkeypatch)
    data = dialog_markup(bot)[0][1].callback_data  # "2"
    orig = fake.keys

    def keys(name, *k, literal=False):
        if k == ("2",):
            fake.pane = PANE_BUSY  # o TUI aceitou a escolha
        return orig(name, *k, literal=literal)

    fake.keys = keys
    await bot.on_callback(upd(data=data), CTX)
    sent = fake.keys_sent()
    assert sent[-1] == ("2",)
    assert bot.hosts[SID].dialog is None
    await bot.on_callback(upd(data=data), CTX)
    assert "Esse diálogo já foi encerrado." in sent_texts(bot)
    fake.append([j_assistant(j_text("Verde")), END])
    await asyncio.wait_for(task, 5)
    assert any(t.startswith("Verde") for t in sent_texts(bot))
    await close(bot)


async def test_digito_que_so_move_o_cursor_ganha_enter(tmp_path, monkeypatch):
    bot, fake, task = await _turno_com_dialogo(tmp_path, monkeypatch)
    data = dialog_markup(bot)[0][2].callback_data  # "3"
    await bot.on_callback(upd(data=data), CTX)
    assert fake.keys_sent()[-2:] == [("3",), ("Enter",)]
    task.cancel()
    await close(bot)


async def test_esc_e_enter(tmp_path, monkeypatch):
    bot, fake, task = await _turno_com_dialogo(tmp_path, monkeypatch)
    esc = dialog_markup(bot)[-1][1].callback_data
    await bot.on_callback(upd(data=esc), CTX)
    assert fake.keys_sent()[-1] == ("Escape",)
    task.cancel()
    await close(bot)


async def test_texto_livre_responde_o_dialogo(tmp_path, monkeypatch):
    bot, fake, task = await _turno_com_dialogo(tmp_path, monkeypatch)
    await bot.handle_text(OWNER, "papagaio", None)
    seq = [c for c in fake.calls if c[0] in ("keys", "paste")][-3:]
    assert seq == [
        ("keys", ("4",), True),
        ("paste", "papagaio"),
        ("keys", ("Enter",), False),
    ]
    assert "↩️ respondido no terminal" in sent_texts(bot)
    assert bot.hosts[SID].dialog is None
    task.cancel()
    await close(bot)


async def test_botao_texto_livre_pede_texto(tmp_path, monkeypatch):
    bot, fake, task = await _turno_com_dialogo(tmp_path, monkeypatch)
    data = dialog_markup(bot)[0][3].callback_data  # "4. Type something."
    await bot.on_callback(upd(data=data), CTX)
    assert fake.keys_sent()[-1] == ("4",)
    assert any(t.startswith("✍️ Digite sua resposta") for t in sent_texts(bot))
    await bot.handle_text(OWNER, "roxo", None)
    seq = [c for c in fake.calls if c[0] in ("keys", "paste")][-2:]
    assert seq == [
        ("paste", "roxo"),
        ("keys", ("Enter",), False),
    ]  # sem repetir o dígito
    task.cancel()
    await close(bot)


# ---------- parar / encerrar / morte / readoção ----------


async def test_parar_manda_esc_e_fecha_o_turno(tmp_path, monkeypatch):
    bot, fake, _ = setup(tmp_path, monkeypatch)
    fake.on_submit = lambda text: [
        j_user(text),
        j_assistant(j_tool(command="sleep 99"), stop="tool_use"),
    ]
    fake.pane_after_submit = PANE_BUSY
    task = asyncio.create_task(bot.run_prompt(OWNER, "longo"))
    assert await until(lambda: SID in bot.hosts and bot.hosts[SID].turn_ours)
    await bot.cmd_parar(upd(text="/parar"), CTX)
    await asyncio.wait_for(task, 5)
    assert ("Escape",) in fake.keys_sent()
    assert any("⏹ Execução interrompida." in t for t in sent_texts(bot))
    assert not bot.runs.busy(SID)
    fake.append(
        [
            {
                "type": "user",
                "message": {"content": "[Request interrupted by user for tool use]"},
            }
        ]
    )
    await asyncio.sleep(0.1)
    assert not bot.runs.busy(SID)
    await close(bot)


async def test_encerrar_mata_a_sessao_tmux(tmp_path, monkeypatch):
    bot, fake, _ = setup(tmp_path, monkeypatch)
    fake.on_submit = reply_ok
    await asyncio.wait_for(bot.run_prompt(OWNER, "oi"), 5)
    res = await bot._end_session(SID, "chat")
    assert res == {"ok": True, "outcome": "stopped"}
    assert ("kill", NAME) in fake.calls and SID not in bot.hosts
    assert bot.runs.get(SID) is None
    await close(bot)


async def test_tmux_que_morre_avisa_e_fecha_o_turno(tmp_path, monkeypatch):
    bot, fake, _ = setup(tmp_path, monkeypatch)
    fake.on_submit = lambda text: [j_user(text)]
    task = asyncio.create_task(bot.run_prompt(OWNER, "oi"))
    assert await until(lambda: SID in bot.hosts and bot.hosts[SID].turn_ours)
    fake.sessions.clear()
    await asyncio.wait_for(task, 5)
    texts = sent_texts(bot)
    assert any("a sessão hospedada (tmux) encerrou" in t for t in texts)
    assert any("⏹ sessão hospedada 2964a863" in t for t in texts)
    assert SID not in bot.hosts
    await close(bot)


async def test_prompt_que_nao_chega_ao_jsonl_vira_erro(tmp_path, monkeypatch):
    monkeypatch.setattr(handlers, "HOST_BIND_TIMEOUT_S", 0.1)
    bot, fake, _ = setup(tmp_path, monkeypatch)
    fake.on_submit = lambda text: []
    await asyncio.wait_for(bot.run_prompt(OWNER, "oi"), 5)
    assert any("o terminal não recebeu a mensagem" in t for t in sent_texts(bot))
    assert not bot.runs.busy(SID)
    await close(bot)


async def test_fim_inferido_pelo_painel_pronto(tmp_path, monkeypatch):
    bot, fake, _ = setup(tmp_path, monkeypatch)
    fake.on_submit = lambda text: [j_user(text), j_assistant(j_text("sem marcador"))]
    await asyncio.wait_for(bot.run_prompt(OWNER, "oi"), 5)
    assert any(t.startswith("sem marcador") for t in sent_texts(bot))
    await close(bot)


async def test_readota_no_start_e_nao_posta_interrompido(tmp_path, monkeypatch):
    bot, fake, project = setup(tmp_path, monkeypatch)
    fake.sessions[NAME] = {
        "CLAUDE_SESSION_ID": SID,
        "CLAUDE_TG_PROJECT": project,
        "CLAUDE_TG_ACCOUNT": "personal",
        "CLAUDE_CONFIG_DIR": str(tmp_path / "cfg3"),
    }
    fake.sessions["cc-0000aaaa"] = {"CLAUDE_SESSION_ID": "lixo"}
    fake.append([BRIDGE, j_user("antes"), j_assistant(j_text("antigo")), END])
    bot.state.interrupted = [
        {
            "id": "ab",
            "session_id": SID,
            "project": project,
            "account": "personal",
            "prompt": "p",
            "ts": time.time(),
            "posted": False,
        }
    ]
    assert await bot.adopt_hosts() == 1
    host = bot.hosts[SID]
    assert host.ready and host.rc_url == URL and host.url_announced
    await bot.post_interrupted()
    assert bot.state.interrupted == []
    assert not any("interrompido" in t for t in sent_texts(bot))
    fake.append([j_user("depois", kind="human"), j_assistant(j_text("novo")), END])
    assert await until(lambda: any(t.startswith("🛰 novo") for t in sent_texts(bot)))
    assert not any("antigo" in t for t in sent_texts(bot))
    await close(bot)


async def test_shutdown_nao_interrompe_a_sessao_hospedada(tmp_path, monkeypatch):
    bot, fake, _ = setup(tmp_path, monkeypatch)
    fake.on_submit = lambda text: [
        j_user(text),
        j_assistant(j_tool(command="x"), stop="tool_use"),
    ]
    fake.pane_after_submit = PANE_BUSY
    task = asyncio.create_task(bot.run_prompt(OWNER, "longo"))
    assert await until(lambda: SID in bot.hosts and bot.hosts[SID].turn_ours)
    await bot.shutdown()
    task.cancel()
    assert ("Escape",) not in fake.keys_sent()
    assert not any(c[0] == "kill" for c in fake.calls)


# ---------- /rc, /status e o servidor avulso ----------


async def test_rc_lista_a_sessao_hospedada(tmp_path, monkeypatch):
    bot, fake, _ = setup(tmp_path, monkeypatch)
    fake.on_submit = reply_ok
    await asyncio.wait_for(bot.run_prompt(OWNER, "oi"), 5)
    monkeypatch.setattr(rc, "status", lambda **_: [])
    bot.tg.send_message.reset_mock()
    await bot.cmd_rc(upd(text="/rc"), CTX)
    text = sent_texts(bot)[0]
    rc_name = handlers.hosted_mod.rc_session_name(str(tmp_path / "cbs-lab"))
    assert "Sessões hospedadas" in text and "cbs-lab · " in text and rc_name in text
    assert f'<a href="{URL}">abrir</a>' in text
    assert "Nenhum servidor Remote Control rodando." in text
    await close(bot)


async def test_hospedada_para_o_servidor_avulso_do_mesmo_projeto(tmp_path, monkeypatch):
    bot, fake, project = setup(tmp_path, monkeypatch)
    unit = rc.unit_name("personal", project) + ".service"
    items = [
        {
            "unit": unit,
            "account": "personal",
            "project": project,
            "active": True,
            "env_url": None,
            "session_urls": [],
            "capacity": None,
        },
        {
            "unit": "claude-rc-personal-outro-abcdef.service",
            "account": "personal",
            "project": "/p/outro",
            "active": True,
            "env_url": None,
            "session_urls": [],
            "capacity": None,
        },
    ]
    stops = []
    monkeypatch.setattr(rc, "status", lambda **_: items)
    monkeypatch.setattr(rc, "stop", lambda u, **_: stops.append(u) or True)
    fake.on_submit = reply_ok
    await asyncio.wait_for(bot.run_prompt(OWNER, "oi"), 5)
    assert stops == [unit]
    await close(bot)


async def test_status_conta_hospedadas(tmp_path, monkeypatch):
    bot, fake, _ = setup(tmp_path, monkeypatch)
    fake.on_submit = reply_ok
    await asyncio.wait_for(bot.run_prompt(OWNER, "oi"), 5)

    async def usage():
        return []

    bot.usage = usage
    await bot.cmd_status(upd(text="/status"), CTX)
    assert "🛰 RC: 1 hospedada(s) · 0 servidor(es)" in sent_texts(bot)[-1]
    await close(bot)


# ---------- paridade com o `claude -p` ----------


async def test_limite_da_conta_no_tui_oferece_trocar(tmp_path, monkeypatch):
    from tests.test_handlers import usage_item

    bot, fake, _ = setup(tmp_path, monkeypatch)
    fake.on_submit = lambda text: [
        j_user(text),
        {
            "type": "assistant",
            "isSidechain": False,
            "isApiErrorMessage": True,
            "error": "rate_limit",
            "message": {
                "stop_reason": "stop_sequence",
                "content": [
                    j_text(
                        "You've hit your weekly limit · resets 6pm (America/Sao_Paulo)"
                    )
                ],
            },
        },
    ]

    async def usage():
        return [usage_item("personal", five=100.0), usage_item("claude", five=10.0)]

    bot.usage = usage
    await asyncio.wait_for(bot.run_prompt(OWNER, "oi"), 5)
    texts = sent_texts(bot)
    assert any(t.startswith("❌ You've hit your weekly limit") for t in texts)
    assert "⚠️ conta personal esgotada. Trocar de conta mantém a sessão:" in texts
    kb = bot.tg.send_message.call_args_list[-1].kwargs["reply_markup"].inline_keyboard
    assert kb[0][0].text.startswith("continuar na claude")
    await close(bot)


async def test_trocar_conta_encerra_o_tmux_vai_ao_p_e_volta_re_hospeda(
    tmp_path, monkeypatch
):
    bot, fake, _ = setup(tmp_path, monkeypatch)
    fake.on_submit = reply_ok
    await asyncio.wait_for(bot.run_prompt(OWNER, "oi"), 5)
    err, _, with_session = bot.apply_account("claude")
    assert err is None and with_session
    assert await until(lambda: ("kill", NAME) in fake.calls and SID not in bot.hosts)
    launched = []

    async def launch(sid, args, cwd, env=None):
        launched.append(args)
        raise OSError("fake")

    bot.runs.launch = launch
    await bot.run_prompt(OWNER, "no -p")
    assert launched and launched[0][1] == "-p" and "--resume" in launched[0]
    err, _, _ = bot.apply_account("personal")
    assert err is None
    await asyncio.wait_for(bot.run_prompt(OWNER, "de volta"), 5)
    starts = [c for c in fake.calls if c[0] == "start"]
    assert len(starts) == 2 and "--resume" in starts[1][4]
    assert SID in bot.hosts
    await close(bot)


async def test_atalho_n_texto_vai_para_a_sessao_hospedada(tmp_path, monkeypatch):
    bot, fake, project = setup(tmp_path, monkeypatch)
    other = "33333333-3333-4333-8333-333333333333"
    bot.state.session_id = other
    bot.state.slots = {
        "2": {"session_id": SID, "project": project, "account": "personal"}
    }
    fake.on_submit = reply_ok
    await bot.handle_text(OWNER, "2: oi", None)
    assert await until(
        lambda: any(t.startswith("[2·cbs-lab] OK") for t in sent_texts(bot))
    )
    assert ("paste", "oi") in fake.calls and bot.state.session_id == other
    await close(bot)


async def test_texto_comum_historico_feed_e_estado_do_painel(tmp_path, monkeypatch):
    bot, fake, _ = setup(tmp_path, monkeypatch)
    seen = {}

    def submit(text):
        seen["running"] = None
        return [j_user(text), j_assistant(j_tool(command="ls"), stop="tool_use")]

    fake.on_submit = submit
    await bot.handle_text(OWNER, "texto do chat", None)
    assert await until(lambda: SID in bot.hosts and bot.hosts[SID].turn_ours)
    state = await bot.api_state()
    assert SID in state["running"]
    assert bot.run_feed(SID)["running"] is True
    fake.append([j_assistant(j_text("feito")), END])
    assert await until(lambda: any(t.startswith("feito") for t in sent_texts(bot)))
    feed = bot.run_feed(SID)
    assert feed["running"] is False and feed["result"]["text"] == "feito"
    got = await bot.read_turns(SID, 10, None)
    assert got is not None
    turns, _ = got
    assert any(t.role == "user" and "texto do chat" in t.text for t in turns)
    assert any(t.role == "assistant" and "feito" in t.text for t in turns)
    assert SID not in (await bot.api_state())["running"]
    await close(bot)
