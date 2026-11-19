"""Texto livre/ditado responde a pergunta pendente (telegram_perguntar); opção "Outro…"."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot import handlers
from bot.localapi import Asks
from tests.test_handlers import CTX, OWNER, SID, SID2, make_bot, sent_texts, upd


@pytest.fixture(autouse=True)
def rapido(monkeypatch):
    monkeypatch.setattr(handlers, "SEND_BACKOFF_S", (0.0, 0.0))


def ask_in_bg(bot, pergunta, opcoes=(), timeout=5):
    return asyncio.create_task(bot.ask(pergunta, list(opcoes), timeout, {}))


async def settle():
    await asyncio.sleep(0.05)


def buttons(bot):
    return bot.tg.send_message.call_args.kwargs["reply_markup"].inline_keyboard


# ---------- Asks (puro) ----------


async def test_asks_texto_livre_responde_a_mais_recente_e_devolve_a_pergunta():
    asks = Asks()
    a, fa = asks.create([], "primeira?")
    b, fb = asks.create([], "segunda?")
    assert asks.has_open() and asks.answer_free_text("resp") == "segunda?"
    assert fb.result() == "resp" and not fa.done()
    assert asks.answer_free_text("outra") == "primeira?"  # agora a que sobrou
    assert fa.result() == "outra"
    assert asks.answer_free_text("sem pendente") is None and not asks.has_open()


async def test_asks_outro_direciona_o_proximo_texto_para_aquela_pergunta():
    asks = Asks()
    a, fa = asks.create(["Sim", "Outro"], "qual a conta?")
    b, fb = asks.create([], "mais recente?")
    assert asks.await_text(a) is True
    assert asks.answer_free_text("personal") == "qual a conta?"  # não a mais recente
    assert fa.result() == "personal" and not fb.done()
    assert (
        asks.answer_free_text("x") == "mais recente?"
    )  # alvo consumido: volta ao padrão


async def test_asks_outro_de_pergunta_encerrada_nao_prende_o_texto():
    asks = Asks()
    a, fa = asks.create(["Outro"], "p1")
    b, fb = asks.create([], "p2")
    asks.await_text(a)
    fa.set_result("já respondida por reply")
    assert asks.answer_free_text("texto") == "p2"
    assert asks.await_text("naoexiste") is False and asks.await_text(a) is False


async def test_asks_option_text_e_question_of():
    asks = Asks()
    a, _ = asks.create(["x", "Outro"], "pergunta?")
    assert asks.option_text(a, 1) == "Outro" and asks.option_text(a, 5) is None
    assert asks.option_text("zzz", 0) is None and asks.question_of(a) == "pergunta?"
    asks.drop(a)
    assert asks.question_of(a) == "" and not asks.has_open()


# ---------- fluxo no bot ----------


async def test_o_caso_do_dono_texto_simples_responde_e_nao_diz_sessao_ocupada(tmp_path):
    bot = make_bot(tmp_path, project=str(tmp_path), session_id=SID)
    bot.runs.busy = lambda sid: True  # a sessão está rodando, esperando a resposta
    bot.run_prompt = AsyncMock()
    task = ask_in_bg(bot, "Qual é o nome do projeto?", ["Vernier", "Outro"])
    await settle()
    await bot.on_text(upd(text="Vernier"), CTX)
    assert await asyncio.wait_for(task, 2) == "Vernier"
    assert any(t == "↩️ respondido: Qual é o nome do projeto?" for t in sent_texts(bot))
    assert not any("ocupada" in t for t in sent_texts(bot))
    await settle()
    bot.run_prompt.assert_not_called()


async def test_resposta_vai_para_a_pergunta_mais_recente(tmp_path):
    bot = make_bot(tmp_path)
    t1 = ask_in_bg(bot, "antiga?")
    await settle()
    t2 = ask_in_bg(bot, "recente?")
    await settle()
    await bot.on_text(upd(text="sim"), CTX)
    assert await asyncio.wait_for(t2, 2) == "sim" and not t1.done()
    await bot.on_text(upd(text="não"), CTX)
    assert await asyncio.wait_for(t1, 2) == "não"


async def test_eco_corta_a_pergunta_em_60_caracteres(tmp_path):
    bot = make_bot(tmp_path)
    task = ask_in_bg(bot, "p" * 200)
    await settle()
    await bot.on_text(upd(text="ok"), CTX)
    await task
    eco = [t for t in sent_texts(bot) if t.startswith("↩️")][0]
    assert eco == "↩️ respondido: " + "p" * 59 + "…"


async def test_transcricao_de_voz_tambem_responde(tmp_path, monkeypatch):
    bot = make_bot(tmp_path)
    bot.tg.get_file = AsyncMock(
        return_value=SimpleNamespace(
            download_as_bytearray=AsyncMock(return_value=bytearray(b"x"))
        )
    )
    monkeypatch.setattr(handlers, "transcribe", AsyncMock(return_value="claude três"))
    bot.run_prompt = AsyncMock()
    task = ask_in_bg(bot, "qual conta?")
    await settle()
    u = upd()
    u.message.voice = SimpleNamespace(file_id="f", file_size=10)
    await bot.on_voice(u, CTX)
    assert await asyncio.wait_for(task, 2) == "claude três"
    bot.run_prompt.assert_not_called()


async def test_prefixo_de_atalho_continua_indo_para_a_sessao_do_atalho(tmp_path):
    bot = make_bot(tmp_path, project="/x/a", session_id=SID)
    bot.state.slots = {"2": {"session_id": SID2, "project": "/x/b", "account": "c1"}}
    bot.run_prompt = AsyncMock()
    task = ask_in_bg(bot, "pendente?")
    await settle()
    await bot.on_text(upd(text="2: rode os testes"), CTX)
    await settle()
    bot.run_prompt.assert_awaited_once_with(OWNER, "rode os testes", slot=2)
    assert not task.done()  # a pergunta segue esperando
    await bot.on_text(upd(text="agora sim"), CTX)  # texto sem prefixo responde
    assert await asyncio.wait_for(task, 2) == "agora sim"


async def test_prefixo_de_atalho_vazio_conta_como_resposta(tmp_path):
    bot = make_bot(tmp_path)
    bot.run_prompt = AsyncMock()
    task = ask_in_bg(bot, "lista?")
    await settle()
    await bot.on_text(
        upd(text="3: item três"), CTX
    )  # atalho 3 não existe: é texto livre
    assert await asyncio.wait_for(task, 2) == "3: item três"


async def test_comandos_e_botoes_do_teclado_nao_respondem_a_pergunta(tmp_path):
    bot = make_bot(tmp_path)
    task = ask_in_bg(bot, "pendente?")
    await settle()
    bot.cmd_status = AsyncMock()
    await bot.on_text(upd(text="ℹ️ Status"), CTX)
    bot.cmd_status.assert_awaited_once()
    assert not task.done()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_sem_pergunta_aberta_vale_o_fluxo_normal_e_a_sessao_ocupada(tmp_path):
    bot = make_bot(tmp_path, project=str(tmp_path), session_id=SID, account="c1")
    bot.run_prompt = AsyncMock()
    await bot.on_text(upd(text="um prompt"), CTX)
    await settle()
    bot.run_prompt.assert_awaited_once_with(OWNER, "um prompt")


async def test_pergunta_respondida_por_botao_nao_captura_o_proximo_texto(tmp_path):
    bot = make_bot(tmp_path)
    bot.run_prompt = AsyncMock()
    task = ask_in_bg(bot, "vai?", ["Sim", "Não"])
    await settle()
    ask_id = next(iter(bot.asks._by_id))
    await bot.on_callback(upd(data=f"q:{ask_id}:0"), CTX)
    assert await asyncio.wait_for(task, 2) == "Sim"
    await bot.on_text(upd(text="próximo prompt"), CTX)
    await settle()
    bot.run_prompt.assert_awaited_once_with(OWNER, "próximo prompt")


# ---------- opção "Outro…" ----------


@pytest.mark.parametrize(
    "opcao",
    [
        "Outro",
        "outro",
        "Outro…",
        "Outro (digitar)",
        "OUTRA",
        "Other",
        "other option",
        "  Outro",
    ],
)
async def test_tocar_em_outro_nao_resolve_e_pede_o_texto(tmp_path, opcao):
    bot = make_bot(tmp_path)
    task = ask_in_bg(bot, "Qual o nome do módulo?", ["Vernier", opcao])
    await settle()
    ask_id = next(iter(bot.asks._by_id))
    await bot.on_callback(upd(data=f"q:{ask_id}:1"), CTX)
    assert not task.done()
    assert sent_texts(bot)[-1] == "✍️ Digite sua resposta para: Qual o nome do módulo?"
    await bot.on_text(upd(text="Greenfield"), CTX)
    assert await asyncio.wait_for(task, 2) == "Greenfield"


@pytest.mark.parametrize(
    "opcao", ["Outros projetos", "Outrora", "Nenhum", "Another", "Mais outro"]
)
async def test_opcoes_que_nao_sao_outro_resolvem_normalmente(tmp_path, opcao):
    bot = make_bot(tmp_path)
    task = ask_in_bg(bot, "p?", [opcao])
    await settle()
    ask_id = next(iter(bot.asks._by_id))
    await bot.on_callback(upd(data=f"q:{ask_id}:0"), CTX)
    assert await asyncio.wait_for(task, 2) == opcao


async def test_outro_de_uma_pergunta_antiga_recebe_o_texto_mesmo_com_outra_mais_nova(
    tmp_path,
):
    bot = make_bot(tmp_path)
    t_old = ask_in_bg(bot, "pergunta antiga?", ["A", "Outro"])
    await settle()
    old_id = next(iter(bot.asks._by_id))
    t_new = ask_in_bg(bot, "pergunta nova?")
    await settle()
    await bot.on_callback(upd(data=f"q:{old_id}:1"), CTX)
    await bot.on_text(upd(text="minha resposta"), CTX)
    assert await asyncio.wait_for(t_old, 2) == "minha resposta" and not t_new.done()
    t_new.cancel()
    await asyncio.gather(t_new, return_exceptions=True)


async def test_tocar_em_outro_duas_vezes_e_depois_em_outra_opcao(tmp_path):
    bot = make_bot(tmp_path)
    task = ask_in_bg(bot, "p?", ["A", "Outro"])
    await settle()
    ask_id = next(iter(bot.asks._by_id))
    await bot.on_callback(upd(data=f"q:{ask_id}:1"), CTX)
    await bot.on_callback(upd(data=f"q:{ask_id}:1"), CTX)
    assert not task.done()
    await bot.on_callback(upd(data=f"q:{ask_id}:0"), CTX)  # mudou de ideia: toca em "A"
    assert await asyncio.wait_for(task, 2) == "A"


async def test_outro_de_pergunta_ja_encerrada_e_indice_invalido_sao_ignorados(tmp_path):
    bot = make_bot(tmp_path)
    n = len(sent_texts(bot))
    await bot.on_callback(upd(data="q:deadbeef:0"), CTX)
    task = ask_in_bg(bot, "p?", ["Outro"])
    await settle()
    ask_id = next(iter(bot.asks._by_id))
    await bot.on_callback(upd(data=f"q:{ask_id}:9"), CTX)
    assert len(sent_texts(bot)) == n + 1  # só a própria pergunta; nada de "Digite…"
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


# ---------- trancado / estranho ----------


async def test_trancado_o_texto_nao_responde_a_pergunta(tmp_path):
    bot = make_bot(tmp_path)
    task = ask_in_bg(bot, "p?", timeout=0.3)
    await settle()
    bot.totp_secret = "ABCDEFGHIJKLMNOP"
    await bot.on_text(upd(text="resposta"), CTX)
    assert sent_texts(bot)[-1] == "🔒 trancado — /destravar <código>"
    assert await task is None  # só expirou


async def test_estranho_nao_responde_a_pergunta_do_dono(tmp_path):
    bot = make_bot(tmp_path)
    task = ask_in_bg(bot, "p?", timeout=0.3)
    await settle()
    n = len(sent_texts(bot))
    await bot.on_text(upd(user=222, text="intruso"), CTX)
    assert len(sent_texts(bot)) == n
    assert await task is None
