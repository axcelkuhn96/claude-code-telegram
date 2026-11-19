"""E2E real da sessão hospedada (tmux + Claude Code interativo + Remote Control). Só com LIVE=1.

Sessão NOVA e descartável na personal em ~/cbs-lab, num socket tmux próprio do teste
(`claude-tg-live`, não o do bot). Ao fim o tmux do teste é encerrado. Não imprime segredos.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest

from bot import hosted
from bot.usage import Profile
from tests.test_handlers import OWNER, make_bot, make_cfg, sent_texts

HOME = Path.home()
CLAUDE = HOME / ".local" / "bin" / "claude"
PROJECT = HOME / "cbs-lab"
CFG3 = HOME / ".claude-personal"
SECRETS = (
    b"TELEGRAM_BOT_TOKEN=",
    b"OPENAI_API_KEY=",
    b"BOT_LOCAL_SECRET=",
    b"TOTP_SECRET=",
)


@pytest.mark.live
async def test_sessao_hospedada_real_responde_ok(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RC_ACCOUNTS", "personal")
    sid = str(uuid.uuid4())
    bot = make_bot(tmp_path, account="personal", project=str(PROJECT), session_id=sid)
    bot.cfg = make_cfg(tmp_path, claude_bin=CLAUDE)
    bot.profiles = lambda: [Profile("personal", CFG3)]
    bot.tmux = hosted.Tmux(socket_name="claude-tg-live")
    try:
        await asyncio.wait_for(bot.run_prompt(OWNER, "responda só OK"), 240)
        texts = sent_texts(bot)
        host = bot.hosts[sid]
        pid = bot.tmux.pane_pid(host.name)
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        environ = Path(f"/proc/{pid}/environ").read_bytes()
        with capsys.disabled():
            print(f"\nLIVE sid={sid} tmux={host.name} pane_pid={pid}")
            print("LIVE argv:", [a.decode() for a in argv if a][1:4])
            print("LIVE rc_url:", host.rc_url)
            print("LIVE mensagens:", [t[:80] for t in texts])
        assert any(t.strip().startswith("OK") for t in texts), texts
        assert any(a.startswith(b"--remote-control=") for a in argv)
        assert not any(s in environ for s in SECRETS)
        assert host.rc_url and host.rc_url.startswith("https://claude.ai/code/")
    finally:
        for h in list(bot.hosts.values()):
            await bot._host_kill(h)
        await bot.shutdown()
        hosted.run_cmd([hosted.TMUX_BIN, "-L", "claude-tg-live", "kill-server"])


@pytest.mark.live
async def test_sessao_hospedada_real_dialogo_e_cgroup(tmp_path, monkeypatch, capsys):
    """AskUserQuestion vira mensagem com botões; o botão "2" responde no TUI. O servidor tmux
    fica num scope próprio (fora do cgroup de quem o iniciou)."""
    import time

    monkeypatch.setenv("RC_ACCOUNTS", "personal")
    sid = str(uuid.uuid4())
    bot = make_bot(tmp_path, account="personal", project=str(PROJECT), session_id=sid)
    bot.cfg = make_cfg(tmp_path, claude_bin=CLAUDE)
    bot.profiles = lambda: [Profile("personal", CFG3)]
    bot.tmux = hosted.Tmux(socket_name="claude-tg-live")
    try:
        task = asyncio.create_task(
            bot.run_prompt(
                OWNER,
                "Use a ferramenta AskUserQuestion para me perguntar uma cor "
                "(vermelho, verde ou azul). Depois responda só a cor escolhida.",
            )
        )
        end = time.monotonic() + 120
        while time.monotonic() < end:
            host = bot.hosts.get(sid)
            if host is not None and host.dialog is not None:
                break
            await asyncio.sleep(0.5)
        host = bot.hosts[sid]
        assert host.dialog is not None, sent_texts(bot)
        dialog = host.dialog
        await bot.host_answer(OWNER, sid[:8], host.dialog_seq, "2")
        await asyncio.wait_for(task, 120)
        texts = sent_texts(bot)
        _, out = hosted.run_cmd(
            [hosted.TMUX_BIN, "-L", "claude-tg-live", "display-message", "-p", "#{pid}"]
        )
        server_cgroup = Path(f"/proc/{out.strip()}/cgroup").read_text().strip()
        with capsys.disabled():
            print("\nLIVE dialog options:", dialog.options, "free:", dialog.free)
            print("LIVE dialog excerpt:", dialog.excerpt.replace("\n", " | ")[:300])
            print("LIVE mensagens:", [t[:60] for t in texts])
            print("LIVE tmux server cgroup:", server_cgroup)
        assert len(dialog.options) >= 3
        assert any("o terminal pergunta" in t for t in texts)
        chosen = dialog.options[1][1].lower()
        assert any(t.strip().lower().startswith(chosen[:4]) for t in texts), texts
        assert ".scope" in server_cgroup
    finally:
        for h in list(bot.hosts.values()):
            await bot._host_kill(h)
        await bot.shutdown()
        hosted.run_cmd([hosted.TMUX_BIN, "-L", "claude-tg-live", "kill-server"])
