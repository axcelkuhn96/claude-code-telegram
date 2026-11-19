"""Remote Control nos handlers: gating por conta/projeto, mensagens, /rc, /status."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bot import handlers, rc
from bot.usage import Profile
from tests.test_handlers import CTX, OWNER, STRANGER, make_bot, sent_texts, upd

PROJECT = "/p/cbs-lab"
ENV_URL = "https://claude.ai/code?environment=env_0FAKE"
UNIT = rc.unit_name("personal", PROJECT)


@pytest.fixture(autouse=True)
def rapido(monkeypatch):
    monkeypatch.setattr(handlers, "SEND_BACKOFF_S", (0.0, 0.0))
    monkeypatch.setattr(handlers, "RC_URL_POLL_S", 0.0)
    monkeypatch.setattr(handlers, "RC_URL_WAIT_S", 0.0)


class FakeRc:
    def __init__(self, start_status="started", items=None):
        self.start_status = start_status
        self.items = items if items is not None else []
        self.starts: list[tuple] = []
        self.stops: list[str] = []

    def install(self, monkeypatch):
        def start(account, config_dir, project, claude_bin, **_):
            self.starts.append((account, str(config_dir), project))
            return rc.RcResult(self.start_status, rc.unit_name(account, project))

        def stop(unit, **_):
            self.stops.append(unit)
            return True

        monkeypatch.setattr(rc, "start", start)
        monkeypatch.setattr(rc, "status", lambda **_: list(self.items))
        monkeypatch.setattr(rc, "stop", stop)


def item(project=PROJECT, account="personal", active=True, url=ENV_URL, cap="1/32"):
    return {
        "unit": rc.unit_name(account, project) + ".service",
        "account": account,
        "project": project,
        "active": active,
        "env_url": url,
        "session_urls": [],
        "capacity": cap,
    }


def rc_bot(tmp_path, monkeypatch, account="personal", **kw):
    monkeypatch.setenv("RC_ACCOUNTS", "personal")
    bot = make_bot(tmp_path, account=account, project=PROJECT, **kw)
    cfg_dir = tmp_path / "cfg3"
    bot.profiles = lambda: [
        Profile("personal", cfg_dir),
        Profile("claude", tmp_path / "c1"),
    ]
    return bot


async def settle(bot):
    for _ in range(50):
        await asyncio.sleep(0)
        if not bot._tasks:
            return
        await asyncio.gather(*list(bot._tasks))


# ---------- gating ----------


async def test_projeto_ativo_na_personal_liga_rc_e_posta_link(tmp_path, monkeypatch):
    fake = FakeRc(items=[item()])
    fake.install(monkeypatch)
    bot = rc_bot(tmp_path, monkeypatch)
    bot.rc_ensure()
    await settle(bot)
    assert fake.starts == [("personal", str(tmp_path / "cfg3"), PROJECT)]
    texts = sent_texts(bot)
    assert len(texts) == 1
    assert "🛰 Remote Control ligado em cbs-lab (personal)" in texts[0]
    assert f'<a href="{ENV_URL}">' in texts[0]


async def test_ja_rodando_nao_repete_mensagem(tmp_path, monkeypatch):
    FakeRc(start_status="already").install(monkeypatch)
    bot = rc_bot(tmp_path, monkeypatch)
    bot.rc_ensure()
    await settle(bot)
    assert sent_texts(bot) == []


async def test_conta_fora_da_lista_nao_faz_nada(tmp_path, monkeypatch):
    fake = FakeRc()
    fake.install(monkeypatch)
    bot = rc_bot(tmp_path, monkeypatch, account="claude")
    bot.rc_ensure()
    await settle(bot)
    assert fake.starts == [] and sent_texts(bot) == []


async def test_sem_projeto_ou_sem_conta_nao_faz_nada(tmp_path, monkeypatch):
    fake = FakeRc()
    fake.install(monkeypatch)
    bot = rc_bot(tmp_path, monkeypatch)
    bot.state.project = None
    bot.rc_ensure()
    bot.state.project, bot.state.account = PROJECT, None
    bot.rc_ensure()
    await settle(bot)
    assert fake.starts == []


async def test_rc_desligado_por_env_vazia(tmp_path, monkeypatch):
    fake = FakeRc()
    fake.install(monkeypatch)
    bot = rc_bot(tmp_path, monkeypatch)
    monkeypatch.setenv("RC_ACCOUNTS", "")
    bot.rc_ensure()
    await settle(bot)
    assert fake.starts == []


async def test_nao_confiado_avisa_uma_vez(tmp_path, monkeypatch):
    fake = FakeRc(start_status="untrusted")
    fake.install(monkeypatch)
    bot = rc_bot(tmp_path, monkeypatch)
    bot.rc_ensure()
    await settle(bot)
    bot.rc_ensure()
    await settle(bot)
    texts = sent_texts(bot)
    assert len(texts) == 1
    assert "projeto não confiado na personal" in texts[0]
    assert f"abra `personal` uma vez em {PROJECT} no PC" in texts[0]


async def test_erro_de_start_nao_derruba_nem_posta(tmp_path, monkeypatch):
    FakeRc(start_status="error").install(monkeypatch)
    bot = rc_bot(tmp_path, monkeypatch)
    bot.rc_ensure()
    await settle(bot)
    assert sent_texts(bot) == []
    assert bot._rc_inflight == set()


async def test_excecao_no_start_e_engolida(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(rc, "start", boom)
    bot = rc_bot(tmp_path, monkeypatch)
    bot.rc_ensure()
    await settle(bot)
    assert sent_texts(bot) == [] and bot._rc_inflight == set()


async def test_chamadas_concorrentes_disparam_um_start(tmp_path, monkeypatch):
    fake = FakeRc(start_status="already")
    fake.install(monkeypatch)
    bot = rc_bot(tmp_path, monkeypatch)
    bot.rc_ensure()
    bot.rc_ensure()
    await settle(bot)
    assert len(fake.starts) == 1


def test_sem_loop_rodando_nao_faz_nada(tmp_path, monkeypatch):
    fake = FakeRc()
    fake.install(monkeypatch)
    bot = rc_bot(tmp_path, monkeypatch)
    bot.rc_ensure()  # síncrono, sem loop
    assert fake.starts == []


# ---------- pontos de ativação ----------


async def test_nova_sessao_projeto_e_troca_de_conta_acionam(tmp_path, monkeypatch):
    fake = FakeRc(start_status="already")
    fake.install(monkeypatch)
    bot = rc_bot(tmp_path, monkeypatch)
    await bot.new_session_chat(OWNER)
    await settle(bot)
    assert len(fake.starts) == 1

    bot.project_list = lambda: ["/home/x", PROJECT, "/p/outro"]
    await bot.on_callback(upd(data="p:2"), CTX)
    await settle(bot)
    assert fake.starts[-1][2] == "/p/outro"

    assert bot.select_project(PROJECT) is None
    await settle(bot)
    assert fake.starts[-1][2] == PROJECT

    bot.state.account = "claude"
    err, _, _ = bot.apply_account("personal")
    await settle(bot)
    assert err is None and fake.starts[-1][0] == "personal"

    n = len(fake.starts)
    bot.new_session()
    await settle(bot)
    assert len(fake.starts) == n + 1


async def test_ativar_sessao_aciona(tmp_path, monkeypatch):
    fake = FakeRc(start_status="already")
    fake.install(monkeypatch)
    bot = rc_bot(tmp_path, monkeypatch)
    sid = "33333333-3333-4333-8333-333333333333"
    bot._find_session_sync = lambda s: ("/p/outro", "personal")
    assert await bot._activate_session(sid) is None
    await settle(bot)
    assert fake.starts[-1] == ("personal", str(tmp_path / "cfg3"), "/p/outro")


# ---------- /rc ----------


async def test_cmd_rc_lista_com_botoes(tmp_path, monkeypatch):
    FakeRc(items=[item(), item(project="/p/outro")]).install(monkeypatch)
    bot = rc_bot(tmp_path, monkeypatch)
    await bot.cmd_rc(upd(text="/rc"), CTX)
    (call,) = bot.tg.send_message.call_args_list
    text = call.args[1]
    assert "cbs-lab · personal" in text and "outro · personal" in text
    assert f'<a href="{ENV_URL}">abrir</a>' in text and "cap 1/32" in text
    kb = call.kwargs["reply_markup"].inline_keyboard
    labels = [r[0].text for r in kb]
    assert labels == ["⏹ cbs-lab", "⏹ outro", "⏹ todos"]
    for row in kb:
        assert len(row[0].callback_data.encode()) <= 64


async def test_cmd_rc_vazio(tmp_path, monkeypatch):
    FakeRc().install(monkeypatch)
    bot = rc_bot(tmp_path, monkeypatch)
    await bot.cmd_rc(upd(text="/rc"), CTX)
    assert "Nenhum servidor" in sent_texts(bot)[0]


async def test_cmd_rc_parar_para_todos(tmp_path, monkeypatch):
    fake = FakeRc(items=[item(), item(project="/p/outro")])
    fake.install(monkeypatch)
    bot = rc_bot(tmp_path, monkeypatch)
    ctx = type("C", (), {"args": ["parar"]})()
    await bot.cmd_rc(upd(text="/rc parar"), ctx)
    assert len(fake.stops) == 2
    assert "2 servidor(es) parado(s)" in sent_texts(bot)[0]


async def test_botao_para_so_o_servidor_e_todos(tmp_path, monkeypatch):
    fake = FakeRc(items=[item(), item(project="/p/outro")])
    fake.install(monkeypatch)
    bot = rc_bot(tmp_path, monkeypatch)
    token = UNIT.rsplit("-", 1)[1]
    await bot.on_callback(upd(data=f"rc:{token}"), CTX)
    assert fake.stops == [UNIT + ".service"]
    await bot.on_callback(upd(data="rc:all"), CTX)
    assert len(fake.stops) == 3
    await bot.on_callback(upd(data="rc:abcdef"), CTX)
    assert "já não está rodando" in sent_texts(bot)[-1]
    assert len(fake.stops) == 3


async def test_rc_respeita_dono_e_trava_totp(tmp_path, monkeypatch):
    fake = FakeRc(items=[item()])
    fake.install(monkeypatch)
    bot = rc_bot(tmp_path, monkeypatch)
    await bot.cmd_rc(upd(user=STRANGER, text="/rc"), CTX)
    assert sent_texts(bot) == []
    locked = rc_bot(tmp_path, monkeypatch)
    locked.totp_secret = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
    locked.state.unlocked_until = 0
    await locked.cmd_rc(upd(text="/rc"), CTX)
    assert all("Remote Control" not in t for t in sent_texts(locked))
    await locked.on_callback(upd(data="rc:all"), CTX)
    assert fake.stops == []


# ---------- /status e ajuda ----------


async def test_status_mostra_contagem(tmp_path, monkeypatch):
    FakeRc(items=[item(), item(project="/p/outro", active=False)]).install(monkeypatch)
    bot = rc_bot(tmp_path, monkeypatch)

    async def usage():
        return []

    bot.usage = usage
    await bot.cmd_status(upd(text="/status"), CTX)
    assert "🛰 RC: 1 servidor(es)" in sent_texts(bot)[0]


def test_help_e_comandos_citam_rc():
    assert "🛰 RC" in handlers.HELP
    assert "rc" in dict(handlers.COMMANDS)
