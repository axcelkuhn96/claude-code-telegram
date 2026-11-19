from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from bot.runner import (
    MAX_SYSTEM_PROMPT_BYTES,
    Run,
    RunManager,
    build_args,
    child_env,
    load_system_prompt,
)

HERE = Path(__file__).parent
FAKE = str(HERE / "fake_claude.py")
FIXTURES = HERE / "fixtures"


def fake(*mode: str) -> list[str]:
    return [sys.executable, FAKE, *mode]


async def collect(run: Run):
    return [e async for e in run.events()]


# ---- build_args ----


def test_build_args_new_session():
    args = build_args(Path("/bin/claude"), "oi", "sid-1", new=True)
    assert args[0] == "/bin/claude"
    assert args[1] == "-p"
    assert args[args.index("--session-id") + 1] == "sid-1"
    assert "--resume" not in args
    assert args[args.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in args and "--dangerously-skip-permissions" in args
    assert all(isinstance(a, str) for a in args)


def test_build_args_resume_and_extra():
    args = build_args(
        "/bin/claude",
        "oi",
        "sid-2",
        new=False,
        extra_args=["--mcp-config", "/x/mcp.json"],
    )
    assert args[args.index("--resume") + 1] == "sid-2"
    assert "--session-id" not in args
    assert args[args.index("--mcp-config") + 1] == "/x/mcp.json"


def test_build_args_prompt_last_after_double_dash():
    # prompt começando com "-" não vira opção; --mcp-config variádico não engole o prompt
    args = build_args(
        "/bin/claude",
        "--evil -x",
        "sid",
        new=False,
        extra_args=["--mcp-config", "/x.json"],
    )
    assert args[-2:] == ["--", "--evil -x"]
    assert args.index("--mcp-config") < args.index("--")


def test_build_args_system_prompt_before_double_dash():
    args = build_args(
        "/bin/claude",
        "oi",
        "sid",
        new=True,
        extra_args=["--mcp-config", "/x.json"],
        system_prompt="regras do telegram",
    )
    i = args.index("--append-system-prompt")
    assert args[i + 1] == "regras do telegram"
    assert i < args.index("--")
    assert args[-2:] == ["--", "oi"]


def test_build_args_system_prompt_none_or_empty_omits_flag():
    assert "--append-system-prompt" not in build_args("/bin/claude", "oi", "s", True)
    assert "--append-system-prompt" not in build_args(
        "/bin/claude", "oi", "s", True, system_prompt=None
    )
    assert "--append-system-prompt" not in build_args(
        "/bin/claude", "oi", "s", True, system_prompt=""
    )


# ---- load_system_prompt ----


def test_load_system_prompt_reads_text(tmp_path):
    f = tmp_path / "p.md"
    f.write_text("  você fala pt-BR\n", encoding="utf-8")
    assert load_system_prompt(f) == "você fala pt-BR"


def test_load_system_prompt_missing_or_empty_is_none(tmp_path):
    assert load_system_prompt(tmp_path / "nao-existe.md") is None
    empty = tmp_path / "vazio.md"
    empty.write_text(" \n\n", encoding="utf-8")
    assert load_system_prompt(empty) is None


def test_load_system_prompt_size_cap(tmp_path):
    f = tmp_path / "grande.md"
    f.write_text("a" * (MAX_SYSTEM_PROMPT_BYTES + 1), encoding="utf-8")
    assert load_system_prompt(f) is None
    ok = tmp_path / "limite.md"
    ok.write_text("a" * MAX_SYSTEM_PROMPT_BYTES, encoding="utf-8")
    assert load_system_prompt(ok) == "a" * MAX_SYSTEM_PROMPT_BYTES


def test_load_system_prompt_directory_or_bad_utf8_is_none(tmp_path):
    assert load_system_prompt(tmp_path) is None
    bad = tmp_path / "bad.md"
    bad.write_bytes(b"\xff\xfe\xfa")
    assert load_system_prompt(bad) is None


# ---- Run ----


async def test_run_ok_events_and_returncode(tmp_path):
    run = Run()
    await run.start(fake("ok"), str(tmp_path), {})
    events = await collect(run)
    assert [e.kind for e in events] == ["init", "tool", "result"]
    assert events[1].tool == "Bash" and events[1].detail == "ls"
    assert events[2].text == "OK"
    assert run.returncode == 0
    assert run.timed_out is False


async def test_run_parses_real_fixture(tmp_path):
    run = Run()
    await run.start(
        fake("fixture", str(FIXTURES / "stream_tool.jsonl")), str(tmp_path), {}
    )
    events = await collect(run)
    assert events[-1].kind == "result" and events[-1].text == "OK"
    assert any(e.kind == "tool" and e.tool == "Bash" for e in events)


async def test_run_failure_stderr_tail(tmp_path):
    run = Run()
    await run.start(fake("fail"), str(tmp_path), {})
    assert await collect(run) == []
    assert run.returncode == 3
    tail = run.stderr_tail(5).splitlines()
    assert tail == [f"erro linha {i}" for i in range(15, 20)]
    assert len(run.stderr_tail().splitlines()) == 15  # default n=15


async def test_run_env_merged_and_cwd(tmp_path):
    run = Run()
    await run.start(
        fake("echoenv", "CLAUDE_CONFIG_DIR"),
        str(tmp_path),
        {"CLAUDE_CONFIG_DIR": "/conta/x"},
    )
    (ev,) = await collect(run)
    value, cwd = ev.text.split("|")
    assert value == "/conta/x"
    assert os.path.realpath(cwd) == os.path.realpath(tmp_path)


async def test_run_huge_line_does_not_break_reader(tmp_path):
    run = Run()
    await run.start(fake("big"), str(tmp_path), {})
    events = await collect(run)
    assert [e.kind for e in events] == ["text", "result"]
    assert len(events[0].text) == 200_000
    assert run.returncode == 0


async def test_stop_sigterm_own_process_group(tmp_path):
    run = Run()
    await run.start(fake("sleep"), str(tmp_path), {})
    assert os.getpgid(run.pid) == run.pid  # grupo próprio (start_new_session)
    assert os.getpgid(run.pid) != os.getpgrp()
    agen = run.events()
    first = await agen.__anext__()
    assert first.kind == "init"
    await run.stop(grace_s=5)
    rest = [e async for e in agen]
    assert rest == []
    assert run.returncode == -15


async def test_stop_escalates_to_sigkill(tmp_path):
    run = Run()
    await run.start(fake("ignoreterm"), str(tmp_path), {})
    agen = run.events()
    await agen.__anext__()  # handler de SIGTERM já instalado (init emitido depois dele)
    await run.stop(grace_s=0.3)
    [e async for e in agen]
    assert run.returncode == -9


async def test_stop_is_noop_without_start_or_after_exit(tmp_path):
    run = Run()
    await run.stop()  # sem start
    assert run.returncode is None
    await run.start(fake("ok"), str(tmp_path), {})
    await collect(run)
    await run.stop()  # já terminou
    assert run.returncode == 0


async def test_timeout_kills_and_flags(tmp_path):
    run = Run(timeout_s=0.5)
    await run.start(fake("sleep"), str(tmp_path), {})
    events = await collect(run)
    assert [e.kind for e in events] == ["init"]
    assert run.timed_out is True
    assert run.returncode in (-15, -9)


async def test_timeout_pausado_nao_mata_e_retoma(tmp_path):
    run = Run(timeout_s=0.6)
    await run.start(fake("sleep"), str(tmp_path), {})
    run.pause_timeout()
    await asyncio.sleep(1.5)  # bem acima do timeout
    assert run.returncode is None and run.timed_out is False
    run.resume_timeout()
    events = await collect(run)  # volta a contar e estoura
    assert [e.kind for e in events] == ["init"]
    assert run.timed_out is True


async def test_pausas_aninhadas(tmp_path):
    run = Run(timeout_s=0.5)
    await run.start(fake("sleep"), str(tmp_path), {})
    run.pause_timeout()
    run.pause_timeout()
    run.resume_timeout()  # ainda pausado (1 pendente)
    await asyncio.sleep(1.2)
    assert run.returncode is None
    run.resume_timeout()
    await collect(run)
    assert run.timed_out is True


async def test_start_missing_binary_raises(tmp_path):
    run = Run()
    with pytest.raises(FileNotFoundError):
        await run.start(["/nao/existe/claude", "-p", "x"], str(tmp_path), {})


# ---- RunManager ----


async def test_manager_busy_and_release(tmp_path):
    mgr = RunManager()
    assert mgr.busy("s1") is False and mgr.get("s1") is None and mgr.running() == []
    run = await mgr.launch("s1", fake("sleep"), str(tmp_path), {})
    assert mgr.busy("s1") is True
    assert mgr.get("s1") is run
    assert mgr.running() == ["s1"]
    with pytest.raises(RuntimeError, match="busy"):
        await mgr.launch("s1", fake("ok"), str(tmp_path), {})
    other = await mgr.launch("s2", fake("sleep"), str(tmp_path), {})
    assert sorted(mgr.running()) == ["s1", "s2"]
    await run.stop(grace_s=2)
    await other.stop(grace_s=2)
    await collect(run)
    await collect(other)
    assert mgr.busy("s1") is False and mgr.running() == []
    again = await mgr.launch("s1", fake("ok"), str(tmp_path), {})
    assert again is not run
    await collect(again)
    assert again.returncode == 0


async def test_manager_launch_failure_leaves_session_free(tmp_path):
    mgr = RunManager()
    with pytest.raises(FileNotFoundError):
        await mgr.launch("s1", ["/nao/existe/claude"], str(tmp_path), {})
    assert mgr.busy("s1") is False
    assert mgr.get("s1") is None


async def test_manager_concurrent_launch_same_session(tmp_path):
    import asyncio

    mgr = RunManager()
    results = await asyncio.gather(
        mgr.launch("s1", fake("sleep"), str(tmp_path), {}),
        mgr.launch("s1", fake("sleep"), str(tmp_path), {}),
        return_exceptions=True,
    )
    errs = [r for r in results if isinstance(r, RuntimeError)]
    runs = [r for r in results if isinstance(r, Run)]
    assert len(errs) == 1 and len(runs) == 1
    await runs[0].stop(grace_s=2)
    await collect(runs[0])


async def test_manager_passes_timeout(tmp_path):
    mgr = RunManager(timeout_s=0.5)
    run = await mgr.launch("s1", fake("sleep"), str(tmp_path), {})
    await collect(run)
    assert run.timed_out is True


# ---- live: smoke com claude real (LIVE=1 LIVE_CLAUDE_CONFIG_DIR=<conta de menor uso>) ----


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("LIVE"), reason="LIVE=1 para rodar")
async def test_live_runner_smoke(tmp_path):
    config_dir = os.environ.get("LIVE_CLAUDE_CONFIG_DIR")
    if not config_dir:
        pytest.skip("defina LIVE_CLAUDE_CONFIG_DIR")
    claude = Path(os.environ.get("LIVE_CLAUDE_BIN", Path.home() / ".local/bin/claude"))
    args = build_args(
        claude, "responda só OK", "6f1c2a3e-1b2c-4d5e-8f90-a1b2c3d4e5f6", new=True
    )
    run = Run(timeout_s=110)
    await run.start(args, str(tmp_path), {"CLAUDE_CONFIG_DIR": config_dir})
    events = await collect(run)
    results = [e for e in events if e.kind == "result"]
    assert results, run.stderr_tail()
    assert "OK" in (results[-1].text or "")
    assert run.returncode == 0


