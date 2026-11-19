"""Fiação do uso: state_dir persistente, conta "ociosa" e login perto de vencer no /status."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from bot import handlers
from bot.usage import AccountUsage, Window
from tests.test_handlers import CTX, make_bot, sent_texts, upd

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)


def item(name="c1", state="ok", login_in=None, five=10.0):
    return AccountUsage(
        name,
        name,
        None,
        Window(five, None),
        Window(5.0, None),
        "normal",
        state,
        NOW,
        login_expires_at=None if login_in is None else NOW + login_in,
    )


async def test_usage_passa_state_dir_ao_get_all_usage(tmp_path, monkeypatch):
    bot = make_bot(tmp_path)
    seen = {}

    async def fake(*args, **kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(handlers, "get_all_usage", fake)
    await bot.usage()
    assert seen["state_dir"] == bot.cfg.state_dir and seen["home"] == bot.cfg.home


async def test_status_avisa_login_perto_de_vencer(tmp_path):
    bot = make_bot(tmp_path, account="c1")
    bot.now = lambda: NOW
    bot.usage = AsyncMock(return_value=[item(login_in=timedelta(days=5, hours=3))])
    await bot.cmd_status(upd(), CTX)
    assert "⚠️ Login da conta c1 vence em 5 d" in sent_texts(bot)[-1]


async def test_status_login_vencido_e_longe_e_conta_ociosa(tmp_path):
    bot = make_bot(tmp_path, account="c1")
    bot.now = lambda: NOW
    bot.usage = AsyncMock(return_value=[item(login_in=timedelta(days=-1))])
    await bot.cmd_status(upd(), CTX)
    assert "Login da conta c1 venceu" in sent_texts(bot)[-1]
    bot.usage = AsyncMock(return_value=[item(login_in=timedelta(days=90))])
    await bot.cmd_status(upd(), CTX)
    assert "Login" not in sent_texts(bot)[-1]
    bot.usage = AsyncMock(
        return_value=[item(state="idle")]
    )  # ociosa, sem data de login
    await bot.cmd_status(upd(), CTX)
    assert "Login" not in sent_texts(bot)[-1]


async def test_status_sem_conta_ou_com_uso_quebrado_nao_falha(tmp_path):
    bot = make_bot(tmp_path)
    bot.usage = AsyncMock(side_effect=RuntimeError("rede"))
    await bot.cmd_status(upd(), CTX)  # sem conta ativa: nem consulta
    bot.usage.assert_not_called()
    bot.state.account = "c1"
    await bot.cmd_status(upd(), CTX)
    assert "Projeto:" in sent_texts(bot)[-1]
