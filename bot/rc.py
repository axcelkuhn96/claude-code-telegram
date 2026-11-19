"""Claude Code Remote Control (`claude remote-control`) como unit transitória do systemd --user.

Um servidor RC por (conta, projeto), independente das execuções `claude -p` do bot. Só lê o
`<config_dir>/.claude.json` (confiança do projeto); nunca escreve em ~/.claude*. Subprocessos
sempre em exec-style (sem shell) e com timeout; o ambiente do filho não leva segredos do bot.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import socket
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from bot.runner import child_env

log = logging.getLogger(__name__)

DEFAULT_ACCOUNTS = "personal"
UNIT_PREFIX = "claude-rc-"
CMD_TIMEOUT_S = 15.0
SLUG_MAX = 40
MAX_CONFIG_BYTES = 32 * 1024 * 1024  # .claude.json é grande
JOURNAL_LINES = 200
DESC_PREFIX = "claude-rc:"  # descrição da unit: claude-rc:<conta>:<caminho do projeto>

_STOP_RE = re.compile(r"claude-rc-[a-z0-9-]+(?:\.service)?")
_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_URL_RE = re.compile(r"https://claude\.ai/code[^\s\x00-\x1f\x7f\"'<>\\]*")
_CAPACITY_RE = re.compile(r"Capacity:\s*(\d+/\d+)")

Runner = Callable[..., tuple[int, str]]


@dataclass(frozen=True)
class RcResult:
    status: str  # started | already | untrusted | error
    unit: str
    detail: str = ""


def accounts(env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Contas com Remote Control (RC_ACCOUNTS, lista por vírgula; default personal; vazio = nenhuma)."""
    env = os.environ if env is None else env
    raw = env.get("RC_ACCOUNTS")
    raw = DEFAULT_ACCOUNTS if raw is None else raw
    return tuple(a for a in (p.strip() for p in raw.split(",")) if a)


def _slug(text: str, limit: int) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].strip("-")
    return s or "x"


def unit_name(account: str, project: str) -> str:
    """claude-rc-<conta>-<slug>-<hash6>; o hash (conta + caminho absoluto) separa projetos homônimos."""
    path = str(project).rstrip("/") or "/"
    digest = hashlib.sha1(f"{account}\0{path}".encode()).hexdigest()[:6]  # noqa: S324 - id, não segurança
    base = Path(path).name or "root"
    return f"{UNIT_PREFIX}{_slug(account, 20)}-{_slug(base, SLUG_MAX)}-{digest}"


def is_trusted(config_dir: str | Path, project: str) -> bool:
    """`hasTrustDialogAccepted` do projeto (ou de um diretório pai) em <config_dir>/.claude.json. Só leitura."""
    try:
        with open(Path(config_dir) / ".claude.json", "rb") as f:
            raw = f.read(MAX_CONFIG_BYTES)
        projects = json.loads(raw.decode("utf-8")).get("projects")
    except (OSError, ValueError, UnicodeDecodeError, AttributeError):
        return False
    if not isinstance(projects, dict):
        return False
    path = Path(project)
    for p in (path, *path.parents):
        entry = projects.get(str(p))
        if isinstance(entry, dict) and entry.get("hasTrustDialogAccepted") is True:
            return True
    return False


