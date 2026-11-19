from __future__ import annotations

import asyncio
import os
import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import BadRequest, RetryAfter
from telegram.ext import ApplicationHandlerStop

from bot import handlers
from bot.config import Config
from bot.handlers import (
    Bot,
    Progress,
    age,
    format_final,
    is_limit_error,
    origin_prefix,
    prefix,
    session_exists,
)
from bot.runner import RunManager
from bot.sessions import SessionInfo
from bot.state import State
from bot.usage import AccountUsage, Profile, Window

OWNER = 111
STRANGER = 222
SID = "11111111-1111-4111-8111-111111111111"
SID2 = "22222222-2222-4222-8222-222222222222"
PHONE = "+55 (11) 99999-9999"
NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)


# ---------- helpers ----------


def make_cfg(tmp_path: Path, claude_bin: Path | None = None) -> Config:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    return Config(
        bot_token="tok",
        openai_key="key",
        allowed_phone="5511999999999",
        home=home,
        state_dir=home / ".config" / "claude-telegram",
        claude_bin=claude_bin or home / ".local" / "bin" / "claude",
        local_port=28765,
    )


def make_bot(tmp_path, owner=OWNER, **state_kw) -> Bot:
    bot = Bot(make_cfg(tmp_path), State(owner_id=owner, **state_kw), RunManager())
    tg = MagicMock()
    tg.send_message = AsyncMock(return_value=SimpleNamespace(message_id=100))
    tg.edit_message_text = AsyncMock()
    tg.delete_message = AsyncMock()
    bot.tg = tg
    return bot


def upd(
    user=OWNER, chat_type="private", text=None, contact=None, reply_to=None, data=None
):
    q = None
    if data is not None:
        q = SimpleNamespace(
            data=data,
            answer=AsyncMock(),
            message=SimpleNamespace(message_id=5, chat_id=user),
        )
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user) if user is not None else None,
        effective_chat=SimpleNamespace(id=user, type=chat_type),
        message=SimpleNamespace(
            text=text,
            contact=contact,
            voice=None,
            audio=None,
            reply_to_message=SimpleNamespace(message_id=reply_to) if reply_to else None,
        ),
        callback_query=q,
    )


def sent_texts(bot) -> list[str]:
    return [
        c.args[1] if len(c.args) > 1 else c.kwargs.get("text", "")
        for c in bot.tg.send_message.call_args_list
    ]


def usage_item(name, five=10.0, state="ok") -> AccountUsage:
    return AccountUsage(
        name, name, None, Window(five, None), Window(5.0, None), "normal", state, NOW
    )


CTX = SimpleNamespace()


@pytest.fixture(autouse=True)
def sem_backoff(monkeypatch):
    monkeypatch.setattr(handlers, "SEND_BACKOFF_S", (0.0, 0.0))


# ---------- retry de envio (falha de rede transitória) ----------


async def test_send_repete_em_network_error_e_conclui(tmp_path):
    from telegram.error import NetworkError, TimedOut

    bot = make_bot(tmp_path)
    ok = SimpleNamespace(message_id=7)
    bot.tg.send_message = AsyncMock(side_effect=[NetworkError("x"), TimedOut(), ok])
    assert await bot.send(OWNER, "oi") is ok
    assert bot.tg.send_message.await_count == 3


async def test_send_desiste_apos_3_tentativas(tmp_path):
    from telegram.error import NetworkError

    bot = make_bot(tmp_path)
    bot.tg.send_message = AsyncMock(side_effect=NetworkError("fora"))
    assert await bot.send(OWNER, "oi") is None
    assert bot.tg.send_message.await_count == 3


async def test_send_nao_repete_bad_request_nem_forbidden(tmp_path):
    from telegram.error import BadRequest, Forbidden, InvalidToken

    for exc in (BadRequest("x"), Forbidden("x"), InvalidToken("x")):
        bot = make_bot(tmp_path)
        bot.tg.send_message = AsyncMock(side_effect=exc)
        assert await bot.send(OWNER, "oi") is None
        assert bot.tg.send_message.await_count == 1


async def test_send_respeita_retry_after(tmp_path, monkeypatch):
    from telegram.error import RetryAfter

    waits = []

    async def fake_sleep(d):
        waits.append(d)

    monkeypatch.setattr(handlers.asyncio, "sleep", fake_sleep)
    bot = make_bot(tmp_path)
    ok = SimpleNamespace(message_id=7)
    bot.tg.send_message = AsyncMock(side_effect=[RetryAfter(4), ok])
    assert await bot.send(OWNER, "oi") is ok
    assert waits == [4]


async def test_send_backoff_1s_e_3s(tmp_path, monkeypatch):
    from telegram.error import NetworkError

    waits = []

    async def fake_sleep(d):
        waits.append(d)

    monkeypatch.setattr(handlers, "SEND_BACKOFF_S", (1.0, 3.0))
    monkeypatch.setattr(handlers.asyncio, "sleep", fake_sleep)
    bot = make_bot(tmp_path)
    bot.tg.send_message = AsyncMock(side_effect=NetworkError("x"))
    await bot.send(OWNER, "oi")
    assert waits == [1.0, 3.0]


async def test_log_de_retry_nao_vaza_token(tmp_path, caplog):
    from telegram.error import NetworkError

    token = "123456789:AAH-segredo_do_bot_0123456789abc"
    bot = make_bot(tmp_path)
    bot.tg.send_message = AsyncMock(
        side_effect=NetworkError(
            f"httpx.ConnectError: https://api.telegram.org/bot{token}/sendMessage"
        )
    )
    with caplog.at_level("WARNING"):
        await bot.send(OWNER, "oi")
    assert token not in caplog.text and "NetworkError" in caplog.text


async def test_progress_edit_repete_em_network_error():
    from telegram.error import NetworkError

    tg = MagicMock()
    tg.send_message = AsyncMock(return_value=SimpleNamespace(message_id=9))
    tg.edit_message_text = AsyncMock(side_effect=[NetworkError("x"), None])
    clock = Clock()
    p = Progress(tg, 1, clock=clock)
    await p.start("x")
    clock.t += 6
    await p.update("y")
    assert tg.edit_message_text.await_count == 2
    assert p.shown == "y"


# ---------- auth gating ----------


