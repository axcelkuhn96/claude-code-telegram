"""Mini App: acompanhar uma sessão hospedada (Remote Control) ao vivo, sem mensagens no chat."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from bot import handlers
from tests.test_handlers import OWNER
from tests.test_hosted_handlers import (  # noqa: F401  (rapido é autouse neste módulo também)
    END,
    close,
    j_assistant,
    j_text,
    j_user,
    rapido,
    reply_ok,
    setup,
    until,
)

HSID = "2964a863-5ebb-4e2c-9004-2f3ce15a55f9"
NAME_H = "cc-2964a863"


def j_tool_use(**inp):
    return {"type": "tool_use", "id": "tx", "name": "Bash", "input": inp}


async def test_turno_do_telegram_na_hospedada_alimenta_o_run_feed(
    tmp_path, monkeypatch
):
    bot, fake, _ = setup(tmp_path, monkeypatch)
    fake.on_submit = reply_ok
    await asyncio.wait_for(bot.run_prompt(OWNER, "oi"), 5)
    snap = bot.run_feed(HSID)
    assert snap["running"] is False and snap["result"]["text"] == "OK"
    kinds = [e["kind"] for e in snap["events"]]
    assert "tool" in kinds and "result" in kinds
    assert all(
        {"seq", "kind", "tool", "detail", "text"} <= set(e) for e in snap["events"]
    )
    await close(bot)


async def test_turno_de_fora_na_hospedada_alimenta_o_run_feed(tmp_path, monkeypatch):
    bot, fake, _ = setup(tmp_path, monkeypatch)
    fake.on_submit = reply_ok
    await asyncio.wait_for(bot.run_prompt(OWNER, "oi"), 5)
    fake.append(
        [
            j_user("do navegador"),
            j_assistant(j_tool_use(command="ls"), stop="tool_use"),
        ]
    )
    assert await until(lambda: bot.run_feed(HSID)["running"] is True)
    assert await until(
        lambda: any(e["kind"] == "tool" for e in bot.run_feed(HSID)["events"])
    )
    fake.append([j_assistant(j_text("fim da web")), END])
    assert await until(lambda: bot.run_feed(HSID)["running"] is False)
    snap = bot.run_feed(HSID)
    assert snap["result"]["text"] == "fim da web"
    assert snap["result"]["is_error"] is False
    seqs = [e["seq"] for e in snap["events"]]
    assert seqs == sorted(seqs)
    assert bot.run_feed(HSID, seqs[0])["events"][0]["seq"] > seqs[0]
    await close(bot)


async def test_hospedada_e_hospedada_nao_pc_e_trabalha_com_turno_aberto(
    tmp_path, monkeypatch
):
    bot, fake, project = setup(tmp_path, monkeypatch)
    fake.on_submit = reply_ok
    await asyncio.wait_for(bot.run_prompt(OWNER, "oi"), 5)
    # o TUI da sessão hospedada também consta no registro vivo do Claude Code
    bot._registry_open = lambda: [
        SimpleNamespace(
            session_id=HSID,
            cwd=project,
            account="personal",
            pid=1,
            title="",
            started_at=datetime.now(UTC),
        )
    ]

    def item():
        (it,) = [i for i in bot._api_sessions_sync("open") if i["session_id"] == HSID]
        return it

    it = item()
    assert it["hosted"] is True and it["pc"] is False and it["working"] is False
    state0 = await bot.api_state()
    assert HSID in state0["hosted"] and state0["working"] == []

    fake.append(
        [
            j_user("longo, do navegador"),
            j_assistant(j_tool_use(command="x"), stop="tool_use"),
        ]
    )
    assert await until(lambda: bot.hosts[HSID].active and item()["working"])
    it = item()
    assert it["hosted"] is True and it["pc"] is False and it["running"] is True
    st = await bot.api_state()
    assert [w["session_id"] for w in st["working"]] == [HSID]
    assert st["working"][0]["hosted"] is True and st["working"][0]["pc"] is False
    assert HSID in st["running"]

    fake.append([j_assistant(j_text("ok")), END])
    assert await until(lambda: not item()["working"])
    await close(bot)


async def test_hospedada_readotada_sem_registro_tambem_e_hospedada(
    tmp_path, monkeypatch
):
    bot, fake, project = setup(tmp_path, monkeypatch)
    fake.sessions[NAME_H] = {
        "CLAUDE_SESSION_ID": HSID,
        "CLAUDE_CONFIG_DIR": str(tmp_path / "cfg3"),
        "CLAUDE_TG_PROJECT": project,
        "CLAUDE_TG_ACCOUNT": "personal",
    }
    assert await bot.adopt_hosts() == 1
    (it,) = [i for i in await bot.api_sessions("open") if i["session_id"] == HSID]
    assert it["hosted"] is True and it["pc"] is False
    await close(bot)


def test_webapp_tem_badge_rc_e_atualizacao_da_conversa():
    html = (Path(handlers.REPO) / "webapp" / "index.html").read_text()
    assert 'id="chat-rc"' in html and "Remote Control" in html
    assert "viewedWorking()" in html and "}, 4000);" in html
    assert "loadHistory(true)" in html
    # o contador "Agora" aceita a lista (api_state) e o número
    assert "Array.isArray(st.working) ? st.working.length" in html
