from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot import main as main_mod


def fake_app(running=True, polling=True):
    return SimpleNamespace(running=running, updater=SimpleNamespace(running=polling))


def fake_bot(owner=None):
    return SimpleNamespace(state=SimpleNamespace(owner_id=owner), send=AsyncMock())


async def test_watchdog_ready_e_ping_so_com_polling_vivo(monkeypatch):
    sent = []
    monkeypatch.setattr(main_mod, "sd_notify", lambda m: sent.append(m) or True)
    app, bot = fake_app(), fake_bot(owner=7)
    task = asyncio.create_task(main_mod.watchdog(app, bot, interval=0.02))
    await asyncio.sleep(0.1)
    assert sent[0].startswith("READY=1")
    assert "WATCHDOG=1" in sent
    bot.send.assert_awaited_once_with(7, "🟢 no ar")
    app.updater.running = False  # polling morreu: para de pingar
    await asyncio.sleep(0.05)
    n = len(sent)
    await asyncio.sleep(0.1)
    assert len(sent) == n
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_watchdog_sem_dono_nao_manda_mensagem(monkeypatch):
    monkeypatch.setattr(main_mod, "sd_notify", lambda m: True)
    bot = fake_bot(owner=None)
    task = asyncio.create_task(main_mod.watchdog(fake_app(), bot, interval=0.02))
    await asyncio.sleep(0.05)
    bot.send.assert_not_called()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def test_porta_default_igual_no_bot_e_no_mcp():
    from bot import config
    from mcp_server import telegram_mcp

    assert config.DEFAULT_PORT == telegram_mcp.DEFAULT_PORT == 28765


async def test_start_api_porta_ocupada_sai_com_erro_claro(caplog):
    import pytest

    from bot.localapi import LocalApi

    async def nop(*a):
        return None

    busy = LocalApi("s", nop, nop, port=0)
    await busy.start()
    try:
        other = LocalApi("s", nop, nop, port=busy.port)
        with pytest.raises(SystemExit) as exc, caplog.at_level("ERROR"):
            await main_mod.start_api(other)
        assert exc.value.code == main_mod.EXIT_PORT_BUSY != 0
        assert "BOT_LOCAL_PORT" in caplog.text
    finally:
        await busy.stop()


async def test_watchdog_polling_tardio_ainda_manda_ready(monkeypatch):
    sent = []
    monkeypatch.setattr(main_mod, "sd_notify", lambda m: sent.append(m) or True)
    monkeypatch.setattr(
        main_mod, "POLLING_WAIT_S", 0.5
    )  # "limite" curto: não pode desistir
    monkeypatch.setattr(main_mod, "POLL_STEP_S", 0.05)
    app = fake_app(polling=False)
    task = asyncio.create_task(main_mod.watchdog(app, fake_bot(owner=3), interval=0.02))
    await asyncio.sleep(1.2)  # passou do limite sem polling
    assert not any(m.startswith("READY=1") for m in sent) and not task.done()
    assert "STATUS=aguardando rede" in sent
    app.updater.running = True  # rede voltou
    await asyncio.sleep(0.3)
    assert any(m.startswith("READY=1") for m in sent) and "WATCHDOG=1" in sent
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def test_redact_token_na_mensagem_e_no_traceback():
    import logging

    token = "123456:SEGREDO-do-bot"
    f = main_mod.RedactToken(token)
    try:
        raise RuntimeError(f"GET https://api.telegram.org/bot{token}/getUpdates falhou")
    except RuntimeError:
        import sys

        rec = logging.LogRecord(
            "x", logging.ERROR, __file__, 1, "erro %s", (f"bot{token}",), sys.exc_info()
        )
    assert f.filter(rec) is True
    out = logging.Formatter("%(message)s").format(rec)
    assert token not in out and "***" in out and "RuntimeError" in out


async def test_watchdog_sem_polling_nao_manda_ready(monkeypatch):
    sent = []
    monkeypatch.setattr(main_mod, "sd_notify", lambda m: sent.append(m) or True)
    task = asyncio.create_task(
        main_mod.watchdog(fake_app(polling=False), fake_bot(), interval=0.02)
    )
    await asyncio.sleep(0.8)
    assert sent == []
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