@pytest.mark.parametrize(
    "method",
    [
        "cmd_status",
        "cmd_contas",
        "cmd_conta",
        "cmd_projetos",
        "cmd_sessoes",
        "cmd_nova",
        "cmd_parar",
        "on_text",
        "on_voice",
        "on_callback",
    ],
)
async def test_estranho_ignorado_sem_resposta(tmp_path, method):
    bot = make_bot(tmp_path)
    await getattr(bot, method)(upd(user=STRANGER, text="oi", data="a:x"), CTX)
    bot.tg.send_message.assert_not_called()


async def test_chat_de_grupo_ignorado_ate_pro_dono(tmp_path):
    bot = make_bot(tmp_path)
    await bot.cmd_status(upd(user=OWNER, chat_type="supergroup"), CTX)
    await bot.on_text(upd(user=OWNER, chat_type="group", text="oi"), CTX)
    bot.tg.send_message.assert_not_called()


async def test_antes_do_vinculo_so_start_e_contato(tmp_path):
    bot = make_bot(tmp_path, owner=None)
    await bot.cmd_status(upd(user=STRANGER), CTX)
    await bot.on_text(upd(user=STRANGER, text="oi"), CTX)
    await bot.cmd_contas(upd(user=STRANGER), CTX)
    bot.tg.send_message.assert_not_called()


async def test_start_antes_do_vinculo_pede_contato(tmp_path):
    bot = make_bot(tmp_path, owner=None)
    await bot.cmd_start(upd(user=STRANGER), CTX)
    bot.tg.send_message.assert_awaited_once()
    markup = bot.tg.send_message.call_args.kwargs["reply_markup"]
    assert markup.keyboard[0][0].request_contact is True


async def test_start_em_grupo_antes_do_vinculo_ignorado(tmp_path):
    bot = make_bot(tmp_path, owner=None)
    await bot.cmd_start(upd(user=STRANGER, chat_type="group"), CTX)
    bot.tg.send_message.assert_not_called()


async def test_start_depois_do_vinculo_estranho_ignorado(tmp_path):
    bot = make_bot(tmp_path)
    await bot.cmd_start(upd(user=STRANGER), CTX)
    bot.tg.send_message.assert_not_called()


async def test_contato_proprio_vincula_e_grava_state(tmp_path):
    bot = make_bot(tmp_path, owner=None)
    contact = SimpleNamespace(user_id=STRANGER, phone_number=PHONE)
    await bot.on_contact(upd(user=STRANGER, contact=contact), CTX)
    assert bot.state.owner_id == STRANGER
    f = bot.cfg.state_dir / "state.json"
    assert f.exists() and stat.S_IMODE(f.stat().st_mode) == 0o600
    assert str(STRANGER) in f.read_text()


async def test_contato_de_terceiro_recusado(tmp_path):
    bot = make_bot(tmp_path, owner=None)
    contact = SimpleNamespace(user_id=999, phone_number=PHONE)
    await bot.on_contact(upd(user=STRANGER, contact=contact), CTX)
    assert bot.state.owner_id is None


async def test_contato_sem_user_id_recusado(tmp_path):
    bot = make_bot(tmp_path, owner=None)
    contact = SimpleNamespace(user_id=None, phone_number=PHONE)
    await bot.on_contact(upd(user=STRANGER, contact=contact), CTX)
    assert bot.state.owner_id is None


async def test_telefone_errado_recusado(tmp_path):
    bot = make_bot(tmp_path, owner=None)
    contact = SimpleNamespace(user_id=STRANGER, phone_number="+55 11 91234-5678")
    await bot.on_contact(upd(user=STRANGER, contact=contact), CTX)
    assert bot.state.owner_id is None


async def test_contato_em_grupo_ignorado(tmp_path):
    bot = make_bot(tmp_path, owner=None)
    contact = SimpleNamespace(user_id=STRANGER, phone_number=PHONE)
    await bot.on_contact(upd(user=STRANGER, chat_type="group", contact=contact), CTX)
    assert bot.state.owner_id is None
    bot.tg.send_message.assert_not_called()


async def test_depois_do_vinculo_contato_nunca_revincula(tmp_path):
    bot = make_bot(tmp_path)
    contact = SimpleNamespace(user_id=STRANGER, phone_number=PHONE)
    await bot.on_contact(upd(user=STRANGER, contact=contact), CTX)
    assert bot.state.owner_id == OWNER
    own = SimpleNamespace(user_id=OWNER, phone_number=PHONE)
    await bot.on_contact(upd(user=OWNER, contact=own), CTX)
    assert bot.state.owner_id == OWNER
    bot.tg.send_message.assert_not_called()


# ---------- formatação / prefixo ----------


def test_load_system_prompt(tmp_path):
    f = tmp_path / "p.md"
    f.write_text("  texto do prompt \n")
    assert handlers.load_system_prompt(f) == "texto do prompt"
    f.write_text("  \n")
    assert handlers.load_system_prompt(f) is None
    assert handlers.load_system_prompt(tmp_path / "nao-existe.md") is None
    assert (
        handlers.load_system_prompt()
        and "AskUserQuestion" in handlers.load_system_prompt()
    )


def test_prefix():
    assert prefix("/home/you/project-a", SID) == "[project-a · 11111111]"


def test_origin_prefix():
    assert (
        origin_prefix({"cwd": "/home/a/proj", "session_id": SID}) == "[proj · 11111111]"
    )
    assert origin_prefix({"cwd": "/home/a/proj", "session_id": None}) == "[proj]"
    assert origin_prefix({"cwd": "/home/a/proj/"}) == "[proj]"
    assert origin_prefix({}) == "[?]"
    assert origin_prefix({"cwd": 5, "session_id": ["x"]}) == "[?]"


def test_age():
    assert age(timedelta(seconds=30)) == "agora"
    assert age(timedelta(minutes=5)) == "5min"
    assert age(timedelta(hours=3, minutes=10)) == "3h"
    assert age(timedelta(days=2, hours=5)) == "2d"


def info(sid, account, minutes, prompt="faça x"):
    return SessionInfo(
        sid, account, Path("/c"), "/p", NOW - timedelta(minutes=minutes), prompt
    )


