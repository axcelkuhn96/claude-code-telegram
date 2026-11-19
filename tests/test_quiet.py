"""Modo silêncio (/silencio, /resumo): matriz de entrega, fila limitada, persistência, comandos."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot import handlers, quiet, state
from bot.handlers import COMMANDS, HELP
from bot.pinned import status_line
from bot.state import State
from bot.usage import Profile
from tests.test_handlers import (
    OWNER,
    SID,
    STRANGER,
    fake_claude_bin,
    make_bot,
    sent_texts,
    upd,
)
from tests.test_hosted import URL
from tests.test_hosted_handlers import (  # noqa: F401  (rapido é autouse neste módulo também)
    END,
    PANE_ASK,
    close,
    j_assistant,
    j_text,
    j_user,
    rapido,
    reply_ok,
    setup,
    until,
)

HSID = "2964a863-5ebb-4e2c-9004-2f3ce15a55f9"


def ctx(*args):
    return SimpleNamespace(args=list(args))


def quiet_bot(tmp_path, mode, **kw):
    bot = make_bot(tmp_path, quiet=mode, **kw)
    bot.cfg.state_dir.mkdir(parents=True, exist_ok=True)
    return bot


def silent_flags(bot):
    return [
        c.kwargs.get("disable_notification") for c in bot.tg.send_message.call_args_list
    ]


# ---------- matriz de entrega (pura) ----------

MATRIX = {
    ("urgent", "off"): "send",
    ("urgent", "final"): "send",
    ("urgent", "total"): "send",
    ("final", "off"): "send",
    ("final", "final"): "send",
    ("final", "total"): "queue",
    ("notify", "off"): "send",
    ("notify", "final"): "silent",
    ("notify", "total"): "queue",
    ("echo", "off"): "send",
    ("echo", "final"): "drop",
    ("echo", "total"): "drop",
}


@pytest.mark.parametrize(("kind", "mode"), list(MATRIX))
def test_matriz_de_entrega(kind, mode):
    assert quiet.policy(kind, mode) == MATRIX[(kind, mode)]


def test_matriz_cobre_todos_os_tipos_e_modos():
    assert set(MATRIX) == {(k, m) for k in quiet.KINDS for m in quiet.MODES}


def test_modo_ou_tipo_desconhecido_entrega():
    assert quiet.policy("final", "xyz") == "send"
    assert quiet.policy("qualquer", "total") == "send"


# ---------- fila: limites e validação ----------


def test_fila_limitada_aos_ultimos_50():
    d: list = []
    for i in range(60):
        d = quiet.enqueue(d, quiet.make_item(float(i), "p", "t", "final", f"x{i}"))
    assert len(d) == quiet.MAX_DIGEST == 50
    assert d[0]["text"] == "x10" and d[-1]["text"] == "x59"


def test_item_com_texto_cortado_em_4000():
    it = quiet.make_item(1.0, "p", "t", "final", "a" * 9000, SID)
    assert len(it["text"]) == 4000 and it["text"].endswith("…")
    assert set(it) == {"ts", "project_label", "title", "kind", "text", "session_id"}


def test_valid_digest_descarta_lixo():
    ok = quiet.make_item(1.0, "p", "t", "final", "x")
    bad = [None, 3, {"text": 5, "ts": 1}, {"text": "a", "ts": True}, {"text": "a"}]
    assert quiet.valid_digest([*bad, ok]) == [ok]
    assert quiet.valid_digest("nope") == []
    assert len(quiet.valid_digest([ok] * 80)) == 50


# ---------- persistência ----------


def test_quiet_e_digest_persistem(tmp_path):
    p = tmp_path / "s" / "state.json"
    it = quiet.make_item(5.0, "proj", "tit", "final", "ola", SID)
    state.save(p, State(owner_id=1, quiet="total", digest=[it]))
    st = state.load(p)
    assert st.quiet == "total" and st.digest == [it]


def test_state_antigo_sem_campos_vira_off_e_vazio(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"owner_id": 1}))
    st = state.load(p)
    assert st.quiet == "off" and st.digest == []


def test_quiet_invalido_vira_off(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"owner_id": 1, "quiet": "mudo", "digest": {"a": 1}}))
    st = state.load(p)
    assert st.quiet == "off" and st.digest == []


# ---------- comandos ----------


async def test_silencio_sem_argumento_mostra_modo_e_botoes(tmp_path):
    bot = quiet_bot(tmp_path, "final")
    await bot.cmd_silencio(upd(text="/silencio"), ctx())
    call = bot.tg.send_message.call_args
    assert "final" in call.args[1] and "🔕" in call.args[1]
    labels = [
        b.text for row in call.kwargs["reply_markup"].inline_keyboard for b in row
    ]
    assert labels == ["Off", "Final", "Total"]
    datas = [
        b.callback_data
        for row in call.kwargs["reply_markup"].inline_keyboard
        for b in row
    ]
    assert datas == ["sl:off", "sl:final", "sl:total"]


@pytest.mark.parametrize("mode", ["off", "final", "total"])
async def test_silencio_com_argumento_muda_e_persiste(tmp_path, mode):
    bot = quiet_bot(tmp_path, "off" if mode != "off" else "total")
    await bot.cmd_silencio(upd(text=f"/silencio {mode}"), ctx(mode))
    assert bot.state.quiet == mode
    assert state.load(bot.cfg.state_dir / "state.json").quiet == mode
    assert mode in sent_texts(bot)[-1]


async def test_silencio_argumento_invalido_nao_muda(tmp_path):
    bot = quiet_bot(tmp_path, "final")
    await bot.cmd_silencio(upd(text="/silencio xis"), ctx("xis"))
    assert bot.state.quiet == "final"
    assert "off" in sent_texts(bot)[-1] and "total" in sent_texts(bot)[-1]


async def test_silencio_botao_inline(tmp_path):
    bot = quiet_bot(tmp_path, "off")
    await bot.on_callback(upd(data="sl:total"), ctx())
    assert bot.state.quiet == "total"
    await bot.on_callback(upd(data="sl:bogus"), ctx())
    assert bot.state.quiet == "total"


async def test_silencio_respeita_dono_e_trava_totp(tmp_path):
    bot = quiet_bot(tmp_path, "off")
    await bot.cmd_silencio(upd(user=STRANGER, text="/silencio total"), ctx("total"))
    await bot.cmd_resumo(upd(user=STRANGER, text="/resumo"), ctx())
    bot.tg.send_message.assert_not_called()
    bot.totp_secret = "JBSWY3DPEHPK3PXP"  # trancado (sem destrave)
    await bot.cmd_silencio(upd(text="/silencio total"), ctx("total"))
    await bot.on_callback(upd(data="sl:total"), ctx())
    assert bot.state.quiet == "off"
    assert all("trancado" in t for t in sent_texts(bot))


async def test_sair_do_silencio_mantem_fila_e_avisa_pendentes(tmp_path):
    bot = quiet_bot(tmp_path, "total")
    bot.state.digest = [quiet.make_item(1.0, "p", "t", "final", "x")]
    await bot.cmd_silencio(upd(text="/silencio off"), ctx("off"))
    assert len(bot.state.digest) == 1
    assert "/resumo" in sent_texts(bot)[-1]


def test_help_e_comandos_listam_silencio_e_resumo():
    assert "/silencio" in HELP and "/resumo" in HELP
    names = dict(COMMANDS)
    assert "silencio" in names and "resumo" in names


async def test_menu_e_teclado_mantem_layout(tmp_path):
    bot = quiet_bot(tmp_path, "total")
    assert handlers.KEYBOARD_ROWS[0][:2] == ["📊 Contas", "🗂 Sessões"]
    kb = bot.main_keyboard()
    assert [[b.text for b in row] for row in kb.keyboard] == handlers.KEYBOARD_ROWS


# ---------- marca 🔕 (barra fixada e /status) ----------


def test_status_line_marca_silencio():
    assert "🔕final" in status_line("p", None, "c", None, quiet="final")
    assert "🔕" not in status_line("p", None, "c", None)
    s = status_line(
        "p" * 200, "t", "c" * 200, None, working=3, locked=True, quiet="total·5"
    )
    assert len(s) <= 120 and s.endswith("🔕total·5 ⏳ 3")


async def test_refresh_status_e_cmd_status_mostram_marca(tmp_path, monkeypatch):
    bot = quiet_bot(tmp_path, "final", project=str(tmp_path), account="c1")
    bot.pinned_enabled = True
    bot.usage = AsyncMock(return_value=[])
    bot.profiles = list
    pinned = SimpleNamespace(update=AsyncMock())
    bot._pinned = pinned
    monkeypatch.setattr(handlers, "power_status", AsyncMock(return_value="🔋"))
    await bot.refresh_status()
    assert "🔕final" in pinned.update.call_args.args[0]
    bot.state.quiet = "total"
    bot.state.digest = [quiet.make_item(1.0, "p", "t", "final", "x")] * 3
    await bot.refresh_status()
    assert "🔕total·3" in pinned.update.call_args.args[0]
    await bot.cmd_status(upd(text="/status"), ctx())
    assert "🔕" in sent_texts(bot)[-1]
    bot.state.quiet = "off"
    await bot.cmd_status(upd(text="/status"), ctx())
    assert "🔕" not in sent_texts(bot)[-1]


# ---------- MCP telegram_notificar ----------


async def test_notify_off_entrega_normal(tmp_path):
    bot = quiet_bot(tmp_path, "off")
    assert await bot.notify("acabou", {"cwd": "/a/proj", "session_id": None})
    assert sent_texts(bot) == ["[proj] acabou"]
    assert silent_flags(bot) == [None]


async def test_notify_final_entrega_sem_notificar(tmp_path):
    bot = quiet_bot(tmp_path, "final")
    assert await bot.notify("acabou", {"cwd": "/a/proj", "session_id": None})
    assert sent_texts(bot) == ["[proj] acabou"]
    assert silent_flags(bot) == [True]


async def test_notify_total_vai_para_fila(tmp_path):
    bot = quiet_bot(tmp_path, "total")
    assert await bot.notify("acabou", {"cwd": "/a/proj", "session_id": SID})
    bot.tg.send_message.assert_not_called()
    (it,) = bot.state.digest
    assert it["kind"] == "notify" and it["text"] == "acabou"
    assert it["project_label"] == "proj" and it["session_id"] == SID
    on_disk = state.load(bot.cfg.state_dir / "state.json")
    assert on_disk.digest == bot.state.digest  # persistido


async def test_notify_total_fila_limitada(tmp_path):
    bot = quiet_bot(tmp_path, "total")
    for i in range(55):
        await bot.notify(f"n{i}", {"cwd": "/a/p", "session_id": None})
    assert len(bot.state.digest) == 50 and bot.state.digest[0]["text"] == "n5"


# ---------- execução do bot (-p) ----------


def run_bot(tmp_path, mode, claude_mode="ok"):
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    bot = quiet_bot(tmp_path, mode, project=str(proj), account="c1")
    bot.cfg.claude_bin = fake_claude_bin(tmp_path, claude_mode)
    bot.profiles = lambda: [Profile("c1", tmp_path / "c1")]
    return bot


async def test_run_off_tem_progresso_e_final(tmp_path):
    bot = run_bot(tmp_path, "off")
    await bot.run_prompt(OWNER, "faça")
    texts = sent_texts(bot)
    assert "⏳ trabalhando…" in texts and any("OK" in t for t in texts)
    assert all(f is None for f in silent_flags(bot))


async def test_run_final_sem_progresso_so_o_resultado(tmp_path):
    bot = run_bot(tmp_path, "final")
    await bot.run_prompt(OWNER, "faça")
    texts = sent_texts(bot)
    assert len(texts) == 1 and "OK" in texts[0] and "⏱ 0.0s" in texts[0]
    bot.tg.edit_message_text.assert_not_called()
    bot.tg.delete_message.assert_not_called()
    assert bot.state.digest == []
    assert silent_flags(bot) == [None]  # o resultado final notifica


async def test_run_final_mantem_botao_do_painel(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBAPP_URL", "https://painel.example")
    bot = run_bot(tmp_path, "final")
    await bot.run_prompt(OWNER, "faça")
    mk = bot.tg.send_message.call_args.kwargs["reply_markup"]
    assert "📖 Ver no painel" in [b.text for row in mk.inline_keyboard for b in row]


@pytest.mark.parametrize(("mode", "avisa"), [("off", True), ("final", False)])
async def test_run_em_sessao_nao_ativa_so_avisa_terminou_com_silencio_off(
    tmp_path, mode, avisa
):
    bot = run_bot(tmp_path, mode)
    target = {"session_id": SID, "project": bot.state.project, "account": "c1"}
    await bot.run_prompt(OWNER, "faça", target=target)
    texts = sent_texts(bot)
    assert any("OK" in t for t in texts)
    assert any("terminou" in t for t in texts) is avisa


def test_prompt_hospedado_curto_e_com_regra_de_perguntas():
    text = handlers.load_system_prompt(handlers.HOSTED_PROMPT_FILE)
    assert text and len(text.splitlines()) <= 10
    assert "AskUserQuestion" in text and "telegram_perguntar" in text
    assert "pergunta solta" in text and "telegram_enviar_arquivo" in text
    assert "pergunta solta" in handlers.load_system_prompt()


def test_claude_argv_com_system_prompt_antes_do_mcp():
    argv = handlers.hosted_mod.claude_argv("c", SID, True, "n", "m", "regra\nx")
    assert argv[-4:] == ["--append-system-prompt", "regra\nx", "--mcp-config", "m"]
    assert "--append-system-prompt" not in handlers.hosted_mod.claude_argv(
        "c", SID, True, "n", "m"
    )


async def test_run_total_enfileira_e_nao_envia(tmp_path):
    bot = run_bot(tmp_path, "total")
    await bot.run_prompt(OWNER, "faça")
    bot.tg.send_message.assert_not_called()
    (it,) = bot.state.digest
    assert it["kind"] == "final" and "OK" in it["text"] and "⏱" in it["text"]
    assert it["project_label"] == "proj"
    assert state.load(bot.cfg.state_dir / "state.json").digest == bot.state.digest


async def test_run_total_erro_e_urgente(tmp_path):
    bot = run_bot(tmp_path, "total", "fail")
    await bot.run_prompt(OWNER, "faça")
    assert any("código 3" in t for t in sent_texts(bot))
    assert bot.state.digest == []
    assert silent_flags(bot) == [None] * len(silent_flags(bot))


async def test_run_total_parar_e_urgente(tmp_path):
    bot = run_bot(tmp_path, "total", "sleep")
    bot.cfg.claude_bin = fake_claude_bin(tmp_path, "sleep")
    task = asyncio.create_task(bot.run_prompt(OWNER, "demora"))
    for _ in range(100):
        if bot.state.session_id and bot.runs.busy(bot.state.session_id):
            break
        await asyncio.sleep(0.05)
    await bot.cmd_parar(upd(), SimpleNamespace(args=[]))
    await asyncio.wait_for(task, 15)
    assert any("interrompid" in t for t in sent_texts(bot))
    assert bot.state.digest == []


async def test_run_total_limite_de_conta_e_urgente(tmp_path, monkeypatch):
    bot = run_bot(tmp_path, "total")
    seen = []
    bot._suggest_accounts = AsyncMock(side_effect=lambda *a, **k: seen.append(a))
    monkeypatch.setattr(handlers, "is_limit_error", lambda t, e: True)
    await bot.run_prompt(OWNER, "faça")
    assert seen  # o prompt de troca de conta segue saindo
    assert len(bot.state.digest) == 1  # o texto do resultado normal ainda vai ao resumo


# ---------- /resumo ----------


async def test_resumo_vazio(tmp_path):
    bot = quiet_bot(tmp_path, "total")
    await bot.cmd_resumo(upd(text="/resumo"), ctx())
    assert len(sent_texts(bot)) == 1 and "nada" in sent_texts(bot)[0].lower()


async def test_resumo_manda_um_cartao_por_item_do_mais_antigo_ao_mais_novo_e_limpa(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WEBAPP_URL", "https://painel.example")
    bot = quiet_bot(tmp_path, "total")
    bot.state.digest = [
        quiet.make_item(1000.0, "alfa", "Primeira", "final", "texto um", SID),
        quiet.make_item(2000.0, "beta", "Segunda", "notify", "texto dois", ""),
        quiet.make_item(3000.0, "gama", "", "final", "texto três", SID),
    ]
    await bot.cmd_resumo(upd(text="/resumo"), ctx())
    texts = sent_texts(bot)
    body = texts[1:]
    assert "3" in texts[0] and len(body) == 3
    assert "alfa" in body[0] and "texto um" in body[0] and "Primeira" in body[0]
    assert "beta" in body[1] and "texto dois" in body[1]
    assert "gama" in body[2] and "texto três" in body[2]
    calls = bot.tg.send_message.call_args_list[1:]
    btns = [
        [b.text for row in c.kwargs["reply_markup"].inline_keyboard for b in row]
        if c.kwargs.get("reply_markup")
        else []
        for c in calls
    ]
    assert (
        btns[0] == ["📖 Ver no painel"]
        and btns[1] == []
        and btns[2] == ["📖 Ver no painel"]
    )
    assert bot.state.digest == []
    assert state.load(bot.cfg.state_dir / "state.json").digest == []
    await bot.cmd_resumo(upd(text="/resumo"), ctx())  # já limpou
    assert "nada" in sent_texts(bot)[-1].lower()


async def test_resumo_cartoes_silenciosos_em_modo_quiet(tmp_path):
    bot = quiet_bot(tmp_path, "total")
    bot.state.digest = [quiet.make_item(1.0, "p", "t", "final", "x")] * 2
    await bot.cmd_resumo(upd(text="/resumo"), ctx())
    flags = silent_flags(bot)
    assert flags[1:] == [True, True]


async def test_resumo_cartao_compacto(tmp_path):
    bot = quiet_bot(tmp_path, "total")
    bot.state.digest = [quiet.make_item(1.0, "p", "t", "final", "z" * 4000)]
    await bot.cmd_resumo(upd(text="/resumo"), ctx())
    card = sent_texts(bot)[1]
    assert len(card) < 2000 and card.count("z") < 4000


# ---------- sessões hospedadas ----------


async def test_hospedada_off_ecoa_via_claude_ai(tmp_path, monkeypatch):
    bot, fake, _ = setup(tmp_path, monkeypatch)
    fake.on_submit = reply_ok
    await asyncio.wait_for(bot.run_prompt(OWNER, "oi"), 5)
    fake.append([j_user("do navegador"), j_assistant(j_text("resp web")), END])
    assert await until(lambda: any("resp web" in t for t in sent_texts(bot)))
    assert "🛰 (via claude.ai) 👤 do navegador" in sent_texts(bot)
    await close(bot)


async def test_hospedada_final_sem_eco_e_sem_progresso_mas_com_resultado(
    tmp_path, monkeypatch
):
    bot, fake, _ = setup(tmp_path, monkeypatch)
    bot.state.quiet = "final"
    fake.on_submit = reply_ok
    await asyncio.wait_for(bot.run_prompt(OWNER, "oi"), 5)
    texts = sent_texts(bot)
    assert any(t.startswith("OK") for t in texts)
    assert not any("sessão também no claude.ai" in t for t in texts)  # aviso do link
    assert "⏳ trabalhando…" not in texts
    bot.tg.edit_message_text.assert_not_called()
    fake.append([j_user("do navegador"), j_assistant(j_text("resp web")), END])
    assert await until(lambda: any("resp web" in t for t in sent_texts(bot)))
    assert not any(
        "via claude.ai" in t and "do navegador" in t for t in sent_texts(bot)
    )
    await close(bot)


async def test_hospedada_total_turno_externo_vai_para_a_fila(tmp_path, monkeypatch):
    bot, fake, _ = setup(tmp_path, monkeypatch)
    bot.state.quiet = "total"
    fake.on_submit = reply_ok
    await asyncio.wait_for(bot.run_prompt(OWNER, "oi"), 5)
    assert bot.state.digest and "OK" in bot.state.digest[-1]["text"]
    n = len(bot.state.digest)
    fake.append([j_user("do navegador"), j_assistant(j_text("resp web")), END])
    assert await until(lambda: len(bot.state.digest) == n + 1)
    assert "resp web" in bot.state.digest[-1]["text"]
    assert bot.state.digest[-1]["session_id"] == HSID
    assert sent_texts(bot) == []
    await close(bot)


async def test_hospedada_total_dialogo_do_terminal_ainda_chega(tmp_path, monkeypatch):
    bot, fake, _ = setup(tmp_path, monkeypatch)
    bot.state.quiet = "total"
    fake.on_submit = lambda text: [
        j_user(text),
        j_assistant(
            {"type": "tool_use", "id": "t9", "name": "AskUserQuestion", "input": {}},
            stop="tool_use",
        ),
    ]
    task = asyncio.create_task(bot.run_prompt(OWNER, "pergunte"))
    assert await until(lambda: HSID in bot.hosts and bot.hosts[HSID].turn_ours)
    fake.pane = PANE_ASK
    assert await until(lambda: bot.hosts[HSID].dialog is not None)
    assert any("o terminal pergunta" in t for t in sent_texts(bot))
    assert silent_flags(bot)[-1] is None  # notifica
    task.cancel()
    await close(bot)


async def test_hospedada_total_erro_de_turno_externo_e_urgente(tmp_path, monkeypatch):
    bot, fake, _ = setup(tmp_path, monkeypatch)
    bot.state.quiet = "total"
    fake.on_submit = reply_ok
    await asyncio.wait_for(bot.run_prompt(OWNER, "oi"), 5)
    n = len(bot.state.digest)
    ev = handlers.hosted_mod.Ev("end", text="falhou feio", is_error=True)
    await bot._host_event(bot.hosts[HSID], ev)
    assert any("falhou feio" in t for t in sent_texts(bot))
    assert len(bot.state.digest) == n
    await close(bot)


# ---------- teclado ----------


async def test_atualizacao_do_teclado_nao_manda_mensagem_no_silencio(tmp_path):
    bot = quiet_bot(tmp_path, "final", project=str(tmp_path))
    bot.state.slots = {
        "1": {"session_id": SID, "project": str(tmp_path), "account": "c"}
    }
    bot.request_keyboard_refresh()
    await asyncio.sleep(0.05)
    bot.tg.send_message.assert_not_called()
    assert URL  # (import usado: garante o módulo de fixtures)
