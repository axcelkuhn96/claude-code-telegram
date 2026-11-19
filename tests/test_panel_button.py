"""Botão "📖 Ver no painel": web_app com deep-link `<WEBAPP_URL>/#s=<sid>`."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot import handlers
from tests.test_handlers import CTX, OWNER, SID, SID2, make_bot, upd
from tests.test_md_chat import rbot, with_turns

URL = "https://painel.example.app"


@pytest.fixture(autouse=True)
def rapido(monkeypatch):
    monkeypatch.setattr(handlers, "SEND_BACKOFF_S", (0.0, 0.0))
    monkeypatch.setenv("WEBAPP_URL", URL)


def panel_buttons(markup):
    return [
        b
        for row in (markup.inline_keyboard if markup else [])
        for b in row
        if b.web_app is not None
    ]


def all_markups(bot):
    return [c.kwargs.get("reply_markup") for c in bot.tg.send_message.call_args_list]


# ---------- o helper ----------


def test_markup_web_app_com_deep_link(tmp_path):
    bot = make_bot(tmp_path)
    btns = panel_buttons(bot.panel_markup(SID))
    assert len(btns) == 1
    assert btns[0].text == "📖 Ver no painel"
    assert btns[0].web_app.url == f"{URL}/#s={SID}"
    assert (
        btns[0].callback_data is None and btns[0].url is None
    )  # é web_app, não callback


def test_barra_final_e_slash_duplo_sao_normalizados(tmp_path, monkeypatch):
    bot = make_bot(tmp_path)
    monkeypatch.setenv("WEBAPP_URL", URL + "///")
    assert panel_buttons(bot.panel_markup(SID))[0].web_app.url == f"{URL}/#s={SID}"
    monkeypatch.setenv("WEBAPP_URL", f"  {URL}  ")
    assert panel_buttons(bot.panel_markup(SID))[0].web_app.url == f"{URL}/#s={SID}"


@pytest.mark.parametrize(
    "valor", ["", "   ", "http://inseguro.example", "ftp://x", "painel.example.app"]
)
def test_sem_url_https_nao_ha_botao(tmp_path, monkeypatch, valor):
    bot = make_bot(tmp_path)
    monkeypatch.setenv("WEBAPP_URL", valor)
    assert bot.panel_markup(SID) is None


def test_sem_variavel_nao_ha_botao(tmp_path, monkeypatch):
    monkeypatch.delenv("WEBAPP_URL")
    assert make_bot(tmp_path).panel_markup(SID) is None


@pytest.mark.parametrize("sid", [None, "", "lixo", "../x", "#s=1", 5, SID + "x"])
def test_sid_invalido_nao_vira_link(tmp_path, sid):
    assert (
        make_bot(tmp_path).panel_markup(sid) is None
    )  # nada de injetar fragmento na URL


def test_linhas_existentes_vem_antes_do_botao(tmp_path, monkeypatch):
    from telegram import InlineKeyboardButton

    bot = make_bot(tmp_path)
    anteriores = [InlineKeyboardButton("⬅️ anteriores", callback_data="h:x")]
    m = bot.panel_markup(SID, [anteriores])
    assert [[b.text for b in r] for r in m.inline_keyboard] == [
        ["⬅️ anteriores"],
        ["📖 Ver no painel"],
    ]
    monkeypatch.setenv("WEBAPP_URL", "")
    m = bot.panel_markup(SID, [anteriores])  # sem painel: só as linhas que já existiam
    assert [[b.text for b in r] for r in m.inline_keyboard] == [["⬅️ anteriores"]]
    assert bot.panel_markup(SID, []) is None


# ---------- resultados ----------


async def test_resultado_leva_o_botao_so_na_ultima_parte(tmp_path):
    texto = "\n".join(f"parágrafo {i} de enchimento do texto longo" for i in range(300))
    bot = rbot(tmp_path, texto)
    await bot.run_prompt(OWNER, "x")
    msgs = [
        (c.args[1], c.kwargs.get("reply_markup"))
        for c in bot.tg.send_message.call_args_list
        if c.kwargs.get("parse_mode") == "HTML"
    ]
    assert len(msgs) >= 3
    assert all(
        panel_buttons(m) == [] for _, m in msgs[:-1]
    )  # partes do meio: sem botão
    last = panel_buttons(msgs[-1][1])
    assert len(last) == 1 and last[0].web_app.url == f"{URL}/#s={SID}"


async def test_resultado_curto_leva_o_botao_e_sem_painel_nao(tmp_path, monkeypatch):
    bot = rbot(tmp_path, "pronto")
    await bot.run_prompt(OWNER, "x")
    final = [m for m in all_markups(bot) if panel_buttons(m)]
    assert len(final) == 1
    monkeypatch.setenv("WEBAPP_URL", "")
    (tmp_path / "b").mkdir()
    bot2 = rbot(tmp_path / "b", "pronto")
    await bot2.run_prompt(OWNER, "x")
    assert not any(panel_buttons(m) for m in all_markups(bot2))


async def test_resultado_aponta_para_o_id_real_da_sessao(tmp_path):
    from pathlib import Path

    from bot.usage import Profile
    from tests.test_handlers import fake_claude_bin

    proj = tmp_path / "proj"
    proj.mkdir()
    fixture = Path(__file__).parent / "fixtures" / "stream_ok.jsonl"
    real = "00000000-0000-4000-8000-0000000000a1"
    bot = make_bot(tmp_path, project=str(proj), account="c1", session_id=SID)
    bot.cfg.claude_bin = fake_claude_bin(tmp_path, f"fixture {fixture}")
    bot.profiles = lambda: [Profile("c1", tmp_path / "c1")]
    await bot.run_prompt(OWNER, "x")
    urls = [b.web_app.url for m in all_markups(bot) for b in panel_buttons(m)]
    assert urls == [f"{URL}/#s={real}"]


# ---------- /historico ----------


def turn(role, text, tool=None):
    return SimpleNamespace(role=role, text=text, tool=tool, ts=None)


async def test_historico_cada_pagina_leva_o_botao_e_anteriores_vem_antes(
    tmp_path, monkeypatch
):
    bot = make_bot(tmp_path, project=str(tmp_path), session_id=SID)
    bot.profiles = lambda: []
    with_turns(
        bot,
        monkeypatch,
        tmp_path,
        [turn("user", "oi"), turn("assistant", "olá")],
        cursor=3,
    )
    await bot.cmd_historico(upd(), CTX)
    m = bot.tg.send_message.call_args.kwargs["reply_markup"]
    assert [[b.text for b in r] for r in m.inline_keyboard] == [
        ["⬅️ anteriores"],
        ["📖 Ver no painel"],
    ]
    assert panel_buttons(m)[0].web_app.url == f"{URL}/#s={SID}"
    await bot.on_callback(upd(data=f"h:{SID}:3:6"), CTX)  # página anterior também
    assert panel_buttons(bot.tg.send_message.call_args.kwargs["reply_markup"])


async def test_historico_ultima_pagina_so_o_botao_do_painel(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, project=str(tmp_path), session_id=SID)
    bot.profiles = lambda: []
    with_turns(bot, monkeypatch, tmp_path, [turn("user", "oi")], cursor=None)
    await bot.cmd_historico(upd(), CTX)
    m = bot.tg.send_message.call_args.kwargs["reply_markup"]
    assert [[b.text for b in r] for r in m.inline_keyboard] == [["📖 Ver no painel"]]


async def test_historico_longo_botao_so_na_ultima_parte(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, project=str(tmp_path), session_id=SID)
    bot.profiles = lambda: []
    longa = "\n".join(f"- ponto {i} com texto" for i in range(900))
    with_turns(bot, monkeypatch, tmp_path, [turn("assistant", longa)], cursor=None)
    await bot.cmd_historico(upd(), CTX)
    markups = all_markups(bot)
    assert len(markups) >= 2
    assert all(m is None for m in markups[:-1]) and panel_buttons(markups[-1])


async def test_historico_sem_painel_mantem_so_o_anteriores(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBAPP_URL", "")
    bot = make_bot(tmp_path, project=str(tmp_path), session_id=SID)
    bot.profiles = lambda: []
    with_turns(bot, monkeypatch, tmp_path, [turn("user", "oi")], cursor=3)
    await bot.cmd_historico(upd(), CTX)
    m = bot.tg.send_message.call_args.kwargs["reply_markup"]
    assert [[b.text for b in r] for r in m.inline_keyboard] == [["⬅️ anteriores"]]


# ---------- resumo ao trocar de sessão ----------


async def test_resumo_da_troca_leva_o_botao(tmp_path, monkeypatch):
    monkeypatch.setattr(
        handlers.sessions_mod, "session_file", lambda *a: tmp_path / "s", raising=False
    )
    monkeypatch.setattr(
        handlers.sessions_mod,
        "last_exchange",
        lambda p: ("pergunta", "resposta"),
        raising=False,
    )
    bot = make_bot(tmp_path, project=str(tmp_path))
    bot.profiles = lambda: []
    await bot.show_last_exchange(OWNER, SID2)
    m = bot.tg.send_message.call_args.kwargs["reply_markup"]
    assert panel_buttons(m)[0].web_app.url == f"{URL}/#s={SID2}"


async def test_troca_de_sessao_pelo_chat_manda_o_resumo_com_o_botao(
    tmp_path, monkeypatch
):
    from bot.sessions import SessionInfo
    from datetime import UTC, datetime

    monkeypatch.setattr(
        handlers.sessions_mod, "session_file", lambda *a: tmp_path / "s", raising=False
    )
    monkeypatch.setattr(
        handlers.sessions_mod, "last_exchange", lambda p: ("p", "r"), raising=False
    )
    bot = make_bot(tmp_path, project=str(tmp_path))
    bot.profiles = lambda: []
    bot.session_list = lambda: [
        SessionInfo(SID, None, tmp_path, str(tmp_path), datetime.now(UTC), "x")
    ]
    await bot.on_callback(upd(data=f"s:{SID}"), CTX)
    assert panel_buttons(bot.tg.send_message.call_args.kwargs["reply_markup"])


async def test_sem_resumo_nao_manda_mensagem_so_por_causa_do_botao(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        handlers.sessions_mod, "session_file", lambda *a: tmp_path / "s", raising=False
    )
    monkeypatch.setattr(
        handlers.sessions_mod, "last_exchange", lambda p: (None, None), raising=False
    )
    bot = make_bot(tmp_path, project=str(tmp_path))
    bot.profiles = lambda: []
    await bot.show_last_exchange(OWNER, SID)
    bot.tg.send_message.assert_not_called()
