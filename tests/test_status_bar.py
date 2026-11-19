"""Barra de status fixada (bot/pinned.py) e placeholder do teclado, ligados ao Bot."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot import handlers
from bot.pinned import PinnedStatus
from bot.usage import AccountUsage, Profile, Window
from tests.test_handlers import (
    CTX,
    OWNER,
    SID,
    fake_claude_bin,
    make_bot,
    sent_texts,
    upd,
)

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)


def usage_for(name="c1", five=9.0, seven=3.0):
    return AccountUsage(
        name, name, None, Window(five, None), Window(seven, None), "normal", "ok", NOW
    )


@pytest.fixture
def sb(tmp_path, monkeypatch):
    bot = make_bot(
        tmp_path, project=str(tmp_path / "project-a"), account="c1", session_id=SID
    )
    bot.cfg.state_dir.mkdir(parents=True, exist_ok=True)
    bot.tg.pin_chat_message = AsyncMock()
    bot.tg.send_message = AsyncMock(return_value=SimpleNamespace(message_id=500))
    bot.profiles = lambda: [Profile("c1", tmp_path / "c1")]
    bot.usage = AsyncMock(return_value=[usage_for()])
    bot.pinned_enabled = True
    bot._pinned = PinnedStatus(
        bot.tg, OWNER, bot.cfg.state_dir / "pinned.json", min_interval=0
    )
    monkeypatch.setattr(
        handlers.sessions_mod,
        "session_file",
        lambda *a: tmp_path / "s.jsonl",
        raising=False,
    )
    monkeypatch.setattr(
        handlers.sessions_mod,
        "tail_meta",
        lambda p, limit=60: ("Refatorar autenticação", "x"),
        raising=False,
    )
    monkeypatch.setattr(
        handlers.sessions_mod, "list_open", lambda profs: [], raising=False
    )
    return bot


async def settle():
    await asyncio.sleep(0.15)


async def test_start_manda_silenciosa_e_fixa_com_o_projeto_titulo_conta_e_uso(sb):
    await sb.refresh_status()
    call = sb.tg.send_message.call_args
    assert call.args[1] == "📍 project-a · Refatorar autenticação · c1 (91% livre)"
    assert call.kwargs["disable_notification"] is True
    sb.tg.pin_chat_message.assert_awaited_once()
    assert (
        sb.cfg.state_dir / "pinned.json"
    ).exists()  # guarda o id p/ editar no próximo start


async def test_save_atualiza_a_barra_sem_esperar_e_coalesce(sb):
    sb.tg.edit_message_text = AsyncMock()
    await sb.refresh_status()
    sb.state.account = "c2"
    sb.usage = AsyncMock(return_value=[usage_for("c2", five=40.0, seven=10.0)])
    for _ in range(5):  # várias mudanças seguidas
        sb.save()
    await settle()
    texts = [c.args[0] for c in sb.tg.edit_message_text.call_args_list]
    assert texts and texts[-1] == "📍 project-a · Refatorar autenticação · c2 (60% livre)"
    assert len(texts) <= 2  # coalescido, não 5 edições


async def test_trocar_projeto_sessao_e_conta_via_chat_atualiza(sb, tmp_path):
    sb.tg.edit_message_text = AsyncMock()
    await sb.refresh_status()
    sb.state.project = str(tmp_path / "outro")
    sb.save()
    await settle()
    assert sb.tg.edit_message_text.call_args.args[0].startswith("📍 outro · ")


async def test_trancar_e_destravar_mudam_o_prefixo_de_cadeado(sb):
    import base64

    from bot import totp

    sb.tg.edit_message_text = AsyncMock()
    secret = base64.b32encode(b"12345678901234567890").decode()
    sb.totp_secret = secret
    sb.state.unlocked_until = 4_000_000_000.0  # destravado
    await sb.refresh_status()
    assert not sb.tg.send_message.call_args.args[1].startswith("🔒")
    sb.now = lambda: NOW
    sb.state.unlocked_until = None
    sb.save()  # trancou
    await settle()
    assert sb.tg.edit_message_text.call_args.args[0].startswith("🔒 📍 project-a")
    msg = SimpleNamespace(message_id=9)
    sb.state.totp_last_step = None
    code = totp.code_at(secret, NOW.timestamp())
    await sb.cmd_destravar(
        SimpleNamespace(
            effective_user=SimpleNamespace(id=OWNER),
            effective_chat=SimpleNamespace(id=OWNER, type="private"),
            message=msg,
            callback_query=None,
        ),
        SimpleNamespace(args=[code]),
    )
    await settle()
    assert not sb.tg.edit_message_text.call_args.args[0].startswith("🔒")


async def test_execucao_mostra_ampulheta_com_a_contagem_e_some_ao_terminar(
    sb, tmp_path
):
    sb.tg.edit_message_text = AsyncMock()
    proj = tmp_path / "project-a"
    proj.mkdir()
    sb.cfg.claude_bin = fake_claude_bin(tmp_path, "sleep")
    await sb.refresh_status()  # a barra já existe; as mudanças seguintes viram edições
    task = asyncio.create_task(sb.run_prompt(OWNER, "demora"))
    for _ in range(100):
        if sb.runs.busy(SID):
            break
        await asyncio.sleep(0.02)
    await settle()
    assert (
        any("⏳ 1" in c.args[0] for c in sb.tg.edit_message_text.call_args_list)
        or "⏳ 1" in sb.tg.send_message.call_args_list[0].args[1]
    )
    await sb.cmd_parar(upd(), CTX)
    await asyncio.wait_for(task, 15)
    await settle()
    assert "⏳" not in sb.tg.edit_message_text.call_args.args[0]


async def test_so_liga_com_pinned_enabled_e_dono(tmp_path):
    bot = make_bot(tmp_path)
    bot.tg.pin_chat_message = AsyncMock()
    bot.save()
    await bot.refresh_status()
    await asyncio.sleep(0.05)
    bot.tg.send_message.assert_not_called()  # desligado: testes e dev não mandam nada
    sem = make_bot(tmp_path / "s", owner=None)
    sem.pinned_enabled = True
    await sem.refresh_status()
    sem.request_status_refresh()
    sem.tg.send_message.assert_not_called()


async def test_falha_do_telegram_nao_derruba_nada(sb):
    sb.tg.send_message = AsyncMock(side_effect=RuntimeError("fora"))
    await sb.refresh_status()  # não levanta
    sb.save()
    await settle()


async def test_status_loop_renova_a_cada_intervalo_e_para_ao_cancelar(sb, monkeypatch):
    monkeypatch.setattr(handlers, "STATUS_EVERY_S", 0.05)
    sb.tg.edit_message_text = AsyncMock()
    sb.usage = AsyncMock(
        side_effect=[
            [usage_for(five=1.0)],
            [usage_for(five=50.0)],
            [usage_for(five=70.0)],
        ]
        + [[usage_for(five=70.0)]] * 20
    )
    task = asyncio.create_task(sb.status_loop())
    await asyncio.sleep(0.4)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    seen = [c.args[0] for c in sb.tg.edit_message_text.call_args_list]
    assert any("(50% livre)" in t for t in seen) and any(
        "(30% livre)" in t for t in seen
    )
    assert sb.usage.await_count >= 3  # uso fresco a cada rodada


async def test_barra_usa_o_uso_da_conta_ativa_e_sem_conta_nao_mostra_percentual(sb):
    sb.state.account = None
    await sb.refresh_status()
    assert sb.tg.send_message.call_args.args[1] == "📍 project-a · Refatorar autenticação"


# ---------- placeholder do campo de texto ----------


async def test_placeholder_acompanha_projeto_conta_e_percentual(sb):
    assert (
        sb.main_keyboard().input_field_placeholder == "project-a · c1"
    )  # antes do 1º refresh
    await sb.refresh_status()
    assert sb.main_keyboard().input_field_placeholder == "project-a · c1 · 91%"
    sb.state.unlocked_until = None
    sb.totp_secret = "ABCDEFGHIJKLMNOP"
    assert sb.main_keyboard().input_field_placeholder.startswith("🔒 project-a")


async def test_placeholder_vai_em_toda_vez_que_o_teclado_e_enviado(sb):
    await sb.refresh_status()
    await sb.cmd_menu(upd(), CTX)
    markup = sb.tg.send_message.call_args.kwargs["reply_markup"]
    assert (
        markup.input_field_placeholder == "project-a · c1 · 91%"
        and markup.is_persistent is True
    )
    sb.state.slots = {"1": {"session_id": SID, "project": "/x/p", "account": "c1"}}
    sb.runs.running = lambda: []
    await sb.send_keyboard(OWNER, "x")
    assert sb.tg.send_message.call_args.kwargs["reply_markup"].input_field_placeholder


def test_placeholder_sem_nada_e_limitado_a_64(tmp_path):
    bot = make_bot(tmp_path, project=None, account=None)
    assert bot.main_keyboard().input_field_placeholder == "Mensagem"
    bot.state.project = "/x/" + "p" * 200
    assert len(bot.main_keyboard().input_field_placeholder) <= 64
