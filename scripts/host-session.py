"""Sobe a sessão hospedada (TUI no tmux com Remote Control) de uma sessão existente, do mesmo
jeito que o bot faria na próxima mensagem — o bot em execução a adota no próximo envio ou start.

Uso: .venv/bin/python scripts/host-session.py <session_id> <projeto> <conta>
Recusa se a sessão tem execução do bot em andamento (`claude -p … <id>` vivo ou registro em
state.json) ou se o projeto não é confiado na conta. Não imprime segredos.
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import hosted, rc  # noqa: E402
from bot.sessions import encode_cwd  # noqa: E402
from bot.usage import load_profiles  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
STATE_DIR = Path.home() / ".config" / "claude-telegram"
READY_S = 120.0


def bot_run_active(sid: str) -> bool:
    for cmd in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            argv = cmd.read_bytes().split(b"\0")
        except OSError:
            continue
        if b"-p" in argv and sid.encode() in argv:
            return True
    try:
        st = json.loads((STATE_DIR / "state.json").read_text())
    except (OSError, ValueError):
        return False
    return any(e.get("session_id") == sid for e in st.get("interrupted") or [])


def _hosted_prompt() -> str | None:
    """Mesmo system prompt que o bot passa às sessões hospedadas (deploy/…-hosted.md)."""
    try:
        text = (REPO / "deploy" / "telegram-system-prompt-hosted.md").read_text(
            encoding="utf-8"
        )
    except (OSError, UnicodeDecodeError):
        return None
    return text.strip() or None


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(__doc__)
        return 2
    sid, project, account = argv[1], argv[2].rstrip("/"), argv[3]
    if str(uuid.UUID(sid)) != sid or not Path(project).is_dir():
        print("sessão ou projeto inválido")
        return 2
    prof = next(
        (
            p
            for p in load_profiles(Path.home() / ".horizon" / "config.yaml")
            if p.name == account
        ),
        None,
    )
    if prof is None:
        print(f"conta desconhecida: {account}")
        return 2
    if not rc.is_trusted(prof.config_dir, project):
        print("projeto não confiado na conta")
        return 3
    if bot_run_active(sid):
        print("execução do bot em andamento nesta sessão: tente depois")
        return 4
    tmux = hosted.Tmux()
    name = hosted.session_name(sid)
    path = Path(prof.config_dir) / "projects" / encode_cwd(project) / f"{sid}.jsonl"
    rc_name = hosted.rc_session_name(project)
    if not tmux.exists(name):
        port = (STATE_DIR / "local.port").read_text().strip()
        env = {
            "CLAUDE_CONFIG_DIR": str(prof.config_dir),
            "CLAUDE_SESSION_ID": sid,
            "BOT_LOCAL_PORT": port if port.isdigit() else "",
            "CLAUDE_TELEGRAM_STATE_DIR": str(STATE_DIR),
            hosted.ENV_PROJECT: project,
            hosted.ENV_ACCOUNT: account,
        }
        argv_claude = hosted.claude_argv(
            Path.home() / ".local" / "bin" / "claude",
            sid,
            not path.exists(),
            rc_name,
            REPO / "deploy" / "mcp.json",
            _hosted_prompt(),
        )
        ok, out = tmux.start(name, project, env, argv_claude)
        if not ok:
            print(f"tmux falhou: {out}")
            return 5
    end = time.monotonic() + READY_S
    state = "starting"
    while time.monotonic() < end:
        pane = tmux.capture(name)
        state = hosted.pane_state(pane)
        if state in ("ready", "dialog"):
            break
        time.sleep(0.5)
    print(
        f"tmux={name} estado={state} rc_name={rc_name} rc_url={hosted.last_rc_url(path)}"
    )
    if state == "dialog":
        print(hosted.parse_dialog(tmux.capture(name)).excerpt)
    return 0 if state == "ready" else 6


if __name__ == "__main__":
    sys.exit(main(sys.argv))