def test_is_limit_error():
    assert is_limit_error("5-hour limit reached", True) is True
    assert is_limit_error("Usage exceeded", True) is True
    assert is_limit_error("rate limited", True) is True
    assert is_limit_error("limit", False) is False  # só erro
    assert is_limit_error("falhou por outro motivo", True) is False
    assert is_limit_error(None, True) is False
    assert (
        is_limit_error("could not generate or migrate", True) is False
    )  # sem borda de palavra


def test_format_final_ok_com_custo_e_prefixo():
    out = format_final(
        text="Feito",
        is_error=False,
        cost=0.0123,
        duration_ms=4500,
        pfx="[p · s]",
        stderr="",
        returncode=0,
        timed_out=False,
        stopped=False,
    )
    assert out.startswith("[p · s] ")
    assert "Feito" in out and "⏱ 4.5s · US$ 0,01" in out


def test_format_final_erro_de_saida_traz_stderr():
    out = format_final(
        text=None,
        is_error=False,
        cost=None,
        duration_ms=None,
        pfx="",
        stderr="boom\nfalhou",
        returncode=3,
        timed_out=False,
        stopped=False,
    )
    assert "❌" in out and "3" in out and "falhou" in out


def test_format_final_timeout_e_parada():
    t = format_final(None, False, None, None, "", "", -15, True, False)
    assert "30 min" in t
    s = format_final(None, False, None, None, "", "", -15, False, True)
    assert "interrompid" in s


def test_session_exists(tmp_path):
    cfg_dir = tmp_path / "c1"
    enc = "-home-x-proj"
    (cfg_dir / "projects" / enc).mkdir(parents=True)
    (cfg_dir / "projects" / enc / f"{SID}.jsonl").write_text("{}\n")
    profiles = [Profile("c0", tmp_path / "c0"), Profile("c1", cfg_dir)]
    assert session_exists(SID, "/home/x/proj", profiles) == cfg_dir
    assert session_exists(SID2, "/home/x/proj", profiles) is None
    assert session_exists(SID, "/outro", profiles) is None


# ---------- Progress ----------


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


async def test_progress_throttle_5s():
    tg = MagicMock()
    tg.send_message = AsyncMock(return_value=SimpleNamespace(message_id=9))
    tg.edit_message_text = AsyncMock()
    clock = Clock()
    p = Progress(tg, 1, clock=clock)
    await p.start("⏳ início")
    await p.update("⏳ a")  # muito cedo: não edita
    assert tg.edit_message_text.await_count == 0
    clock.t += 5.1
    await p.update("⏳ b")
    assert tg.edit_message_text.await_count == 1
    clock.t += 1
    await p.update("⏳ c")  # dentro dos 5 s
    assert tg.edit_message_text.await_count == 1
    clock.t += 5
    await p.update("⏳ c")  # mesmo texto da última tentativa pendente -> edita c
    assert tg.edit_message_text.await_count == 2
    clock.t += 6
    await p.update("⏳ c")  # texto igual ao já exibido: não edita
    assert tg.edit_message_text.await_count == 2


async def test_progress_not_modified_ignorado():
    tg = MagicMock()
    tg.send_message = AsyncMock(return_value=SimpleNamespace(message_id=9))
    tg.edit_message_text = AsyncMock(side_effect=BadRequest("Message is not modified"))
    clock = Clock()
    p = Progress(tg, 1, clock=clock)
    await p.start("x")
    clock.t += 6
    await p.update("y")  # não levanta


async def test_progress_retry_after_adia():
    tg = MagicMock()
    tg.send_message = AsyncMock(return_value=SimpleNamespace(message_id=9))
    tg.edit_message_text = AsyncMock(side_effect=[RetryAfter(30), None])
    clock = Clock()
    p = Progress(tg, 1, clock=clock)
    await p.start("x")
    clock.t += 6
    await p.update("y")  # RetryAfter(30)
    assert tg.edit_message_text.await_count == 1
    clock.t += 10
    await p.update("z")  # ainda dentro dos 30 s
    assert tg.edit_message_text.await_count == 1
    clock.t += 25
    await p.update("z")
    assert tg.edit_message_text.await_count == 2


async def test_progress_finish_apaga():
    tg = MagicMock()
    tg.send_message = AsyncMock(return_value=SimpleNamespace(message_id=9))
    tg.delete_message = AsyncMock()
    p = Progress(tg, 1, clock=Clock())
    await p.start("x")
    await p.finish()
    tg.delete_message.assert_awaited_once()


# ---------- asks (reply / botão / ciclo) ----------


async def test_reply_a_pergunta_pendente_resolve_e_nao_vira_prompt(tmp_path):
    bot = make_bot(tmp_path, project=str(tmp_path))
    task = asyncio.create_task(bot.ask("vai?", [], 60, {"cwd": "/a/proj"}))
    await asyncio.sleep(0.05)
    assert bot.asks.pending() == 1
    run_prompt = AsyncMock()
    bot.run_prompt = run_prompt
    await bot.on_text(upd(text="sim, vai", reply_to=100), CTX)
    assert await asyncio.wait_for(task, 2) == "sim, vai"
    run_prompt.assert_not_called()
    assert bot.asks.pending() == 0


async def test_texto_sem_reply_com_ask_pendente_responde_a_pergunta_e_nao_vira_prompt(
    tmp_path,
):
    bot = make_bot(tmp_path, project=str(tmp_path))
    task = asyncio.create_task(bot.ask("vai?", [], 60, {}))
    await asyncio.sleep(0.05)
    bot.run_prompt = AsyncMock()
    await bot.on_text(upd(text="outra coisa"), CTX)
    assert await asyncio.wait_for(task, 2) == "outra coisa"
    await asyncio.sleep(0.05)
    bot.run_prompt.assert_not_called()  # a sessão está esperando: não é um prompt novo
    assert bot.asks.pending() == 0


async def test_ask_botao_resolve_com_opcao(tmp_path):
    bot = make_bot(tmp_path)
    task = asyncio.create_task(
        bot.ask("qual?", ["a", "b"], 60, {"cwd": "/x/proj", "session_id": SID})
    )
    await asyncio.sleep(0.05)
    sent = bot.tg.send_message.call_args
    assert sent.args[1].startswith("[proj · 11111111]") and "qual?" in sent.args[1]
    buttons = sent.kwargs["reply_markup"].inline_keyboard
    data = [row[0].callback_data for row in buttons]
    assert len(data[0].encode()) <= 64
    await bot.on_callback(upd(data=data[1]), CTX)
    assert await asyncio.wait_for(task, 2) == "b"


