"""Reinício seguro (/restart-when-idle + scripts/safe-restart.sh) e execuções interrompidas."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from bot import handlers, state as state_mod
from bot.localapi import LocalApi, write_port, write_secret
from bot.usage import Profile
from tests.test_handlers import (
    CTX,
    OWNER,
    SID,
    SID2,
    fake_claude_bin,
    make_bot,
    sent_texts,
    upd,
)
from tests.test_localapi import SECRET, auth, http

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "safe-restart.sh"


@pytest.fixture(autouse=True)
def rapido(monkeypatch):
    monkeypatch.setattr(handlers, "SEND_BACKOFF_S", (0.0, 0.0))
    monkeypatch.setattr(handlers, "DRAIN_POLL_S", 0.02)


def rbot(tmp_path, mode="ok", **kw):
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    bot = make_bot(tmp_path, project=str(proj), account="c1", session_id=SID, **kw)
    bot.cfg.claude_bin = fake_claude_bin(tmp_path, mode)
    bot.profiles = lambda: [Profile("c1", tmp_path / "c1")]
    return bot


async def wait_busy(bot, sid=SID):
    for _ in range(200):
        if bot.runs.busy(sid):
            return
        await asyncio.sleep(0.02)
    raise AssertionError("a execução não começou")


# ---------- drenagem ----------


async def test_restart_when_idle_sem_execucao_sai_logo(tmp_path):
    bot = make_bot(tmp_path)
    exits = []
    bot.exit_fn = lambda: exits.append(1)
    assert await bot.restart_when_idle() == {"ok": True, "draining": True, "running": 0}
    await asyncio.sleep(0.8)
    assert exits == [1] and bot.draining is True


async def test_espera_a_execucao_terminar_antes_de_sair(tmp_path):
    bot = make_bot(tmp_path)
    exits = []
    bot.exit_fn = lambda: exits.append(1)
    ativo = [SID, SID2]
    bot.runs.running = lambda: list(ativo)
    res = await bot.restart_when_idle()
    assert res["running"] == 2
    await asyncio.sleep(0.4)
    assert exits == []  # ainda tem execução: não sai
    ativo.clear()
    await asyncio.sleep(0.9)
    assert exits == [1]


async def test_idempotente_um_so_exit(tmp_path):
    bot = make_bot(tmp_path)
    exits = []
    bot.exit_fn = lambda: exits.append(1)
    await bot.restart_when_idle()
    await bot.restart_when_idle()
    await bot.restart_when_idle()
    await asyncio.sleep(0.9)
    assert exits == [1]


async def test_limite_de_espera_sai_mesmo_com_execucao(tmp_path, monkeypatch):
    monkeypatch.setattr(handlers, "DRAIN_CAP_S", 0.1)
    bot = make_bot(tmp_path)
    exits = []
    bot.exit_fn = lambda: exits.append(1)
    bot.runs.running = lambda: [SID]
    await bot.restart_when_idle()
    await asyncio.sleep(1.0)
    assert exits == [1]


async def test_exit_padrao_manda_sigterm_ao_proprio_processo(tmp_path, monkeypatch):
    bot = make_bot(tmp_path)
    sent = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append((pid, sig)))
    bot.exit_fn()
    assert sent == [(os.getpid(), signal.SIGTERM)]


async def test_drenando_recusa_execucao_nova_com_a_mensagem_combinada(tmp_path):
    bot = rbot(tmp_path)
    bot.exit_fn = lambda: None
    await bot.restart_when_idle()
    await bot.run_prompt(OWNER, "faça algo")
    await bot.run_prompt(OWNER, "x", slot=1)
    assert sent_texts(bot) == [handlers.DRAIN_MESSAGE] * 2
    assert (
        handlers.DRAIN_MESSAGE == "⏳ reiniciando em instantes — mande de novo em 1 min"
    )
    assert bot.runs.running() == [] and bot.state.interrupted == []
    await bot.on_text(upd(text="outra"), CTX)
    await asyncio.sleep(0.05)
    assert sent_texts(bot)[-1] == handlers.DRAIN_MESSAGE


async def test_drenando_notify_e_ask_continuam(tmp_path):
    bot = make_bot(tmp_path)
    bot.exit_fn = lambda: None
    await bot.restart_when_idle()
    assert await bot.notify("alerta", {"cwd": "/a/proj"}) is True
    task = asyncio.create_task(bot.ask("vai?", ["sim"], 5, {}))
    await asyncio.sleep(0.05)
    assert "vai?" in sent_texts(bot)[-1]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_execucao_em_andamento_termina_normalmente_durante_a_drenagem(tmp_path):
    bot = rbot(tmp_path, "ok")
    bot.exit_fn = lambda: None
    task = asyncio.create_task(bot.run_prompt(OWNER, "x"))
    await asyncio.sleep(0.01)
    await bot.restart_when_idle()
    await asyncio.wait_for(task, 10)
    assert any("OK" in t for t in sent_texts(bot))  # a resposta chegou


# ---------- API local ----------


async def test_rota_restart_when_idle_exige_segredo_e_devolve_o_estado():
    chamadas = []

    async def restart():
        chamadas.append(1)
        return {"ok": True, "draining": True, "running": 3}

    async def nop(*a):
        return None

    srv = LocalApi(SECRET, nop, nop, port=0, on_restart_when_idle=restart)
    await srv.start()
    try:
        assert (await http(srv.port, path="/restart-when-idle", body=b"{}"))[0] == 401
        assert chamadas == []
        status, data = await http(
            srv.port, path="/restart-when-idle", headers=auth(), body=b"{}"
        )
        assert (status, data) == (200, {"ok": True, "draining": True, "running": 3})
        assert chamadas == [1]
        assert (
            await http(
                srv.port, method="GET", path="/restart-when-idle", headers=auth()
            )
        )[0] == 405
        assert (
            await http(
                srv.port, path="/restart-when-idle", headers=auth(), body=b"nao json"
            )
        )[0] == 400
    finally:
        await srv.stop()


async def test_rota_404_sem_handler():
    async def nop(*a):
        return None

    srv = LocalApi(SECRET, nop, nop, port=0)
    await srv.start()
    try:
        assert (
            await http(srv.port, path="/restart-when-idle", headers=auth(), body=b"{}")
        )[0] == 404
    finally:
        await srv.stop()


# ---------- scripts/safe-restart.sh ----------


async def run_script(env, *args):
    proc = await asyncio.create_subprocess_exec(
        "bash",
        str(SCRIPT),
        *args,
        env={"PATH": os.environ["PATH"], "HOME": env["HOME"], **env},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(proc.communicate(), 30)
    return proc.returncode, out.decode(), err.decode()


async def test_script_pede_o_reinicio_sem_vazar_o_segredo(tmp_path):
    chamadas = []

    async def restart():
        chamadas.append(1)
        return {"ok": True, "draining": True, "running": 2}

    async def nop(*a):
        return None

    srv = LocalApi(SECRET, nop, nop, port=0, on_restart_when_idle=restart)
    await srv.start()
    try:
        state = tmp_path / "estado"
        write_secret(state, SECRET)
        write_port(state, srv.port)
        env = {"HOME": str(tmp_path), "CLAUDE_TELEGRAM_STATE_DIR": str(state)}
        code, out, err = await run_script(env, "--no-wait")
    finally:
        await srv.stop()
    assert code == 0 and chamadas == [1]
    assert "Execuções em andamento: 2" in out
    assert SECRET not in out + err  # nunca imprime o segredo


async def test_script_bot_parado_avisa_e_nao_reinicia(tmp_path):
    state = tmp_path / "estado"
    state.mkdir()
    env = {"HOME": str(tmp_path), "CLAUDE_TELEGRAM_STATE_DIR": str(state)}
    code, out, err = await run_script(env, "--no-wait")  # sem local.secret
    assert code == 2 and "não está rodando" in err and "systemctl --user start" in err
    write_secret(state, SECRET)
    write_port(state, 1)  # porta sem ninguém ouvindo
    code, out, err = await run_script(env, "--no-wait")
    assert code == 2 and "nada foi reiniciado" in err


async def test_script_segredo_recusado_e_bot_antigo(tmp_path):
    async def nop(*a):
        return None

    srv = LocalApi(SECRET, nop, nop, port=0)  # sem a rota: bot "antigo"
    await srv.start()
    try:
        state = tmp_path / "estado"
        write_secret(state, SECRET)
        write_port(state, srv.port)
        env = {"HOME": str(tmp_path), "CLAUDE_TELEGRAM_STATE_DIR": str(state)}
        code, _, err = await run_script(env, "--no-wait")
        assert code == 2 and "ainda não tem o reinício seguro" in err
        write_secret(state, "outro-segredo")
        code, _, err = await run_script(env, "--no-wait")
        assert code == 2 and "recusou o segredo" in err and "outro-segredo" not in err
    finally:
        await srv.stop()


# ---------- execuções interrompidas ----------


async def test_execucao_fica_registrada_no_state_enquanto_roda_e_some_ao_terminar(
    tmp_path,
):
    bot = rbot(tmp_path, "sleep")
    task = asyncio.create_task(bot.run_prompt(OWNER, "faça isso demorado"))
    await wait_busy(bot)
    [e] = bot.state.interrupted
    assert (e["session_id"], e["project"], e["account"], e["prompt"], e["posted"]) == (
        SID,
        str(tmp_path / "proj"),
        "c1",
        "faça isso demorado",
        False,
    )
    assert state_mod.load(bot.cfg.state_dir / "state.json").interrupted == [
        e
    ]  # em disco
    await bot.cmd_parar(upd(), CTX)  # parada pelo dono = fim normal
    await asyncio.wait_for(task, 15)
    assert bot.state.interrupted == []
    assert any(
        "interrompida" in t for t in sent_texts(bot)
    )  # "⏹ Execução interrompida"


async def test_fim_normal_remove_o_registro(tmp_path):
    bot = rbot(tmp_path, "ok")
    await bot.run_prompt(OWNER, "x")
    assert bot.state.interrupted == []
    assert state_mod.load(bot.cfg.state_dir / "state.json").interrupted == []


async def test_erro_normal_do_claude_nao_vira_interrompido(tmp_path):
    bot = rbot(tmp_path, "fail")  # exit 3
    await bot.run_prompt(OWNER, "x")
    assert bot.state.interrupted == [] and any("código 3" in t for t in sent_texts(bot))


async def test_sigterm_de_fora_vira_interrompido_com_botao_e_sem_codigo_143(tmp_path):
    bot = rbot(tmp_path, "sleep")
    task = asyncio.create_task(bot.run_prompt(OWNER, "faça isso"))
    await wait_busy(bot)
    os.killpg(bot.runs.get(SID).pid, signal.SIGTERM)  # o systemd/alguém mata o claude
    await asyncio.wait_for(task, 15)
    texts = sent_texts(bot)
    assert not any("143" in t or "saiu com código" in t for t in texts)
    msg = bot.tg.send_message.call_args
    assert msg.args[1].startswith("⏸ interrompido (o processo recebeu SIGTERM): <b>")
    assert msg.kwargs["parse_mode"] == "HTML"
    [e] = bot.state.interrupted
    btn = msg.kwargs["reply_markup"].inline_keyboard[0][0]
    assert btn.text == "🔁 Reenviar" and btn.callback_data == f"i:{e['id']}"
    assert e["posted"] is True and e["prompt"] == "faça isso"


async def test_shutdown_com_execucao_ativa_guarda_e_nao_manda_mensagem_de_erro(
    tmp_path,
):
    bot = rbot(tmp_path, "sleep")
    task = asyncio.create_task(bot.run_prompt(OWNER, "prompt do reinício"))
    await wait_busy(bot)
    n_antes = len(sent_texts(bot))
    await bot.shutdown()  # parada forçada (SIGTERM do serviço)
    await asyncio.gather(task, return_exceptions=True)
    assert not any(
        "saiu com código" in t or "143" in t for t in sent_texts(bot)[n_antes:]
    )
    [e] = bot.state.interrupted
    assert (
        e["prompt"] == "prompt do reinício" and e["posted"] is False
    )  # avisa no próximo start
    assert (
        state_mod.load(bot.cfg.state_dir / "state.json").interrupted[0]["prompt"]
        == "prompt do reinício"
    )


async def test_proximo_start_avisa_uma_vez_com_titulo_e_botao_reenviar(
    tmp_path, monkeypatch
):
    novo = make_bot(tmp_path / "n", project=None)
    proj = str(tmp_path / "proj-x")
    novo.state.interrupted = [
        {
            "id": "ab12cd34",
            "session_id": SID,
            "project": proj,
            "account": "c1",
            "prompt": "refaz",
            "ts": __import__("time").time(),
            "posted": False,
        }
    ]
    monkeypatch.setattr(
        handlers.sessions_mod, "session_file", lambda *a: tmp_path / "s", raising=False
    )
    monkeypatch.setattr(
        handlers.sessions_mod,
        "tail_meta",
        lambda p, limit=60: ("API do RAD <v2>", ""),
        raising=False,
    )
    novo.profiles = lambda: []
    await novo.post_interrupted()
    msg = novo.tg.send_message.call_args
    assert msg.args[1] == "⏸ interrompido pelo reinício: <b>API do RAD &lt;v2&gt;</b>"
    assert (
        msg.kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "i:ab12cd34"
    )
    assert novo.state.interrupted[0]["posted"] is True
    n = novo.tg.send_message.await_count
    await novo.post_interrupted()  # reinício seguinte: não repete
    assert novo.tg.send_message.await_count == n


async def test_start_sem_titulo_usa_o_projeto_e_descarta_entradas_velhas(tmp_path):
    bot = make_bot(tmp_path)
    t = __import__("time").time()
    bot.state.interrupted = [
        {
            "id": "aaaaaaaa",
            "session_id": SID,
            "project": "/x/meu-proj",
            "account": "",
            "prompt": "p",
            "ts": t,
            "posted": False,
        },
        {
            "id": "bbbbbbbb",
            "session_id": SID2,
            "project": "/x/velho",
            "account": "",
            "prompt": "p",
            "ts": t - 25 * 3600,
            "posted": False,
        },
    ]
    bot.profiles = lambda: []
    await bot.post_interrupted()
    assert sent_texts(bot) == ["⏸ interrompido pelo reinício: <b>meu-proj</b>"]
    assert [e["id"] for e in bot.state.interrupted] == ["aaaaaaaa"]


async def test_reenviar_roda_o_mesmo_prompt_na_mesma_sessao_e_conta_sem_mexer_na_ativa(
    tmp_path,
):
    proj_a, proj_b = tmp_path / "a", tmp_path / "b"
    proj_a.mkdir()
    proj_b.mkdir()
    bot = make_bot(tmp_path, project=str(proj_a), account="c1", session_id=SID)
    out = tmp_path / "dump"
    out.mkdir()
    fake = Path(__file__).parent / "fake_claude.py"
    script = tmp_path / "claude"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" > "{out}/argv.txt"\n'
        f'echo "$CLAUDE_CONFIG_DIR|$(pwd)" > "{out}/env.txt"\n'
        f'exec "{sys.executable}" "{fake}" ok\n'
    )
    script.chmod(0o755)
    bot.cfg.claude_bin = script
    bot.profiles = lambda: [
        Profile("c1", tmp_path / "c1"),
        Profile("c3", tmp_path / "c3"),
    ]
    bot.state.interrupted = [
        {
            "id": "cafe0001",
            "session_id": SID2,
            "project": str(proj_b),
            "account": "c3",
            "prompt": "o pedido original",
            "ts": __import__("time").time(),
            "posted": True,
        }
    ]
    await bot.on_callback(upd(data="i:cafe0001"), CTX)
    for _ in range(200):
        if (out / "env.txt").exists() and any("OK" in t for t in sent_texts(bot)):
            break
        await asyncio.sleep(0.05)
    argv = (out / "argv.txt").read_text().splitlines()
    assert argv[-2:] == ["--", "o pedido original"]
    assert argv[argv.index("--session-id") + 1] == SID2  # sessão nova no disco do teste
    cfg_dir, cwd = (out / "env.txt").read_text().strip().split("|")
    assert cfg_dir == str(tmp_path / "c3") and cwd == str(proj_b.resolve())
    assert (bot.state.project, bot.state.account, bot.state.session_id) == (
        str(proj_a),
        "c1",
        SID,
    )
    assert bot.state.interrupted == []  # consumido
    assert any(
        t.startswith(f"[b · {SID2[:8]}]") for t in sent_texts(bot)
    )  # não é a ativa: prefixo


async def test_reenviar_id_desconhecido_ja_usado_e_durante_drenagem(tmp_path):
    bot = rbot(tmp_path)
    await bot.on_callback(upd(data="i:deadbeef"), CTX)
    assert "já foi reenviado ou expirou" in sent_texts(bot)[-1]
    bot.state.interrupted = [
        {
            "id": "cafe0002",
            "session_id": SID,
            "project": str(tmp_path / "proj"),
            "account": "c1",
            "prompt": "p",
            "ts": __import__("time").time(),
            "posted": True,
        }
    ]
    bot.draining = True
    await bot.on_callback(upd(data="i:cafe0002"), CTX)
    assert sent_texts(bot)[-1] == handlers.DRAIN_MESSAGE
    assert len(bot.state.interrupted) == 1  # o pedido continua guardado
    for bad in ("i:", "i:xyz", "i:" + "a" * 17, "i:../../x"):
        await bot.on_callback(upd(data=bad), CTX)


async def test_reenviar_trancado_e_estranho(tmp_path):
    bot = rbot(tmp_path)
    bot.state.interrupted = [
        {
            "id": "cafe0003",
            "session_id": SID,
            "project": str(tmp_path / "proj"),
            "account": "c1",
            "prompt": "p",
            "ts": __import__("time").time(),
            "posted": True,
        }
    ]
    bot.run_prompt = AsyncMock()
    await bot.on_callback(upd(user=222, data="i:cafe0003"), CTX)
    bot.totp_secret = "ABCDEFGHIJKLMNOP"
    await bot.on_callback(upd(data="i:cafe0003"), CTX)
    await asyncio.sleep(0.05)
    bot.run_prompt.assert_not_called()
    assert len(bot.state.interrupted) == 1


def test_state_interrompidas_so_aceita_entradas_bem_formadas(tmp_path):
    p = tmp_path / "s.json"
    good = {
        "id": "abc",
        "session_id": SID,
        "project": "/p",
        "account": "c1",
        "prompt": "x",
        "ts": 5,
        "posted": True,
    }
    p.write_text(
        json.dumps(
            {
                "interrupted": [
                    good,
                    {"id": 1},
                    "lixo",
                    {**good, "session_id": "nao-uuid"},
                    {**good, "prompt": 5},
                    {**good, "prompt": "y" * 30000},
                ]
            }
        )
    )
    got = state_mod.load(p).interrupted
    assert got[0] == good and len(got) == 2 and len(got[1]["prompt"]) == 20000
    p.write_text(json.dumps({"interrupted": "lixo"}))
    assert state_mod.load(p).interrupted == []
    state_mod.save(p, state_mod.State(interrupted=[good]))
    assert state_mod.load(p).interrupted == [good]


def test_format_final_143_nao_diz_saiu_com_codigo():
    out = handlers.format_final(None, False, None, None, "", "", 143, False, False)
    assert out.startswith("⏸ interrompido") and "143" not in out
    out = handlers.format_final(None, False, None, None, "", "", -15, False, False)
    assert "saiu com código" not in out
    out = handlers.format_final(None, False, None, None, "", "", 2, False, False)
    assert "código 2" in out
