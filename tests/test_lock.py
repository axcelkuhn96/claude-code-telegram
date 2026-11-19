"""Trava TOTP: estado trancado/destravado, gating por tipo de handler, falhas, expiração, fachada."""

from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot import handlers, main as main_mod, state as state_mod, totp
from bot.runner import child_env
from tests.test_handlers import (
    CTX,
    OWNER,
    SID,
    STRANGER,
    make_bot,
    sent_texts,
    upd,
)

SECRET = base64.b32encode(b"12345678901234567890").decode()
T0 = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def sem_backoff(monkeypatch):
    monkeypatch.setattr(handlers, "SEND_BACKOFF_S", (0.0, 0.0))


class Clock:
    def __init__(self, start=T0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


def lbot(tmp_path, hours=12.0, **state_kw):
    bot = make_bot(tmp_path, **state_kw)
    bot.totp_secret = SECRET
    bot.unlock_hours = hours
    bot.now = Clock()
    return bot


def code(bot, shift=0):
    return totp.code_at(SECRET, bot.now().timestamp() + 30 * shift)


def d_upd(msg_id=77, user=OWNER, chat_type="private"):
    u = upd(user=user, chat_type=chat_type, text="/destravar")
    u.message.message_id = msg_id
    return u


def args(*a):
    return SimpleNamespace(args=list(a))


async def unlock(bot, shift=0):
    await bot.cmd_destravar(d_upd(), args(code(bot, shift)))


# ---------- sem TOTP configurado: como antes ----------


async def test_sem_totp_nunca_tranca_e_avisa_no_status(tmp_path):
    bot = make_bot(tmp_path)
    assert bot.totp_secret is None and bot.is_locked() is False
    await bot.cmd_status(upd(), CTX)
    assert "🔓 sem TOTP configurado" in sent_texts(bot)[0]
    await bot.cmd_destravar(d_upd(), args("123456"))
    assert "sem TOTP configurado" in sent_texts(bot)[-1]
    await bot.cmd_trancar(upd(), CTX)
    assert "nada a trancar" in sent_texts(bot)[-1] and bot.is_locked() is False


# ---------- trancado: gating por tipo de update ----------

LOCKED_MSG = "🔒 trancado — /destravar <código>"
GATED = [
    "cmd_status",
    "cmd_contas",
    "cmd_conta",
    "cmd_projetos",
    "cmd_sessoes",
    "cmd_nova",
    "cmd_parar",
    "cmd_historico",
    "cmd_menu",
    "cmd_trancar",
    "on_text",
    "on_voice",
    "on_file",
    "on_callback",
    "cmd_start",
]


@pytest.mark.parametrize("method", GATED)
async def test_trancado_responde_so_o_aviso(tmp_path, method):
    bot = lbot(tmp_path, project=str(tmp_path))
    bot.run_prompt = AsyncMock()
    u = upd(text="faça algo", data="a:c1" if method == "on_callback" else None)
    await getattr(bot, method)(u, CTX)
    await asyncio.sleep(0.02)
    assert sent_texts(bot) == [LOCKED_MSG]
    bot.run_prompt.assert_not_called()


async def test_callback_trancado_responde_o_toast_e_nao_executa(tmp_path):
    bot = lbot(tmp_path, account="c1")
    u = upd(data="a:c2")
    await bot.on_callback(u, CTX)
    u.callback_query.answer.assert_awaited_once_with("🔒 trancado")
    assert bot.state.account == "c1"
    assert sent_texts(bot) == [LOCKED_MSG]


async def test_botao_do_teclado_e_prompt_nao_passam_trancado(tmp_path):
    bot = lbot(tmp_path, project=str(tmp_path))
    bot.run_prompt = AsyncMock()
    bot.cmd_status = AsyncMock()
    await bot.on_text(upd(text="ℹ️ Status"), CTX)
    await bot.on_text(upd(text="rode rm -rf"), CTX)
    await asyncio.sleep(0.02)
    bot.run_prompt.assert_not_called()
    assert sent_texts(bot) == [LOCKED_MSG, LOCKED_MSG]


@pytest.mark.parametrize("method", GATED + ["cmd_destravar"])
async def test_estranho_em_qualquer_estado_fica_no_silencio(tmp_path, method):
    bot = lbot(tmp_path)
    await getattr(bot, method)(upd(user=STRANGER, text="x", data="a:x"), CTX)
    bot.tg.send_message.assert_not_called()


async def test_grupo_do_dono_ignorado_mesmo_trancado(tmp_path):
    bot = lbot(tmp_path)
    await bot.on_text(upd(chat_type="group", text="oi"), CTX)
    await bot.cmd_destravar(d_upd(chat_type="group"), args(code(bot)))
    bot.tg.send_message.assert_not_called()
    assert bot.is_locked() is True


# ---------- /destravar ----------


async def test_destravar_valido_libera_por_12h_apaga_o_codigo_e_persiste(tmp_path):
    bot = lbot(tmp_path)
    await unlock(bot)
    assert bot.is_locked() is False
    bot.tg.delete_message.assert_awaited_once_with(
        OWNER, 77
    )  # some com o código do chat
    assert sent_texts(bot)[-1].startswith("🔓 liberado até ")
    until = bot.state.unlocked_until
    assert until == pytest.approx((T0 + timedelta(hours=12)).timestamp())
    saved = state_mod.load(bot.cfg.state_dir / "state.json")
    assert saved.unlocked_until == until and saved.totp_last_step is not None
    # após um restart (mesmo estado em disco) continua liberado
    novo = lbot(tmp_path)
    novo.state = saved
    assert novo.is_locked() is False


async def test_destravar_horas_configuraveis(tmp_path):
    bot = lbot(tmp_path, hours=1.5)
    await unlock(bot)
    assert bot.state.unlocked_until == pytest.approx(
        (T0 + timedelta(hours=1.5)).timestamp()
    )


async def test_status_mostra_ate_quando_esta_liberado(tmp_path):
    bot = lbot(tmp_path)
    await unlock(bot)
    await bot.cmd_status(upd(), CTX)
    assert "🔓 liberado até" in sent_texts(bot)[-1]


async def test_destravar_aceita_codigo_do_passo_vizinho(tmp_path):
    bot = lbot(tmp_path)
    await unlock(bot, shift=-1)
    assert bot.is_locked() is False


async def test_codigo_invalido_conta_e_apaga_mesmo_assim(tmp_path):
    bot = lbot(tmp_path)
    await bot.cmd_destravar(d_upd(), args("000000"))
    assert sent_texts(bot) == ["Código inválido (1/5)"]
    bot.tg.delete_message.assert_awaited_once()
    assert bot.is_locked() is True and bot.state.unlocked_until is None
    for bad in ("abc", "12345", "1234567"):
        await bot.cmd_destravar(d_upd(), args(bad))
    assert sent_texts(bot)[-1] == "Código inválido (4/5)"


async def test_sem_codigo_mostra_uso_e_nao_conta(tmp_path):
    bot = lbot(tmp_path)
    await bot.cmd_destravar(d_upd(), args())
    assert "uso: /destravar" in sent_texts(bot)[0] and bot._totp_fails == 0


async def test_5_falhas_bloqueiam_15min_e_codigo_valido_nao_vale_no_bloqueio(tmp_path):
    bot = lbot(tmp_path)
    for _ in range(5):
        await bot.cmd_destravar(d_upd(), args("000000"))
    assert "🚨" in sent_texts(bot)[-1] and "15 min" in sent_texts(bot)[-1]
    await bot.cmd_destravar(d_upd(), args(code(bot)))  # certo, mas bloqueado
    assert "bloqueado" in sent_texts(bot)[-1] and bot.is_locked() is True
    await bot.on_text(upd(text="oi"), CTX)
    assert "bloqueado por tentativas" in sent_texts(bot)[-1]
    bot.now.advance(minutes=14)
    await bot.cmd_destravar(d_upd(), args(code(bot)))
    assert bot.is_locked() is True
    bot.now.advance(minutes=2)  # 16 min
    await unlock(bot)
    assert bot.is_locked() is False


async def test_sucesso_zera_o_contador(tmp_path):
    bot = lbot(tmp_path)
    for _ in range(4):
        await bot.cmd_destravar(d_upd(), args("000000"))
    await unlock(bot)
    await bot.cmd_trancar(upd(), CTX)
    await bot.cmd_destravar(d_upd(), args("000000"))
    assert sent_texts(bot)[-1] == "Código inválido (1/5)"


async def test_codigo_ja_usado_nao_destrava_de_novo(tmp_path):
    bot = lbot(tmp_path)
    c = code(bot)
    await bot.cmd_destravar(d_upd(), args(c))
    await bot.cmd_trancar(upd(), CTX)
    assert bot.is_locked() is True
    await bot.cmd_destravar(d_upd(), args(c))  # mesmo código, mesma janela
    assert bot.is_locked() is True and sent_texts(bot)[-1].startswith("Código inválido")
    bot.now.advance(seconds=30)  # próximo passo
    await unlock(bot)
    assert bot.is_locked() is False


async def test_reuso_persistido_sobrevive_ao_restart(tmp_path):
    bot = lbot(tmp_path)
    c = code(bot)
    await bot.cmd_destravar(d_upd(), args(c))
    await bot.cmd_trancar(upd(), CTX)
    novo = lbot(tmp_path)
    novo.state = state_mod.load(bot.cfg.state_dir / "state.json")
    await novo.cmd_destravar(d_upd(), args(c))
    assert novo.is_locked() is True


# ---------- /trancar e expiração ----------


async def test_trancar_tranca_na_hora_e_nao_para_execucoes(tmp_path):
    bot = lbot(tmp_path, session_id=SID)
    await unlock(bot)
    bot.runs.get = lambda sid: pytest.fail("não deve mexer em execuções")
    await bot.cmd_trancar(upd(), CTX)
    assert sent_texts(bot)[-1] == "🔒 trancado"
    assert bot.is_locked() is True and bot.state.unlocked_until is None
    assert state_mod.load(bot.cfg.state_dir / "state.json").unlocked_until is None
    await bot.cmd_status(upd(), CTX)
    assert sent_texts(bot)[-1] == LOCKED_MSG


async def test_expira_e_a_proxima_mensagem_recebe_o_aviso(tmp_path):
    bot = lbot(tmp_path, hours=12)
    await unlock(bot)
    bot.now.advance(hours=11, minutes=59)
    await bot.cmd_status(upd(), CTX)
    assert "🔓 liberado até" in sent_texts(bot)[-1]
    bot.now.advance(minutes=2)
    assert bot.is_locked() is True
    await bot.on_text(upd(text="oi"), CTX)
    assert sent_texts(bot)[-1] == LOCKED_MSG


async def test_start_do_dono_trancado_mostra_aviso_nao_a_ajuda(tmp_path):
    bot = lbot(tmp_path)
    await bot.cmd_start(upd(), CTX)
    assert sent_texts(bot) == [LOCKED_MSG]
    await unlock(bot)
    await bot.cmd_start(upd(), CTX)
    assert "Comandos:" in sent_texts(bot)[-1]


# ---------- fachada do Mini App ----------


async def test_fachada_nao_age_nem_revela_trancada(tmp_path):
    bot = lbot(tmp_path, project=str(tmp_path), account="c1", session_id=SID)
    bot.run_prompt = AsyncMock()
    assert bot.is_locked() is True
    assert await bot.api_state() == {"locked": True}
    assert (
        await bot.api_accounts() == []
        and bot.api_projects() == []
        and await bot.api_sessions() == []
        and await bot.api_sessions("open") == []
    )
    assert await bot.api_history(SID) == {"turns": [], "before": None}
    assert bot.run_feed(SID) == {"running": False, "events": [], "result": None}
    assert await bot.select_account("c2") == "locked"
    assert bot.select_project(str(tmp_path)) == "locked"
    assert await bot.select_session(SID) == "locked"
    assert await bot.use_session(SID) == {"error": "locked"}
    assert await bot.end_session(SID) == {"ok": False, "error": "locked"}
    assert await bot.stop_active() is False
    assert bot.new_session() == ""
    await asyncio.sleep(0.02)
    bot.run_prompt.assert_not_called()
    assert bot.state.account == "c1" and bot.state.session_id == SID


async def test_fachada_funciona_destrancada(tmp_path):
    bot = lbot(tmp_path, project=str(tmp_path), account="c1")
    await unlock(bot)
    st = await bot.api_state()
    assert st["account"] == "c1" and "locked" not in st
    assert bot.new_session()


# ---------- API local: notify/ask seguem, send_file não ----------


async def test_notify_e_ask_entregues_trancado_mas_send_file_nao(tmp_path):
    bot = lbot(tmp_path)
    assert await bot.notify("alerta", {"cwd": "/a/proj"}) is True
    assert sent_texts(bot)[-1] == "[proj] alerta"
    task = asyncio.create_task(bot.ask("vai?", ["sim"], 5, {}))
    await asyncio.sleep(0.05)
    assert "vai?" in sent_texts(bot)[-1]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    p = bot.cfg.home / "docs" / "a.txt"
    p.parent.mkdir(parents=True)
    p.write_text("x")
    bot.tg.send_document = AsyncMock()
    ok, err = await bot.send_file(str(p), None, {})
    assert ok is False and "trancado" in err
    bot.tg.send_document.assert_not_called()
    await unlock(bot)
    assert (await bot.send_file(str(p), None, {}))[0] is True


async def test_ask_nao_resolve_por_botao_trancado(tmp_path):
    bot = lbot(tmp_path)
    task = asyncio.create_task(bot.ask("vai?", ["sim"], 0.3, {}))
    await asyncio.sleep(0.05)
    ask_id = next(iter(bot.asks._by_id))
    await bot.on_callback(upd(data=f"q:{ask_id}:0"), CTX)
    assert await task is None  # só expirou: o toque trancado não respondeu


# ---------- menu button lazy ----------


async def test_menu_button_lazy_uma_vez_se_o_start_falhou(tmp_path, monkeypatch):
    from telegram.error import BadRequest

    monkeypatch.setenv("WEBAPP_URL", "https://painel.example.app")
    bot = make_bot(tmp_path)
    bot.tg.set_chat_menu_button = AsyncMock(side_effect=BadRequest("User not found"))
    assert await bot.apply_menu_button() is False  # tentativa do start falhou
    await bot.cmd_menu(upd(), CTX)  # 1º update autorizado: tenta de novo
    await asyncio.sleep(0.05)
    assert bot.tg.set_chat_menu_button.await_count == 2
    await bot.cmd_menu(upd(), CTX)
    await asyncio.sleep(0.05)
    assert bot.tg.set_chat_menu_button.await_count == 2  # só uma vez por processo
    # se o start deu certo, nem tenta
    ok = make_bot(tmp_path / "ok")
    ok.tg.set_chat_menu_button = AsyncMock()
    assert await ok.apply_menu_button() is True
    await ok.cmd_menu(upd(), CTX)
    await asyncio.sleep(0.05)
    assert ok.tg.set_chat_menu_button.await_count == 1


async def test_menu_button_lazy_funciona_trancado_e_estranho_nao_dispara(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WEBAPP_URL", "https://painel.example.app")
    bot = lbot(tmp_path)
    bot.tg.set_chat_menu_button = AsyncMock()
    await bot.on_text(upd(user=STRANGER, text="x"), CTX)
    await asyncio.sleep(0.02)
    bot.tg.set_chat_menu_button.assert_not_called()
    await bot.on_text(upd(text="x"), CTX)  # dono, trancado
    await asyncio.sleep(0.05)
    bot.tg.set_chat_menu_button.assert_awaited_once()


# ---------- main: configuração ----------


def test_totp_settings():
    assert main_mod.totp_settings({}) == (None, 12.0)
    assert main_mod.totp_settings({"TOTP_SECRET": "  "}) == (None, 12.0)
    chunks = [SECRET[i : i + 4] for i in range(0, len(SECRET), 4)]
    messy = " ".join(chunks[:3]) + "-" + "-".join(chunks[3:])
    secret, hours = main_mod.totp_settings(
        {"TOTP_SECRET": messy.lower(), "TOTP_UNLOCK_HOURS": "6"}
    )
    assert secret == SECRET and hours == 6.0
    for bad_hours in ("abc", "0", "-3", "100000"):
        assert main_mod.totp_settings({"TOTP_UNLOCK_HOURS": bad_hours})[1] == 12.0
    with pytest.raises(ValueError, match="TOTP_SECRET"):
        main_mod.totp_settings({"TOTP_SECRET": "isto não é base32!"})


def test_segredo_totp_nao_vai_para_o_env_do_claude(monkeypatch):
    monkeypatch.setenv("TOTP_SECRET", "ABCDEFGHIJKLMNOP")
    assert "TOTP_SECRET" not in child_env()


def test_state_roundtrip_e_campos_invalidos(tmp_path):
    p = tmp_path / "s.json"
    st = state_mod.State(owner_id=1, unlocked_until=123.5, totp_last_step=99)
    state_mod.save(p, st)
    assert state_mod.load(p) == st
    p.write_text('{"owner_id": 1, "unlocked_until": "amanhã", "totp_last_step": 1.5}')
    got = state_mod.load(p)
    assert got.unlocked_until is None and got.totp_last_step is None
    p.write_text('{"unlocked_until": true, "totp_last_step": true}')
    got = state_mod.load(p)
    assert got.unlocked_until is None and got.totp_last_step is None
    assert Path(p).exists()