async def test_ask_timeout_devolve_none(tmp_path):
    bot = make_bot(tmp_path)
    assert await bot.ask("p", [], 0.05, {}) is None
    assert bot.asks.pending() == 0
    bot.tg.edit_message_text.assert_awaited()  # marca "sem resposta"


async def test_ask_sem_dono_devolve_none(tmp_path):
    bot = make_bot(tmp_path, owner=None)
    assert await bot.ask("p", [], 5, {}) is None
    bot.tg.send_message.assert_not_called()


async def test_ask_cancelado_solta_pendente(tmp_path):
    bot = make_bot(tmp_path)
    task = asyncio.create_task(bot.ask("p", ["a"], 60, {}))
    await asyncio.sleep(0.05)
    assert bot.asks.pending() == 1
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert bot.asks.pending() == 0


async def test_ask_telegram_falha_devolve_none(tmp_path):
    from telegram.error import NetworkError

    bot = make_bot(tmp_path)
    bot.tg.send_message = AsyncMock(side_effect=NetworkError("x"))
    assert await bot.ask("p", [], 5, {}) is None
    assert bot.asks.pending() == 0


async def test_reply_a_pergunta_encerrada_nao_vira_prompt(tmp_path):
    bot = make_bot(tmp_path, project=str(tmp_path))
    assert await bot.ask("p", [], 0.05, {}) is None  # expira; msg 100 encerrada
    bot.tg.send_message.reset_mock()
    bot.run_prompt = AsyncMock()
    await bot.on_text(upd(text="tarde demais", reply_to=100), CTX)
    await asyncio.sleep(0.05)
    bot.run_prompt.assert_not_called()
    assert "encerrada" in sent_texts(bot)[0]


# ---------- updates antigos ----------


def upd_dated(date, user=OWNER, callback=False):
    u = upd(user=user, text="oi")
    u.message.date = date
    if callback:
        u.message = None
    return u


async def test_update_antigo_e_descartado_com_aviso_unico(tmp_path, monkeypatch):
    monkeypatch.setattr(handlers, "STALE_NOTICE_DELAY_S", 0.05)
    bot = make_bot(tmp_path)
    bot.now = lambda: NOW
    old = NOW - timedelta(minutes=11)
    for _ in range(3):
        with pytest.raises(ApplicationHandlerStop):
            await bot.drop_stale(upd_dated(old), CTX)
    await asyncio.sleep(0.2)
    texts = sent_texts(bot)
    assert len(texts) == 1 and "3 mensagem" in texts[0]


async def test_update_recente_passa(tmp_path):
    bot = make_bot(tmp_path)
    bot.now = lambda: NOW
    await bot.drop_stale(upd_dated(NOW - timedelta(minutes=9)), CTX)
    bot.tg.send_message.assert_not_called()


async def test_update_antigo_de_estranho_descartado_sem_aviso(tmp_path):
    bot = make_bot(tmp_path)
    bot.now = lambda: NOW
    with pytest.raises(ApplicationHandlerStop):
        await bot.drop_stale(upd_dated(NOW - timedelta(hours=2), user=STRANGER), CTX)
    await asyncio.sleep(0.05)
    bot.tg.send_message.assert_not_called()


async def test_callback_nao_e_filtrado_por_idade(tmp_path):
    bot = make_bot(tmp_path)
    bot.now = lambda: NOW
    await bot.drop_stale(upd_dated(None, callback=True), CTX)  # sem message: passa


# ---------- fix wave: sessão mais recente, timeout, erro, progresso ----------


def test_session_exists_pega_a_conta_com_jsonl_mais_novo(tmp_path):
    enc = handlers.encode_cwd("/home/x/proj")
    old, new = tmp_path / "c1", tmp_path / "c2"
    for d, mtime in ((old, 1_000_000), (new, 2_000_000)):
        (d / "projects" / enc).mkdir(parents=True)
        f = d / "projects" / enc / f"{SID}.jsonl"
        f.write_text("{}\n")
        os.utime(f, (mtime, mtime))
    profiles = [Profile("c1", old), Profile("c2", new)]
    assert session_exists(SID, "/home/x/proj", profiles) == new
    # volta para a conta antiga com cópia velha: continua valendo a mais nova
    assert session_exists(SID, "/home/x/proj", list(reversed(profiles))) == new
    # empate (mesmo arquivo via symlink): a primeira conta
    os.utime(old / "projects" / enc / f"{SID}.jsonl", (2_000_000, 2_000_000))
    assert session_exists(SID, "/home/x/proj", profiles) == old


async def test_ask_pausa_o_timeout_da_execucao_da_sessao(tmp_path):
    bot = make_bot(tmp_path)
    run = MagicMock()
    bot.runs.get = lambda sid: run if sid == SID else None
    task = asyncio.create_task(
        bot.ask("p", ["a"], 60, {"cwd": "/x", "session_id": SID})
    )
    await asyncio.sleep(0.05)
    run.pause_timeout.assert_called_once()
    run.resume_timeout.assert_not_called()
    await bot.on_callback(upd(data=f"q:{next(iter(bot.asks._by_id))}:0"), CTX)
    assert await asyncio.wait_for(task, 2) == "a"
    run.resume_timeout.assert_called_once()


async def test_ask_cancelado_retoma_o_timeout(tmp_path):
    bot = make_bot(tmp_path)
    run = MagicMock()
    bot.runs.get = lambda sid: run
    task = asyncio.create_task(bot.ask("p", [], 60, {"session_id": SID}))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    run.resume_timeout.assert_called_once()


async def test_ask_edicao_final_nao_corta_o_desfecho(tmp_path):
    bot = make_bot(tmp_path)
    task = asyncio.create_task(bot.ask("x" * 5000, [], 60, {}))
    await asyncio.sleep(0.05)
    await bot.on_text(upd(text="resposta final", reply_to=100), CTX)
    await asyncio.wait_for(task, 2)
    edited = bot.tg.edit_message_text.call_args.args[0]
    assert len(edited) <= 4096 and edited.endswith("✅ resposta final")


