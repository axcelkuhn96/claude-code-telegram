"""Smoke com rede e claude reais. Só roda com LIVE=1 (ver conftest). Não imprime segredos."""

from __future__ import annotations

import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from bot import config, usage
from bot.runner import Run, build_args

HORIZON_CONFIG = Path.home() / ".horizon" / "config.yaml"
HORIZON_CACHE = Path.home() / ".horizon" / "claude-usage-cache.json"
SMOKE_TIMEOUT_S = 120.0


@pytest.mark.live
async def test_getme_do_bot():
    cfg = config.load()
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"https://api.telegram.org/bot{cfg.bot_token}/getMe")
    # só status/ok: a URL (com o token) não entra em mensagem de falha
    assert resp.status_code == 200, f"getMe status {resp.status_code}"
    body = resp.json()
    assert body.get("ok") is True
    assert body["result"].get("is_bot") is True


@pytest.mark.live
async def test_runner_smoke_responda_ok():
    cfg = config.load()
    profiles = usage.load_profiles(HORIZON_CONFIG)
    usage.clear_memo()
    async with httpx.AsyncClient(timeout=10.0) as client:
        items = await usage.get_all_usage(
            profiles, HORIZON_CACHE, client, datetime.now(timezone.utc)
        )
    best = usage.pick_best(items)
    assert best is not None, "nenhuma conta ok com folga de uso"
    profile = next(p for p in profiles if p.name == best)

    args = build_args(cfg.claude_bin, "responda só OK", str(uuid.uuid4()), True)
    # sessão nova (--session-id) numa pasta temporária; o .jsonl vai pra projects/ da conta (comportamento normal do claude)
    with tempfile.TemporaryDirectory(prefix="claude-telegram-smoke-") as tmp:
        run = Run(timeout_s=SMOKE_TIMEOUT_S)
        await run.start(args, tmp, {"CLAUDE_CONFIG_DIR": str(profile.config_dir)})
        result = None
        async for ev in run.events():
            if ev.kind == "result":
                result = ev
    assert result is not None, f"sem result; stderr: {run.stderr_tail()}"
    assert not result.is_error, f"result com erro: {result.text}"
    assert "OK" in (result.text or "").upper()