def run_cmd(argv: list[str], timeout: float = CMD_TIMEOUT_S) -> tuple[int, str]:
    """(código, stdout). Exec-style, sem shell, com timeout; falha de execução vira código ≠ 0."""
    try:
        p = subprocess.run(  # noqa: S603
            argv,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=child_env(),
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return 127, type(e).__name__
    return p.returncode, (p.stdout or "") + (p.stderr or "" if p.returncode else "")


def _is_active(unit: str, runner: Runner) -> bool:
    _, out = runner(["systemctl", "--user", "is-active", unit], CMD_TIMEOUT_S)
    return out.strip() in ("active", "activating", "reloading")


def start(
    account: str,
    config_dir: str | Path,
    project: str,
    claude_bin: str | Path,
    runner: Runner | None = None,
    hostname: str | None = None,
    path_env: str | None = None,
) -> RcResult:
    """Garante o servidor RC de (conta, projeto). Idempotente: unit ativa -> "already"."""
    runner = runner or run_cmd
    unit = unit_name(account, project)
    if _is_active(unit, runner):
        return RcResult("already", unit)
    if not is_trusted(config_dir, project):
        return RcResult("untrusted", unit)
    path = path_env if path_env is not None else os.environ.get("PATH", "/usr/bin:/bin")
    bin_dir = str(Path(claude_bin).parent)
    if bin_dir not in path.split(":"):
        path = f"{bin_dir}:{path}"
    name = f"{hostname or socket.gethostname()}-{Path(project).name or 'root'}"
    argv = [
        "systemd-run",
        "--user",
        f"--unit={unit}",
        f"--description={DESC_PREFIX}{account}:{project}",
        "--collect",
        "-p",
        "Restart=on-failure",
        "-p",
        "RestartSec=10",
        "-p",
        "StartLimitIntervalSec=120",  # projeto que passou a falhar não fica em loop eterno
        "-p",
        "StartLimitBurst=5",
        "-p",
        "StandardInput=null",
        f"--working-directory={project}",
        f"--setenv=CLAUDE_CONFIG_DIR={config_dir}",
        f"--setenv=PATH={path}",
        str(claude_bin),
        "remote-control",
        "--name",
        name,
        "--permission-mode",
        "bypassPermissions",
    ]
    code, out = runner(argv, CMD_TIMEOUT_S)
    if code == 0:
        return RcResult("started", unit)
    if _is_active(unit, runner):  # corrida: outra chamada subiu a mesma unit
        return RcResult("already", unit)
    return RcResult("error", unit, out.strip()[-200:])


def strip_ansi(text: str) -> str:
    return _OSC_RE.sub("", _CSI_RE.sub("", text))


def parse_journal(raw: str) -> dict:
    """URL do ambiente (a mais recente), URLs de sessão (únicas, em ordem) e capacidade "n/32"
    da saída do `claude remote-control`, com escapes ANSI/OSC-8 removidos."""
    text = _CSI_RE.sub("", raw)  # links OSC-8 carregam a URL: extrai antes de tirá-los
    urls = list(dict.fromkeys(_URL_RE.findall(text)))
    env = [u for u in urls if "?environment=" in u]
    sessions = [u for u in urls if "?environment=" not in u]
    caps = _CAPACITY_RE.findall(strip_ansi(raw))
    return {
        "env_url": env[-1] if env else None,
        "session_urls": sessions,
        "capacity": caps[-1] if caps else None,
    }


def _split_description(desc: str) -> tuple[str | None, str | None]:
    if desc.startswith(DESC_PREFIX):
        account, sep, project = desc[len(DESC_PREFIX) :].partition(":")
        if sep and account and project:
            return account, project
    return None, None


def status(runner: Runner | None = None) -> list[dict]:
    """Servidores RC: [{unit, account, project, active, env_url, session_urls, capacity}]."""
    runner = runner or run_cmd
    code, out = runner(
        [
            "systemctl",
            "--user",
            "list-units",
            "claude-rc-*",
            "--all",
            "--no-pager",
            "--output=json",
        ],
        CMD_TIMEOUT_S,
    )
    if code != 0:
        return []
    try:
        rows = json.loads(out)
    except ValueError:
        return []
    items = []
    for row in rows if isinstance(rows, list) else []:
        unit = str(row.get("unit", "")) if isinstance(row, dict) else ""
        if not unit.startswith(UNIT_PREFIX) or not _STOP_RE.fullmatch(unit):
            continue
        account, project = _split_description(str(row.get("description", "")))
        active = row.get("active") == "active"
        info = {"env_url": None, "session_urls": [], "capacity": None}
        if row.get("active") in ("active", "activating"):
            jcode, jout = runner(
                [
                    "journalctl",
                    "--user",
                    "-u",
                    unit,
                    "-n",
                    str(JOURNAL_LINES),
                    "-o",
                    "cat",
                    "--no-pager",
                ],
                CMD_TIMEOUT_S,
            )
            if jcode == 0:
                info = parse_journal(jout)
        items.append(
            {
                "unit": unit,
                "account": account,
                "project": project,
                "active": active,
                **info,
            }
        )
    return items


def stop(unit: str, runner: Runner | None = None) -> bool:
    """`systemctl --user stop`, só para unidades claude-rc-*."""
    if not isinstance(unit, str) or not _STOP_RE.fullmatch(unit):
        return False
    code, _ = (runner or run_cmd)(["systemctl", "--user", "stop", unit], CMD_TIMEOUT_S)
    return code == 0