async def test_on_error_avisa_dono_uma_vez_por_minuto(tmp_path, caplog):
    bot = make_bot(tmp_path)
    clock = Clock()
    bot.clock = clock
    ctx = SimpleNamespace(error=ValueError("boom"))
    with caplog.at_level("ERROR"):
        await bot.on_error(upd(), ctx)
    assert sent_texts(bot) == ["⚠️ erro interno: ValueError"]
    assert "Traceback" in caplog.text or caplog.records[0].exc_info
    await bot.on_error(upd(), ctx)  # dentro de 60 s: calado
    assert len(sent_texts(bot)) == 1
    clock.t += 61
    await bot.on_error(upd(), ctx)
    assert len(sent_texts(bot)) == 2


async def test_on_error_nao_avisa_estranho_nem_erro_de_rede(tmp_path):
    from telegram.error import NetworkError

    bot = make_bot(tmp_path)
    await bot.on_error(upd(user=STRANGER), SimpleNamespace(error=ValueError("x")))
    await bot.on_error(upd(), SimpleNamespace(error=NetworkError("x")))
    await bot.on_error(object(), SimpleNamespace(error=ValueError("x")))
    bot.tg.send_message.assert_not_called()


async def test_progress_timer_publica_o_ultimo_tool_use():
    tg = MagicMock()
    tg.send_message = AsyncMock(return_value=SimpleNamespace(message_id=9))
    tg.edit_message_text = AsyncMock()
    tg.delete_message = AsyncMock()
    clock = Clock()

    async def fake_sleep(d):
        clock.t += d

    p = Progress(tg, 1, clock=clock, sleep=fake_sleep)
    await p.start("início")
    await p.update("⏳ tool B")  # dentro da janela: fica pendente
    assert tg.edit_message_text.await_count == 0
    await p._timer  # nenhum evento novo chega; o timer publica
    assert tg.edit_message_text.await_count == 1
    assert tg.edit_message_text.call_args.args[0] == "⏳ tool B"
    await p.finish()


async def test_suggest_accounts_so_ok_de_menor_uso_e_callback_valido(tmp_path):
    bot = make_bot(tmp_path)
    nome_longo = "n" * 70
    bot.usage = AsyncMock(
        return_value=[
            usage_item("atual", 90.0),
            usage_item("c2", 20.0),
            usage_item("c3", 10.0),
            usage_item("c4", 5.0, state="expired"),
            usage_item(nome_longo, 1.0),
        ]
    )
    await bot._suggest_accounts(OWNER, "atual")
    rows = bot.tg.send_message.call_args.kwargs["reply_markup"].inline_keyboard
    data = [r[0].callback_data for r in rows]
    assert data == ["a:c3", "a:c2"]  # ordem por 5h; expirada e nome gigante fora
    assert all(len(d.encode()) <= 64 for d in data)


# ---------- voz, /parar, fiação ----------


def voice_upd(file_size=10, reply_to=None):
    u = upd(reply_to=reply_to)
    u.message.voice = SimpleNamespace(file_id="f", file_size=file_size)
    return u


async def test_voz_transcreve_ecoa_e_vira_prompt(tmp_path, monkeypatch):
    bot = make_bot(tmp_path)
    bot.tg.get_file = AsyncMock(
        return_value=SimpleNamespace(
            download_as_bytearray=AsyncMock(return_value=bytearray(b"ogg"))
        )
    )
    tr = AsyncMock(return_value="olá mundo")
    monkeypatch.setattr(handlers, "transcribe", tr)
    bot.run_prompt = AsyncMock()
    await bot.on_voice(voice_upd(), CTX)
    await asyncio.sleep(0.05)
    assert "🎙️ olá mundo" in sent_texts(bot)
    assert tr.await_args.args[:3] == (b"ogg", "voice.oga", "key")
    bot.run_prompt.assert_awaited_once_with(OWNER, "olá mundo")


async def test_voz_grande_demais_avisa_sem_transcrever(tmp_path, monkeypatch):
    bot = make_bot(tmp_path)
    tr = AsyncMock()
    monkeypatch.setattr(handlers, "transcribe", tr)
    await bot.on_voice(voice_upd(file_size=30 * 1024 * 1024), CTX)
    tr.assert_not_called()
    assert "grande demais" in sent_texts(bot)[0]


async def test_reply_de_voz_resolve_ask(tmp_path, monkeypatch):
    bot = make_bot(tmp_path)
    bot.tg.get_file = AsyncMock(
        return_value=SimpleNamespace(
            download_as_bytearray=AsyncMock(return_value=bytearray(b"x"))
        )
    )
    monkeypatch.setattr(handlers, "transcribe", AsyncMock(return_value="sim"))
    bot.run_prompt = AsyncMock()
    task = asyncio.create_task(bot.ask("vai?", [], 60, {}))
    await asyncio.sleep(0.05)
    await bot.on_voice(voice_upd(reply_to=100), CTX)
    assert await asyncio.wait_for(task, 2) == "sim"
    bot.run_prompt.assert_not_called()


async def test_voz_falha_na_transcricao_avisa(tmp_path, monkeypatch):
    from bot.voice import VoiceError

    bot = make_bot(tmp_path)
    bot.tg.get_file = AsyncMock(
        return_value=SimpleNamespace(
            download_as_bytearray=AsyncMock(return_value=bytearray(b"x"))
        )
    )
    monkeypatch.setattr(
        handlers, "transcribe", AsyncMock(side_effect=VoiceError("quota"))
    )
    bot.run_prompt = AsyncMock()
    await bot.on_voice(voice_upd(), CTX)
    assert "quota" in sent_texts(bot)[0]
    bot.run_prompt.assert_not_called()