# ---- fix round 1 ----


async def test_child_env_strips_bot_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("OPENAI_API_KEY", "sk")
    monkeypatch.setenv("BOT_LOCAL_SECRET", "sec")
    monkeypatch.setenv("OUTRO_BOT_TOKEN", "t2")
    monkeypatch.setenv("BOT_LOCAL_PORT", "8765")
    for var, expected in [
        ("TELEGRAM_BOT_TOKEN", ""),
        ("OPENAI_API_KEY", ""),
        ("BOT_LOCAL_SECRET", ""),
        ("OUTRO_BOT_TOKEN", ""),
        ("BOT_LOCAL_PORT", "8765"),  # não é segredo; o MCP usa
    ]:
        run = Run()
        await run.start(
            fake("echoenv", var), str(tmp_path), {"CLAUDE_CONFIG_DIR": "/c"}
        )
        (ev,) = await collect(run)
        assert ev.text.split("|")[0] == expected, var


def test_child_env_explicit_extra_wins(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    env = child_env({"CLAUDE_CONFIG_DIR": "/c"})
    assert "TELEGRAM_BOT_TOKEN" not in env and env["CLAUDE_CONFIG_DIR"] == "/c"
    assert os.environ["TELEGRAM_BOT_TOKEN"] == "tok"  # os.environ intacto


async def test_events_early_exit_stops_process(tmp_path):
    run = Run()
    await run.start(fake("sleep"), str(tmp_path), {})
    agen = run.events()
    assert (await agen.__anext__()).kind == "init"
    await agen.aclose()  # consumidor desistiu
    assert run.returncode is not None
    assert not run.active


async def test_events_cancelled_consumer_stops_process(tmp_path):
    run = Run()
    await run.start(fake("sleep"), str(tmp_path), {})

    async def consume():
        async for _ in run.events():
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert run.returncode is not None
    assert not run.active


async def test_events_oversized_line_stops_process(tmp_path, monkeypatch):
    monkeypatch.setattr("bot.runner.STDOUT_LIMIT", 1000)
    run = Run()
    await run.start(fake("bigsleep"), str(tmp_path), {})
    with pytest.raises(ValueError):
        await collect(run)
    assert run.returncode is not None


async def test_stop_sigkills_group_after_leader_exits(tmp_path):
    run = Run()
    await run.start(fake("grandchild"), str(tmp_path), {})
    agen = run.events()
    grandchild = int((await agen.__anext__()).session_id)
    os.kill(grandchild, 0)  # vivo
    await run.stop(grace_s=2)
    await agen.aclose()
    assert run.returncode == -15  # líder saiu com SIGTERM; o neto ignorava
    for _ in range(40):
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("neto sobreviveu ao stop")


async def test_busy_until_events_drained(tmp_path):
    mgr = RunManager()
    run = await mgr.launch("s1", fake("ok"), str(tmp_path), {})
    agen = run.events()
    await agen.__anext__()  # init
    assert mgr.busy("s1")
    async for _ in agen:
        pass
    assert run.returncode == 0
    assert not mgr.busy("s1")
