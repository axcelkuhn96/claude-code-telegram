"""Markdown do Claude -> HTML do Telegram em todo texto do Claude; histórico completo; .md longo."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, NetworkError

from bot import handlers
from bot.handlers import html_to_plain
from bot.tgformat import _units
from tests.test_handlers import (
    CTX,
    OWNER,
    SID,
    fake_claude_bin,
    make_bot,
    sent_texts,
    upd,
)
from bot.usage import Profile


@pytest.fixture(autouse=True)
def rapido(monkeypatch):
    monkeypatch.setattr(handlers, "SEND_BACKOFF_S", (0.0, 0.0))


def result_fixture(tmp_path, text, sid=SID):
    f = tmp_path / "res.jsonl"
    f.write_text(
        json.dumps({"type": "system", "subtype": "init", "session_id": sid})
        + "\n"
        + json.dumps(
            {
                "type": "result",
                "is_error": False,
                "result": text,
                "session_id": sid,
                "total_cost_usd": 0.31,
                "duration_ms": 252000,
            }
        )
        + "\n"
    )
    return f


def rbot(tmp_path, text):
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    bot = make_bot(tmp_path, project=str(proj), account="c1", session_id=SID)
    bot.cfg.claude_bin = fake_claude_bin(
        tmp_path, f"fixture {result_fixture(tmp_path, text)}"
    )
    bot.profiles = lambda: [Profile("c1", tmp_path / "c1")]
    bot.tg.send_document = AsyncMock()
    return bot


def html_calls(bot):
    """(texto, parse_mode) de cada send_message."""
    return [
        (c.args[1], c.kwargs.get("parse_mode"))
        for c in bot.tg.send_message.call_args_list
    ]


def balanced(part: str) -> bool:
    stack = []
    for m in re.finditer(r"<(/?)(\w+)[^>]*>", part):
        close, name = m.group(1) == "/", m.group(2)
        if not close:
            stack.append(name)
        elif not stack or stack.pop() != name:
            return False
    return not stack


# ---------- resultado ----------


async def test_resultado_markdown_vira_html_com_parse_mode(tmp_path):
    bot = rbot(
        tmp_path,
        "Feito: **negrito**, `código` e [site](https://x.com)\n\n- item 1\n- item 2",
    )
    await bot.run_prompt(OWNER, "x")
    final = [(t, pm) for t, pm in html_calls(bot) if "negrito" in t][0]
    assert final[1] == "HTML"
    assert "<b>negrito</b>" in final[0] and "<code>código</code>" in final[0]
    assert '<a href="https://x.com">site</a>' in final[0]
    assert "• item 1" in final[0] or "- item 1" in final[0]
    assert "⏱ 4m12s · US$ 0,31" in final[0]  # rodapé segue junto


async def test_html_cru_do_claude_e_escapado(tmp_path):
    bot = rbot(tmp_path, "use <script>alert(1)</script> & <b>x</b>")
    await bot.run_prompt(OWNER, "x")
    t = [t for t, _ in html_calls(bot) if "script" in t][0]
    assert "&lt;script&gt;" in t and "<script>" not in t and "&amp;" in t


async def test_resultado_longo_divide_sem_quebrar_tags(tmp_path):
    body = (
        "**título**\n\n```python\n"
        + "\n".join(f"linha_{i} = {i}" for i in range(700))
        + "\n```\n"
    )
    bot = rbot(tmp_path, body)
    await bot.run_prompt(OWNER, "x")
    parts = [t for t, pm in html_calls(bot) if pm == "HTML"]
    assert len(parts) >= 3
    assert all(_units(p) <= 4096 and balanced(p) for p in parts)
    assert all("<pre>" in p for p in parts[1:-1])  # o bloco é reaberto em cada parte
    # tamanho abaixo de 12000: sem .md
    assert len(body) < 12000
    bot.tg.send_document.assert_not_called()


async def test_resultado_acima_de_12000_manda_partes_e_o_md_completo(tmp_path):
    texto = "\n".join(
        f"parágrafo {i} com **ênfase** e texto de enchimento" for i in range(400)
    )
    assert len(texto) > handlers.LONG_REPLY_CHARS
    bot = rbot(tmp_path, texto)
    bot.now = lambda: datetime(2026, 11, 19, 15, 7, tzinfo=UTC)
    await bot.run_prompt(OWNER, "x")
    parts = [t for t, pm in html_calls(bot) if pm == "HTML"]
    assert len(parts) >= 3 and all(_units(p) <= 4096 and balanced(p) for p in parts)
    doc = bot.tg.send_document.call_args
    assert doc.args[0] == OWNER
    assert re.fullmatch(r"resposta-11111111-\d{4}\.md", doc.kwargs["filename"])
    assert doc.kwargs["document"] == texto.encode("utf-8")  # o texto cru e completo
    assert "resposta completa" in doc.kwargs["caption"]


async def test_exatamente_no_limite_nao_manda_md(tmp_path):
    bot = rbot(tmp_path, "x" * handlers.LONG_REPLY_CHARS)
    await bot.run_prompt(OWNER, "x")
    bot.tg.send_document.assert_not_called()


async def test_md_longo_falha_no_envio_nao_derruba(tmp_path):
    bot = rbot(tmp_path, "y" * 13000)
    bot.tg.send_document = AsyncMock(side_effect=NetworkError("fora"))
    await bot.run_prompt(OWNER, "x")  # não levanta
    assert any("yyyy" in t for t, _ in html_calls(bot))


# ---------- fallback "can't parse entities" ----------


async def test_html_recusado_reenvia_so_aquela_parte_como_texto(tmp_path):
    bot = make_bot(tmp_path)
    ok = SimpleNamespace(message_id=1)
    calls = []

    async def fake_send(chat_id, text, **kw):
        calls.append((text, kw.get("parse_mode")))
        if kw.get("parse_mode") == "HTML" and "ruim" in text:
            raise BadRequest("Can't parse entities: unsupported start tag")
        return ok

    bot.tg.send_message = fake_send
    bot.tg.send_message.__name__ = "send_message"
    await bot.send_html(OWNER, "<b>bom</b>\n" + "a" * 4000 + "\n<b>ruim &amp; feio</b>")
    modos = [(t[:12], pm) for t, pm in calls]
    assert modos[0][1] == "HTML" and "<b>bom</b>" in calls[0][0]
    ruim_html = [c for c in calls if c[1] == "HTML" and "ruim" in c[0]]
    plain = [c for c in calls if c[1] is None]
    assert len(ruim_html) == 1 and len(plain) == 1
    assert plain[0][0].endswith("ruim & feio") and "<" not in plain[0][0]


async def test_outro_bad_request_nao_vira_texto_puro(tmp_path):
    bot = make_bot(tmp_path)
    bot.tg.send_message = AsyncMock(side_effect=BadRequest("Chat not found"))
    assert await bot.send_html(OWNER, "<b>x</b>") is False
    assert bot.tg.send_message.await_count == 1


async def test_markup_so_na_ultima_parte(tmp_path):
    bot = make_bot(tmp_path)
    mk = object()
    await bot.send_html(OWNER, "a" * 5000 + "\n" + "b" * 5000, mk)
    markups = [c.kwargs["reply_markup"] for c in bot.tg.send_message.call_args_list]
    assert (
        markups[-1] is mk and all(m is None for m in markups[:-1]) and len(markups) >= 3
    )


def test_html_to_plain():
    assert html_to_plain("<b>a &amp; b</b> <code>&lt;x&gt;</code>") == "a & b <x>"


# ---------- /historico completo ----------


def turn(role, text, tool=None):
    return SimpleNamespace(role=role, text=text, tool=tool, ts=None)


def with_turns(bot, monkeypatch, tmp_path, turns, cursor=None):
    monkeypatch.setattr(
        handlers.sessions_mod, "session_file", lambda *a: tmp_path / "s", raising=False
    )
    calls = []

    def read(path, limit=50, before=None):
        calls.append((limit, before))
        return turns, cursor

    monkeypatch.setattr(handlers.sessions_mod, "read_history", read, raising=False)
    return calls


async def test_historico_assistente_completo_usuario_1000_ferramenta_compacta(
    tmp_path, monkeypatch
):
    bot = make_bot(tmp_path, project=str(tmp_path), session_id=SID)
    bot.profiles = lambda: []
    longa = "\n".join(f"- ponto {i} com **destaque**" for i in range(300))  # >> 4096
    with_turns(
        bot,
        monkeypatch,
        tmp_path,
        [
            turn("user", "u" * 1500),
            turn("tool", "saída enorme " * 100, "Bash"),
            turn("assistant", longa),
        ],
    )
    await bot.cmd_historico(upd(), CTX)
    parts = [t for t, pm in html_calls(bot) if pm == "HTML"]
    assert len(parts) >= 2 and all(_units(p) <= 4096 and balanced(p) for p in parts)
    todo = "\n".join(parts)
    assert (
        "👤 " + "u" * 999 + "…" in todo and "u" * 1001 not in todo
    )  # usuário: 1000 chars
    assert re.search(
        r"🔧 <i>Bash: saída enorme[^<]{0,120}…</i>", todo
    )  # ferramenta: 1 linha
    assert (
        "ponto 299" in todo and "<b>destaque</b>" in todo
    )  # assistente: inteiro, em HTML
    assert (
        todo.index("👤") < todo.index("🔧") < todo.index("🤖")
    )  # cronológico, de cima p/ baixo


async def test_historico_pagina_anterior_vem_rotulada_e_com_novo_cursor(
    tmp_path, monkeypatch
):
    bot = make_bot(tmp_path, project=str(tmp_path), session_id=SID)
    bot.profiles = lambda: []
    calls = with_turns(
        bot,
        monkeypatch,
        tmp_path,
        [turn("user", "antiga"), turn("assistant", "resp")],
        cursor=3,
    )
    await bot.cmd_historico(upd(), CTX)
    primeira = bot.tg.send_message.call_args
    assert (
        "anteriores" not in primeira.args[1].split("\n")[0]
    )  # página mais nova: sem rótulo
    btn = primeira.kwargs["reply_markup"].inline_keyboard[0][0]
    assert btn.text == "⬅️ anteriores" and btn.callback_data == f"h:{SID}:3:6"
    await bot.on_callback(upd(data=btn.callback_data), CTX)
    assert calls[-1] == (6, 3)
    older = bot.tg.send_message.call_args
    assert older.args[1].startswith("<i>⬆️ mensagens anteriores</i>\n\n👤 antiga")
    assert older.kwargs["parse_mode"] == "HTML"
    assert (
        older.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
        == f"h:{SID}:3:6"
    )


async def test_historico_vazio(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, project=str(tmp_path), session_id=SID)
    bot.profiles = lambda: []
    with_turns(bot, monkeypatch, tmp_path, [])
    await bot.cmd_historico(upd(), CTX)
    assert "sem mensagens" in sent_texts(bot)[-1]


async def test_historico_escapa_html_do_texto_do_usuario_e_da_ferramenta(
    tmp_path, monkeypatch
):
    bot = make_bot(tmp_path, project=str(tmp_path), session_id=SID)
    bot.profiles = lambda: []
    with_turns(
        bot,
        monkeypatch,
        tmp_path,
        [turn("user", "<b>oi</b> & tchau"), turn("tool", "<x>", "Read")],
    )
    await bot.cmd_historico(upd(), CTX)
    t = sent_texts(bot)[-1]
    assert "&lt;b&gt;oi&lt;/b&gt; &amp; tchau" in t and "<i>Read: &lt;x&gt;</i>" in t


# ---------- resumo ao trocar de sessão ----------


async def test_resumo_converte_markdown_da_resposta(tmp_path, monkeypatch):
    monkeypatch.setattr(
        handlers.sessions_mod, "session_file", lambda *a: tmp_path / "s", raising=False
    )
    monkeypatch.setattr(
        handlers.sessions_mod,
        "last_exchange",
        lambda p: ("faça <x> & y", "Feito: **ok** e `cmd`\n\n- a\n- b"),
        raising=False,
    )
    bot = make_bot(tmp_path, project=str(tmp_path))
    bot.profiles = lambda: []
    await bot.show_last_exchange(OWNER, SID)
    text, pm = html_calls(bot)[-1]
    assert pm == "HTML"
    assert text.startswith(
        "Você: faça &lt;x&gt; &amp; y\nClaude: Feito: <b>ok</b> e <code>cmd</code>"
    )


async def test_resumo_longo_e_cortado_em_800_antes_de_converter(tmp_path, monkeypatch):
    monkeypatch.setattr(
        handlers.sessions_mod, "session_file", lambda *a: tmp_path / "s", raising=False
    )
    monkeypatch.setattr(
        handlers.sessions_mod,
        "last_exchange",
        lambda p: ("p", "**" + "a" * 2000 + "**"),
        raising=False,
    )
    bot = make_bot(tmp_path, project=str(tmp_path))
    bot.profiles = lambda: []
    await bot.show_last_exchange(OWNER, SID)
    text, _ = html_calls(bot)[-1]
    assert balanced(text) and len(text) < 1000