async def test_parar_interrompe_e_resultado_diz_interrompida(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    bot = make_bot(tmp_path, project=str(proj), account="c1", session_id=SID)
    bot.cfg.claude_bin = fake_claude_bin(tmp_path, "sleep")
    bot.profiles = lambda: [Profile("c1", tmp_path / "c1")]
    task = asyncio.create_task(bot.run_prompt(OWNER, "demora"))
    for _ in range(100):
        if bot.runs.busy(SID):
            break
        await asyncio.sleep(0.05)
    assert bot.runs.busy(SID)
    await bot.cmd_parar(upd(), CTX)
    await asyncio.wait_for(task, 15)
    assert any("interrompid" in t for t in sent_texts(bot))
    assert not bot.runs.busy(SID)


async def test_parar_sem_execucao(tmp_path):
    bot = make_bot(tmp_path, session_id=SID)
    await bot.cmd_parar(upd(), CTX)
    assert "Nada em execução" in sent_texts(bot)[0]


def _route(app, payload):
    from telegram import Update as TgUpdate

    u = TgUpdate.de_json(payload, app.bot)
    for h in app.handlers[0]:
        if h.check_update(u):
            return h.callback
    return None


def test_register_roteia_comando_texto_e_voz(tmp_path):
    from telegram.ext import Application, TypeHandler

    bot = make_bot(tmp_path)
    app = Application.builder().token("1:abc").build()
    from telegram import User

    app.bot._bot_user = User(id=1, first_name="b", is_bot=True, username="b_bot")
    bot.register(app)
    assert any(
        isinstance(h, TypeHandler) for h in app.handlers[-1]
    )  # filtro de antigos
    base = {
        "message_id": 1,
        "date": 1_800_000_000,
        "chat": {"id": 1, "type": "private"},
        "from": {"id": 1, "is_bot": False, "first_name": "x"},
    }

    def msg(**extra):
        return {"update_id": 1, "message": {**base, **extra}}

    cmd = msg(
        text="/status", entities=[{"type": "bot_command", "offset": 0, "length": 7}]
    )
    assert _route(app, cmd) == bot.cmd_status
    assert _route(app, msg(text="oi")) == bot.on_text
    assert (
        _route(app, msg(voice={"file_id": "a", "file_unique_id": "b", "duration": 1}))
        == bot.on_voice
    )
    contact = msg(contact={"phone_number": "1", "first_name": "x"})
    assert _route(app, contact) == bot.on_contact
    cb = {
        "update_id": 2,
        "callback_query": {
            "id": "1",
            "chat_instance": "c",
            "from": {"id": 1, "is_bot": False, "first_name": "x"},
            "data": "a:x",
        },
    }
    assert _route(app, cb) == bot.on_callback


# ---------- argv/env passados ao claude ----------


def dumping_claude(tmp_path) -> tuple[Path, Path]:
    out = tmp_path / "dump"
    out.mkdir()
    fake = Path(__file__).parent / "fake_claude.py"
    script = tmp_path / "claude"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" > "{out}/argv.txt"\n'
        f'env > "{out}/env.txt"\n'
        f'exec "{sys.executable}" "{fake}" ok\n'
    )
    script.chmod(0o755)
    return script, out


def read_dump(out: Path) -> tuple[list[str], dict[str, str]]:
    argv = (out / "argv.txt").read_text().splitlines()
    env = dict(
        line.split("=", 1)
        for line in (out / "env.txt").read_text().splitlines()
        if "=" in line
    )
    return argv, env


async def test_run_prompt_sessao_nova_usa_session_id_config_dir_e_mcp(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok-secreto-123")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secreto-456")
    proj = tmp_path / "proj"
    proj.mkdir()
    c1 = tmp_path / "c1"
    bot = make_bot(tmp_path, project=str(proj), account="c1", session_id=SID)
    bot.cfg.claude_bin, out = dumping_claude(tmp_path)
    bot.profiles = lambda: [Profile("c1", c1)]
    await bot.run_prompt(OWNER, "faça isso")
    argv, env = read_dump(out)
    assert argv[argv.index("--session-id") + 1] == SID and "--resume" not in argv
    assert argv[argv.index("--mcp-config") + 1] == str(handlers.MCP_CONFIG)
    assert handlers.MCP_CONFIG.is_absolute() and handlers.MCP_CONFIG.name == "mcp.json"
    assert argv[-2:] == ["--", "faça isso"]
    # ponte AskUserQuestion -> telegram_perguntar: system prompt do repo, antes do `--`
    sp = argv[
        argv.index("--append-system-prompt") + 1
    ]  # o dump quebra o texto por linha
    assert sp == handlers.load_system_prompt().splitlines()[0]
    assert "telegram_perguntar" in "\n".join(argv)
    assert argv.index("--append-system-prompt") < len(argv) - 2
    assert "--dangerously-skip-permissions" in argv
    assert env["CLAUDE_CONFIG_DIR"] == str(c1)
    assert env["CLAUDE_SESSION_ID"] == SID
    assert env["BOT_LOCAL_PORT"] == "28765"
    assert env["CLAUDE_TELEGRAM_STATE_DIR"] == str(bot.cfg.state_dir)
    blob = (out / "env.txt").read_text()
    assert "tok-secreto-123" not in blob and "sk-secreto-456" not in blob
    assert "TELEGRAM_BOT_TOKEN" not in env and "OPENAI_API_KEY" not in env


