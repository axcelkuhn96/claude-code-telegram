"""Atalhos (slots 1-4) no chat: fixar, teclado, troca, prefixo "n: texto", execução sem trocar a ativa."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot import handlers, state as state_mod
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

SID3 = "33333333-3333-4333-8333-333333333333"
SID4 = "44444444-4444-4444-8444-444444444444"
SID5 = "55555555-5555-4555-8555-555555555555"


@pytest.fixture(autouse=True)
def rapido(monkeypatch):
    monkeypatch.setattr(handlers, "SEND_BACKOFF_S", (0.0, 0.0))
    monkeypatch.setattr(handlers, "KB_REFRESH_MIN_S", 0.0)


def sbot(tmp_path, **kw):
    bot = make_bot(tmp_path, **kw)
    bot.profiles = lambda: [
        Profile("c1", tmp_path / "c1"),
        Profile("c2", tmp_path / "c2"),
    ]
    return bot


def kb_labels(bot):
    kb = bot.tg.send_message.call_args.kwargs["reply_markup"]
    return [[b.text for b in row] for row in kb.keyboard]


def args(*a):
    return SimpleNamespace(args=list(a))


def pin_entry(sid=SID, project="/home/x/proj", account="c1"):
    return {"session_id": sid, "project": project, "account": account}


# ---------- fixar / desafixar ----------


async def test_fixar_no_primeiro_livre_manda_teclado_com_o_atalho(tmp_path):
    bot = sbot(tmp_path, project="/home/x/proj", account="c1", session_id=SID)
    await bot.cmd_fixar(upd(), CTX)
    assert bot.state.slots == {"1": pin_entry()}
    assert kb_labels(bot)[0] == ["1·proj"]  # 1ª linha = atalhos
    assert (
        "Fixada no atalho 1" in sent_texts(bot)[-1]
        and "1: sua mensagem" in sent_texts(bot)[-1]
    )
    saved = state_mod.load(bot.cfg.state_dir / "state.json")
    assert saved.slots == {"1": pin_entry()}  # persistido


async def test_fixar_n_explicito_substitui_e_ja_fixada(tmp_path):
    bot = sbot(tmp_path, project="/home/x/proj", account="c1", session_id=SID)
    bot.state.slots = {"3": pin_entry(SID2, "/home/x/outro")}
    await bot.cmd_fixar(upd(), args("3"))
    assert bot.state.slots["3"]["session_id"] == SID
    assert "substituído" in sent_texts(bot)[-1]
    await bot.cmd_fixar(upd(), args("3"))
    assert "já é o atalho 3" in sent_texts(bot)[-1]
    await bot.cmd_fixar(upd(), args("1"))  # mesma sessão em outro slot: move
    assert list(bot.state.slots) == ["1"]


async def test_fixar_argumento_invalido_e_sem_sessao(tmp_path):
    bot = sbot(tmp_path, project="/home/x/proj", session_id=SID)
    for bad in ("0", "5", "x", "1; rm"):
        await bot.cmd_fixar(upd(), args(bad))
        assert "uso: /fixar" in sent_texts(bot)[-1]
    assert bot.state.slots == {}
    bot.state.session_id = None
    await bot.cmd_fixar(upd(), CTX)
    assert "Sem sessão ativa" in sent_texts(bot)[-1]


async def test_quatro_cheios_oferece_substituir_e_o_toque_substitui(tmp_path):
    bot = sbot(tmp_path, project="/home/x/proj", account="c1", session_id=SID5)
    bot.state.slots = {
        "1": pin_entry(SID, "/home/x/a"),
        "2": pin_entry(SID2, "/home/x/b"),
        "3": pin_entry(SID3, "/home/x/c"),
        "4": pin_entry(SID4, "/home/x/d"),
    }
    await bot.cmd_fixar(upd(), CTX)
    rows = bot.tg.send_message.call_args.kwargs["reply_markup"].inline_keyboard
    assert [r[0].text for r in rows] == [
        f"Substituir {i}·{x}" for i, x in zip("1234", "abcd")
    ]
    data = [r[0].callback_data for r in rows]
    assert data[1] == f"f:2:{SID5}" and all(len(d.encode()) <= 64 for d in data)
    assert bot.state.slots["2"]["session_id"] == SID2  # nada mudou ainda
    await bot.on_callback(upd(data=data[1]), CTX)
    assert bot.state.slots["2"] == pin_entry(SID5, "/home/x/proj")


async def test_substituir_recusa_se_a_sessao_ativa_mudou(tmp_path):
    bot = sbot(tmp_path, project="/home/x/proj", session_id=SID2)
    await bot.on_callback(upd(data=f"f:1:{SID}"), CTX)
    assert "mudou" in sent_texts(bot)[-1] and bot.state.slots == {}
    for bad in ("f:9:" + SID, "f:1:nao-uuid", "f:1:"):
        await bot.on_callback(upd(data=bad), CTX)
    assert bot.state.slots == {}


async def test_desafixar_remove_e_atualiza_o_teclado(tmp_path):
    bot = sbot(tmp_path)
    bot.state.slots = {"1": pin_entry(), "2": pin_entry(SID2, "/home/x/b")}
    await bot.cmd_desafixar(upd(), args("1"))
    assert list(bot.state.slots) == ["2"]
    assert kb_labels(bot)[0] == ["2·b"] and "removido" in sent_texts(bot)[-1]
    await bot.cmd_desafixar(upd(), args("1"))
    assert "já está vazio" in sent_texts(bot)[-1]
    for bad in ((), ("9",), ("x",)):
        await bot.cmd_desafixar(upd(), args(*bad))
        assert "uso: /desafixar" in sent_texts(bot)[-1]
    assert list(bot.state.slots) == ["2"]


async def test_botao_fixar_do_teclado_e_comandos_trancados(tmp_path):
    bot = sbot(tmp_path, project="/home/x/proj", session_id=SID)
    await bot.on_text(upd(text="📌 Fixar"), CTX)
    assert "1" in bot.state.slots
    bot.totp_secret = "ABCDEFGHIJKLMNOP"
    for fn in (bot.cmd_fixar, bot.cmd_desafixar):
        await fn(upd(), args("1"))
    assert sent_texts(bot)[-1] == "🔒 trancado — /destravar <código>"
    assert "1" in bot.state.slots


# ---------- teclado ----------


async def test_teclado_marca_atalho_em_execucao(tmp_path):
    bot = sbot(tmp_path)
    bot.state.slots = {
        "1": pin_entry(),
        "2": pin_entry(SID2, "/home/x/uma-pasta-longa"),
    }
    bot.runs.running = lambda: [SID2]
    await bot.cmd_menu(upd(), CTX)
    # nome do projeto cortado em 10 caracteres; ⏳ só no que está rodando
    assert kb_labels(bot)[0] == ["1·proj", "2·uma-pasta- ⏳"]


async def test_teclado_sem_atalhos_nao_tem_linha_extra(tmp_path):
    bot = sbot(tmp_path)
    await bot.cmd_menu(upd(), CTX)
    assert kb_labels(bot)[0] == ["📊 Contas", "🗂 Sessões", "🆕 Nova"]


async def test_refresh_so_se_mudou_e_uma_vez(tmp_path):
    bot = sbot(tmp_path)
    bot.state.slots = {"1": pin_entry()}
    bot._kb_sent = bot._slot_labels()
    bot.request_keyboard_refresh()
    await asyncio.sleep(0.05)
    bot.tg.send_message.assert_not_called()  # nada mudou: nenhuma mensagem
    bot.runs.running = lambda: [SID]
    bot.request_keyboard_refresh()
    bot.request_keyboard_refresh()  # chamadas repetidas não duplicam
    await asyncio.sleep(0.05)
    assert bot.tg.send_message.await_count == 1
    assert sent_texts(bot)[-1] == "⌨️" and kb_labels(bot)[0] == ["1·proj ⏳"]
    bot.request_keyboard_refresh()
    await asyncio.sleep(0.05)
    assert bot.tg.send_message.await_count == 1  # já está em dia
    bot.runs.running = lambda: []
    bot.request_keyboard_refresh()
    await asyncio.sleep(0.05)
    assert bot.tg.send_message.await_count == 2 and kb_labels(bot)[0] == ["1·proj"]


async def test_refresh_respeita_o_intervalo_minimo(tmp_path, monkeypatch):
    monkeypatch.setattr(handlers, "KB_REFRESH_MIN_S", 0.3)
    bot = sbot(tmp_path)
    bot.state.slots = {"1": pin_entry()}
    bot._kb_sent = bot._slot_labels()
    bot._kb_last = bot.clock()  # acabou de atualizar
    bot.runs.running = lambda: [SID]
    bot.request_keyboard_refresh()
    await asyncio.sleep(0.1)
    bot.tg.send_message.assert_not_called()  # ainda dentro do intervalo
    await asyncio.sleep(0.4)
    assert bot.tg.send_message.await_count == 1  # o último estado foi publicado


async def test_refresh_nao_acontece_trancado_nem_sem_dono(tmp_path):
    bot = sbot(tmp_path)
    bot.state.slots = {"1": pin_entry()}
    bot._kb_sent = ()
    bot.totp_secret = "ABCDEFGHIJKLMNOP"
    bot.request_keyboard_refresh()
    await asyncio.sleep(0.05)
    bot.tg.send_message.assert_not_called()
    sem = sbot(tmp_path / "s", owner=None)
    sem.state.slots = {"1": pin_entry()}
    sem._kb_sent = ()
    sem.request_keyboard_refresh()
    await asyncio.sleep(0.05)
    sem.tg.send_message.assert_not_called()


# ---------- trocar pelo botão do atalho ----------


async def test_toque_no_atalho_troca_sessao_projeto_conta_e_mostra_resumo(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        handlers.sessions_mod,
        "session_file",
        lambda *a: tmp_path / "s.jsonl",
        raising=False,
    )
    monkeypatch.setattr(
        handlers.sessions_mod,
        "last_exchange",
        lambda p: ("pergunta", "resposta"),
        raising=False,
    )
    bot = sbot(tmp_path, project="/home/x/a", account="c1", session_id=SID)
    bot.state.slots = {"2": pin_entry(SID2, "/home/x/outro", "c2")}
    bot.run_prompt = AsyncMock()
    await bot.on_text(upd(text="2·outro"), CTX)
    assert (bot.state.project, bot.state.session_id, bot.state.account) == (
        "/home/x/outro",
        SID2,
        "c2",
    )
    assert "atalho 2" in sent_texts(bot)[0] and "outro" in sent_texts(bot)[0]
    assert sent_texts(bot)[1] == "Você: pergunta\nClaude: resposta"
    await asyncio.sleep(0.02)
    bot.run_prompt.assert_not_called()  # não virou prompt
    assert state_mod.load(bot.cfg.state_dir / "state.json").session_id == SID2


async def test_toque_em_atalho_com_marca_de_execucao(tmp_path):
    bot = sbot(tmp_path, project="/home/x/a", session_id=SID)
    bot.state.slots = {"1": pin_entry(SID2, "/home/x/b")}
    await bot.on_text(upd(text="1·b ⏳"), CTX)
    assert bot.state.session_id == SID2


async def test_texto_parecido_com_botao_de_atalho_vazio_vira_prompt(tmp_path):
    bot = sbot(tmp_path, project="/home/x/a", session_id=SID)
    bot.run_prompt = AsyncMock()
    await bot.on_text(upd(text="1·foo"), CTX)
    await asyncio.sleep(0.05)
    bot.run_prompt.assert_awaited_once_with(OWNER, "1·foo")


# ---------- prefixo "n: texto" ----------


@pytest.mark.parametrize(
    "texto,esperado",
    [
        ("2: roda os testes", "roda os testes"),
        ("  3 ：faz x", "faz x"),
        ("4:   ok", "ok"),
    ],
)
async def test_prefixo_vai_para_o_atalho_sem_trocar_a_ativa(tmp_path, texto, esperado):
    bot = sbot(tmp_path, project="/home/x/a", account="c1", session_id=SID)
    bot.state.slots = {str(n): pin_entry(SID2, f"/home/x/p{n}") for n in (2, 3, 4)}
    bot.run_prompt = AsyncMock()
    await bot.on_text(upd(text=texto), CTX)
    await asyncio.sleep(0.05)
    n = int(texto.strip()[0])
    bot.run_prompt.assert_awaited_once_with(OWNER, esperado, slot=n)
    assert (bot.state.project, bot.state.session_id) == (
        "/home/x/a",
        SID,
    )  # ativa intacta


async def test_prefixo_sem_atalho_fixado_vai_inteiro_para_a_sessao_ativa(tmp_path):
    bot = sbot(tmp_path, project="/home/x/a", session_id=SID)
    bot.state.slots = {"1": pin_entry()}
    bot.run_prompt = AsyncMock()
    await bot.on_text(upd(text="2: lista de compras"), CTX)  # o 2 está vazio
    await bot.on_text(upd(text="5: nada"), CTX)
    await bot.on_text(upd(text="1:"), CTX)  # sem corpo
    await bot.on_text(upd(text="item 2: foo"), CTX)  # não é prefixo
    await asyncio.sleep(0.05)
    assert [c.args[1] for c in bot.run_prompt.await_args_list] == [
        "2: lista de compras",
        "5: nada",
        "1:",
        "item 2: foo",
    ]
    assert all(c.kwargs == {} for c in bot.run_prompt.await_args_list)


async def test_transcricao_de_voz_tambem_vai_para_o_atalho(tmp_path, monkeypatch):
    bot = sbot(tmp_path, project="/home/x/a", session_id=SID)
    bot.state.slots = {"2": pin_entry(SID2, "/home/x/b")}
    bot.tg.get_file = AsyncMock(
        return_value=SimpleNamespace(
            download_as_bytearray=AsyncMock(return_value=bytearray(b"x"))
        )
    )
    monkeypatch.setattr(
        handlers, "transcribe", AsyncMock(return_value="2: faz o deploy")
    )
    bot.run_prompt = AsyncMock()
    u = upd()
    u.message.voice = SimpleNamespace(file_id="f", file_size=10)
    await bot.on_voice(u, CTX)
    await asyncio.sleep(0.05)
    bot.run_prompt.assert_awaited_once_with(OWNER, "faz o deploy", slot=2)


async def test_reply_a_pergunta_tem_prioridade_sobre_o_prefixo(tmp_path):
    bot = sbot(tmp_path, project="/home/x/a", session_id=SID)
    bot.state.slots = {"2": pin_entry(SID2, "/home/x/b")}
    bot.run_prompt = AsyncMock()
    task = asyncio.create_task(bot.ask("qual?", [], 5, {}))
    await asyncio.sleep(0.05)
    await bot.on_text(upd(text="2: sim", reply_to=100), CTX)
    assert await asyncio.wait_for(task, 2) == "2: sim"
    bot.run_prompt.assert_not_called()


# ---------- execução no atalho ----------


async def test_execucao_no_atalho_usa_a_sessao_dele_e_nao_mexe_na_ativa(tmp_path):
    proj_a, proj_b = tmp_path / "a", tmp_path / "b"
    proj_a.mkdir()
    proj_b.mkdir()
    bot = sbot(tmp_path, project=str(proj_a), account="c1", session_id=SID)
    bot.state.slots = {"2": pin_entry(SID2, str(proj_b), "c2")}
    out = tmp_path / "dump"
    out.mkdir()
    fake = Path(__file__).parent / "fake_claude.py"
    script = tmp_path / "claude"
    import sys as _sys

    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" > "{out}/argv.txt"\n'
        f'echo "$CLAUDE_CONFIG_DIR|$CLAUDE_SESSION_ID|$(pwd)" > "{out}/env.txt"\n'
        f'exec "{_sys.executable}" "{fake}" ok\n'
    )
    script.chmod(0o755)
    bot.cfg.claude_bin = script
    await bot.run_prompt(OWNER, "faz isso", slot=2)
    argv = (out / "argv.txt").read_text().splitlines()
    assert argv[argv.index("--session-id") + 1] == SID2  # nova: arquivo não existe
    cfg_dir, sid, cwd = (out / "env.txt").read_text().strip().split("|")
    assert (cfg_dir, sid, cwd) == (str(tmp_path / "c2"), SID2, str(proj_b.resolve()))
    # a sessão ativa não mudou
    assert (bot.state.project, bot.state.account, bot.state.session_id) == (
        str(proj_a),
        "c1",
        SID,
    )
    texts = sent_texts(bot)
    assert any(
        t.startswith("[2·b]") and "OK" in t for t in texts
    )  # resposta prefixada [n·projeto]
    assert any(
        t.startswith("✅ terminou [2·b] em") for t in texts
    )  # aviso curto separado


async def test_execucao_no_atalho_vazio_ou_ocupado_ou_projeto_sumiu(tmp_path):
    bot = sbot(tmp_path, project=str(tmp_path), account="c1", session_id=SID)
    await bot.run_prompt(OWNER, "x", slot=3)
    assert "Atalho 3 vazio" in sent_texts(bot)[-1]
    bot.state.slots = {"1": pin_entry(SID2, str(tmp_path / "nao-existe"))}
    await bot.run_prompt(OWNER, "x", slot=1)
    assert "projeto do atalho não existe" in sent_texts(bot)[-1]
    real = tmp_path / "real"
    real.mkdir()
    bot.state.slots = {"1": pin_entry(SID2, str(real))}
    bot.runs.busy = lambda sid: sid == SID2
    await bot.run_prompt(OWNER, "x", slot=1)
    assert "ocupada" in sent_texts(bot)[-1]


async def test_atalho_segue_o_id_real_da_sessao(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    fixture = Path(__file__).parent / "fixtures" / "stream_ok.jsonl"
    real = "00000000-0000-4000-8000-0000000000a1"
    bot = sbot(tmp_path, project=str(proj), account="c1", session_id=SID)
    bot.state.slots = {"1": pin_entry(SID, str(proj))}
    bot.cfg.claude_bin = fake_claude_bin(tmp_path, f"fixture {fixture}")
    await bot.run_prompt(OWNER, "x")
    assert bot.state.session_id == real
    assert bot.state.slots["1"]["session_id"] == real  # o atalho acompanhou
    assert (
        state_mod.load(bot.cfg.state_dir / "state.json").slots["1"]["session_id"]
        == real
    )


async def test_conta_esgotada_no_atalho_nao_oferece_reenvio_automatico(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    f = tmp_path / "limit.jsonl"
    f.write_text(
        json.dumps(
            {
                "type": "result",
                "is_error": True,
                "result": "5-hour limit reached",
                "session_id": SID2,
            }
        )
        + "\n"
    )
    bot = sbot(tmp_path, project=str(tmp_path), account="c1", session_id=SID)
    bot.state.slots = {"2": pin_entry(SID2, str(proj), "c1")}
    bot.cfg.claude_bin = fake_claude_bin(tmp_path, f"fixture {f}")
    from tests.test_handlers import usage_item

    bot.usage = AsyncMock(return_value=[usage_item("c1", 100.0), usage_item("c2", 9.0)])
    await bot.run_prompt(OWNER, "x", slot=2)
    markup = bot.tg.send_message.call_args.kwargs["reply_markup"]
    data = [r[0].callback_data for r in markup.inline_keyboard]
    assert (
        data == ["a:c2"] and bot._retry == {}
    )  # sem token de reenvio (só vale p/ a ativa)


# ---------- fachada / state ----------


async def test_api_state_expoe_os_atalhos(tmp_path):
    bot = sbot(tmp_path)
    bot.state.slots = {
        "1": pin_entry(SID, "/home/x/proj", "c1"),
        "2": pin_entry(SID2, str(bot.cfg.home), ""),
    }
    bot.runs.running = lambda: [SID2]
    slots = (await bot.api_state())["slots"]
    assert slots["1"] == {
        "session_id": SID,
        "project": "/home/x/proj",
        "project_label": "proj",
        "account": "c1",
        "running": False,
    }
    assert slots["2"]["project_label"] == "~ (home)" and slots["2"]["account"] is None
    assert slots["2"]["running"] is True
    json.dumps(slots)


def test_state_valida_slots_do_disco(tmp_path):
    p = tmp_path / "s.json"
    good = pin_entry()
    p.write_text(
        json.dumps(
            {
                "slots": {
                    "1": good,
                    "2": good,
                    "9": good,
                    "3": {"session_id": "x"},
                    "4": 5,
                }
            }
        )
    )
    assert state_mod.load(p).slots == {
        "1": good
    }  # duplicado, fora da faixa e inválidos caem
    p.write_text(json.dumps({"slots": "lixo"}))
    assert state_mod.load(p).slots == {}
    state_mod.save(p, state_mod.State(slots={"2": good}))
    assert state_mod.load(p).slots == {"2": good}
    assert datetime.now(UTC)  # (silencia import não usado em ambientes sem freezegun)