async def test_run_prompt_sessao_existente_usa_resume_e_conta_trocada(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    c1, c2 = tmp_path / "c1", tmp_path / "c2"
    enc = handlers.encode_cwd(str(proj))
    (c1 / "projects" / enc).mkdir(parents=True)
    (c1 / "projects" / enc / f"{SID}.jsonl").write_text("{}\n")
    c2.mkdir()
    bot = make_bot(tmp_path, project=str(proj), account="c1", session_id=SID)
    bot.cfg.claude_bin, out = dumping_claude(tmp_path)
    bot.profiles = lambda: [Profile("c1", c1), Profile("c2", c2)]
    await bot.run_prompt(OWNER, "um")
    argv, env = read_dump(out)
    assert argv[argv.index("--resume") + 1] == SID and "--session-id" not in argv
    assert env["CLAUDE_CONFIG_DIR"] == str(c1)
    await bot.switch_account(OWNER, "c2")  # troca mantendo a sessão
    await bot.run_prompt(OWNER, "dois")
    argv, env = read_dump(out)
    assert argv[argv.index("--resume") + 1] == SID
    assert env["CLAUDE_CONFIG_DIR"] == str(c2)
    assert (c2 / "projects" / enc / f"{SID}.jsonl").exists()


# ---------- notify ----------


async def test_notify_envia_com_prefixo(tmp_path):
    bot = make_bot(tmp_path)
    assert await bot.notify("acabou", {"cwd": "/a/proj", "session_id": None}) is True
    assert sent_texts(bot) == ["[proj] acabou"]


async def test_notify_sem_dono_false(tmp_path):
    bot = make_bot(tmp_path, owner=None)
    assert await bot.notify("x", {}) is False
    bot.tg.send_message.assert_not_called()


async def test_notify_telegram_falha_false(tmp_path):
    from telegram.error import NetworkError

    bot = make_bot(tmp_path)
    bot.tg.send_message = AsyncMock(side_effect=NetworkError("x"))
    assert await bot.notify("x", {}) is False


# ---------- fluxo de prompt ----------


async def test_sem_projeto_pede_projetos(tmp_path):
    bot = make_bot(tmp_path)
    await bot.run_prompt(OWNER, "faça algo")
    assert "/projetos" in sent_texts(bot)[0]
    assert bot.runs.running() == []


async def test_sem_conta_utilizavel_avisa_sem_fallback(tmp_path):
    bot = make_bot(tmp_path, project=str(tmp_path))
    bot.profiles = lambda: [Profile("c1", tmp_path / "c1")]
    bot.usage = AsyncMock(return_value=[usage_item("c1", state="expired")])
    await bot.run_prompt(OWNER, "oi")
    assert "Nenhuma conta" in sent_texts(bot)[0]
    assert bot.runs.running() == []


async def test_sessao_ocupada_avisa(tmp_path):
    bot = make_bot(tmp_path, project=str(tmp_path), account="c1", session_id=SID)
    bot.profiles = lambda: [Profile("c1", tmp_path / "c1")]
    bot.runs.busy = lambda sid: True
    await bot.run_prompt(OWNER, "oi")
    assert "ocupada" in sent_texts(bot)[0] and "/parar" in sent_texts(bot)[0]


def fake_claude_bin(tmp_path, mode="ok") -> Path:
    script = tmp_path / "claude"
    fake = Path(__file__).parent / "fake_claude.py"
    script.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake}" {mode}\n')
    script.chmod(0o755)
    return script


async def test_run_completo_entrega_resultado(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    bot = make_bot(tmp_path, project=str(proj), account="c1")
    bot.cfg.claude_bin = fake_claude_bin(tmp_path, "ok")
    bot.profiles = lambda: [Profile("c1", tmp_path / "c1")]
    await bot.run_prompt(OWNER, "faça")
    texts = sent_texts(bot)
    assert any("OK" in t and "⏱ 0.0s · US$ 0,01" in t for t in texts)
    assert bot.state.session_id is not None  # sessão criada automaticamente
    assert (bot.cfg.state_dir / "state.json").exists()
    assert bot.runs.running() == []
    # prefixo ausente: a sessão era a ativa
    assert not any(t.startswith("[proj") for t in texts)


async def test_run_exit_diferente_de_zero_mostra_stderr(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    bot = make_bot(tmp_path, project=str(proj), account="c1")
    bot.cfg.claude_bin = fake_claude_bin(tmp_path, "fail")
    bot.profiles = lambda: [Profile("c1", tmp_path / "c1")]
    await bot.run_prompt(OWNER, "faça")
    final = sent_texts(bot)[-1]
    assert "❌" in final and "erro linha 19" in final


async def test_perfil_que_sumiu_avisa(tmp_path):
    bot = make_bot(tmp_path, project=str(tmp_path))
    bot.profiles = lambda: [Profile("c1", tmp_path / "c1")]
    bot.usage = AsyncMock(return_value=[usage_item("fantasma")])
    await bot.run_prompt(OWNER, "oi")
    assert "indisponível" in sent_texts(bot)[0]


async def test_session_id_real_e_adotado_e_mapeado(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    fixture = Path(__file__).parent / "fixtures" / "stream_ok.jsonl"
    real = "00000000-0000-4000-8000-0000000000a1"
    bot = make_bot(tmp_path, project=str(proj), account="c1", session_id=SID)
    bot.cfg.claude_bin = fake_claude_bin(tmp_path, f"fixture {fixture}")
    bot.profiles = lambda: [Profile("c1", tmp_path / "c1")]
    await bot.run_prompt(OWNER, "faça")
    assert bot.state.session_id == real
    assert bot.session_accounts[real] == "c1" and real in bot.bot_sessions


async def test_session_id_real_mapeado_mesmo_se_sessao_ativa_mudou(
    tmp_path, monkeypatch
):
    proj = tmp_path / "proj"
    proj.mkdir()
    fixture = Path(__file__).parent / "fixtures" / "stream_ok.jsonl"
    real = "00000000-0000-4000-8000-0000000000a1"
    bot = make_bot(tmp_path, project=str(proj), account="c1", session_id=SID)
    bot.cfg.claude_bin = fake_claude_bin(tmp_path, f"fixture {fixture}")
    bot.profiles = lambda: [Profile("c1", tmp_path / "c1")]
    orig = Progress.start

    async def start(self, text):
        await orig(self, text)
        bot.state.session_id = SID2

    monkeypatch.setattr(Progress, "start", start)
    await bot.run_prompt(OWNER, "faça")
    assert bot.state.session_id == SID2  # não sequestra a sessão ativa
    assert bot.session_accounts[real] == "c1" and real in bot.bot_sessions


async def test_run_de_sessao_nao_ativa_leva_prefixo(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    bot = make_bot(tmp_path, project=str(proj), account="c1", session_id=SID)
    bot.cfg.claude_bin = fake_claude_bin(tmp_path, "ok")
    bot.profiles = lambda: [Profile("c1", tmp_path / "c1")]

    async def troca_sessao(*a, **k):
        bot.state.session_id = SID2  # usuário alterna enquanto roda

    # alterna logo após o launch (progress.start é chamado depois do launch)
    orig = Progress.start

    async def start(self, text):
        await orig(self, text)
        await troca_sessao()

    monkeypatch.setattr(Progress, "start", start)
    await bot.run_prompt(OWNER, "faça")
    final = [t for t in sent_texts(bot) if "OK" in t][-1]
    assert final.startswith(f"[proj · {SID[:8]}]")


# ---------- contas / troca ----------


async def test_troca_de_conta_recusada_com_sessao_em_execucao(tmp_path):
    bot = make_bot(tmp_path, project=str(tmp_path), account="c1", session_id=SID)
    bot.profiles = lambda: [
        Profile("c1", tmp_path / "c1"),
        Profile("c2", tmp_path / "c2"),
    ]
    bot.runs.busy = lambda sid: True
    await bot.switch_account(OWNER, "c2")
    assert bot.state.account == "c1"
    assert "execução" in sent_texts(bot)[0]


async def test_troca_de_conta_copia_sessao_e_confirma(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    c1, c2 = tmp_path / "c1", tmp_path / "c2"
    enc = handlers.encode_cwd(str(proj))
    (c1 / "projects" / enc).mkdir(parents=True)
    (c1 / "projects" / enc / f"{SID}.jsonl").write_text("{}\n")
    c2.mkdir()
    bot = make_bot(tmp_path, project=str(proj), account="c1", session_id=SID)
    bot.profiles = lambda: [Profile("c1", c1), Profile("c2", c2)]
    await bot.switch_account(OWNER, "c2")
    assert bot.state.account == "c2" and bot.state.session_id == SID
    assert (c2 / "projects" / enc / f"{SID}.jsonl").exists()
    assert "🔀" in sent_texts(bot)[-1] and "c2" in sent_texts(bot)[-1]


async def test_conta_inexistente_recusada(tmp_path):
    bot = make_bot(tmp_path, account="c1")
    bot.profiles = lambda: [Profile("c1", tmp_path / "c1")]
    await bot.switch_account(OWNER, "nao-existe")
    assert bot.state.account == "c1"


async def test_callback_invalido_nao_quebra(tmp_path):
    bot = make_bot(tmp_path)
    bot.profiles = lambda: []
    for data in ("", "zzz", "p:abc", "p:999", "s:../../x", "q:xx:notint", "a:"):
        await bot.on_callback(upd(data=data), CTX)


async def test_projeto_callback_define_projeto_e_limpa_sessao(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, project="/velho", session_id=SID)
    monkeypatch.setattr(handlers, "load_projects", lambda *a, **k: ["/p0", "/p1"])
    await bot.on_callback(upd(data="p:2"), CTX)  # índice 0 é a home
    assert bot.state.project == "/p1" and bot.state.session_id is None
    await bot.on_callback(upd(data="p:0"), CTX)
    assert bot.state.project == str(bot.cfg.home)


async def test_projetos_home_sempre_primeiro_e_sem_duplicar(tmp_path, monkeypatch):
    bot = make_bot(tmp_path)
    home = str(bot.cfg.home)
    monkeypatch.setattr(
        handlers, "load_projects", lambda *a, **k: ["/p0", home, "/p0", "/p1/"]
    )
    assert bot.project_list() == [home, "/p0", "/p1"]
    await bot.cmd_projetos(upd(), CTX)
    rows = bot.tg.send_message.call_args.kwargs["reply_markup"].inline_keyboard
    labels = [r[0].text for r in rows]
    assert labels == ["📁 ~ (home)", "📁 p0", "📁 p1"]
    assert [r[0].callback_data for r in rows] == ["p:0", "p:1", "p:2"]


async def test_projetos_home_marcada_quando_ativa(tmp_path, monkeypatch):
    bot = make_bot(tmp_path)
    bot.state.project = str(bot.cfg.home)
    monkeypatch.setattr(handlers, "load_projects", lambda *a, **k: [])
    await bot.cmd_projetos(upd(), CTX)
    rows = bot.tg.send_message.call_args.kwargs["reply_markup"].inline_keyboard
    assert [r[0].text for r in rows] == ["🔵 ~ (home)"]


async def test_projetos_extras_do_projects_txt(tmp_path, monkeypatch):
    bot = make_bot(tmp_path)
    home = bot.cfg.home
    (home / "meu-app").mkdir()
    (tmp_path / "fora").mkdir()
    bot.cfg.state_dir.mkdir(parents=True)
    (bot.cfg.state_dir / "projects.txt").write_text(
        "# comentário\n\n~/meu-app\n"
        f"{tmp_path / 'fora'}\n"
        "~/nao-existe\n"
        "/caminho/inexistente\n"
        "relativo/x\n"
        "~/meu-app\n"  # duplicado
    )
    monkeypatch.setattr(handlers, "load_projects", lambda *a, **k: ["/h0"])
    assert bot.project_list() == [
        str(home),
        "/h0",
        str(home / "meu-app"),
        str(tmp_path / "fora"),
    ]


def test_projetos_sem_projects_txt_so_horizon_e_home(tmp_path, monkeypatch):
    bot = make_bot(tmp_path)
    monkeypatch.setattr(handlers, "load_projects", lambda *a, **k: [])
    assert bot.project_list() == [str(bot.cfg.home)]


async def test_contas_usa_html_botoes_ordenados_e_ativa_marcada(tmp_path):
    bot = make_bot(tmp_path, account="c2")
    bot.usage = AsyncMock(
        return_value=[
            usage_item("c1", five=80.0),
            usage_item("c2", five=10.0),
            usage_item("c3", five=5.0, state="expired"),
        ]
    )
    await bot.cmd_contas(upd(), CTX)
    call = bot.tg.send_message.call_args
    assert call.kwargs["parse_mode"] == "HTML"
    assert call.args[1].startswith("<b>Contas Claude</b>")
    assert "✅ <b>c2</b>" in call.args[1]
    rows = call.kwargs["reply_markup"].inline_keyboard
    assert [r[0].callback_data for r in rows] == ["a:c2", "a:c1", "a:c3"]
    assert rows[0][0].text.startswith("✅ 🟢 c2")


async def test_nova_gera_uuid(tmp_path):
    bot = make_bot(tmp_path, project=str(tmp_path))
    await bot.cmd_nova(upd(), CTX)
    import uuid

    assert str(uuid.UUID(bot.state.session_id)) == bot.state.session_id


async def test_nova_sem_projeto_pede_projetos(tmp_path):
    bot = make_bot(tmp_path)
    await bot.cmd_nova(upd(), CTX)
    assert "/projetos" in sent_texts(bot)[0]
    assert bot.state.session_id is None
