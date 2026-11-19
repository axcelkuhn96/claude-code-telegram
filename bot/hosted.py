"""Sessões hospedadas: o Claude Code INTERATIVO (TUI) da sessão roda num tmux dedicado, com
Remote Control, e o bot conversa com ele colando texto no terminal e lendo o `.jsonl`.

Assim a MESMA sessão aparece no Telegram e em claude.ai/code. Este módulo é a parte síncrona e
testável: tmux (exec-style, sem shell, com timeout, ambiente sem segredos do bot), leitura do
painel (pronto / ocupado / diálogo) e leitura incremental do `.jsonl` com detecção de fim de turno.

Marcadores medidos em `.jsonl` reais do Claude Code 2.1.328 (ver docs/analises/
2026-11-19-modo-hosted-rc.md):
- fim de turno: linha `system` `stop_hook_summary` (≈1 por prompt, logo após o `assistant` com
  `stop_reason` "end_turn") e/ou `system` `turn_duration` (às vezes ausente);
- erro de API (limite, login): `assistant` com `isApiErrorMessage` e SEM `stop_hook_summary`;
- interrupção (Esc): `user` "[Request interrupted by user…]" — ou nada, se nada saiu ainda;
- prompt humano: `user` com `origin.kind == "human"` (`promptSource` typed/queued); notificações
  de tarefa (`task-notification`), pares e agendados não são humanos;
- link do Remote Control: `system` `bridge_status` com `url`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import socket
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from bot.runner import child_env
from bot.sessions import clean_prompt
from bot.stream import _detail

log = logging.getLogger(__name__)

TMUX_BIN = "/usr/bin/tmux"
SOCKET = "claude-tg"
PREFIX = "cc-"
WIDTH, HEIGHT = 220, 50
CMD_TIMEOUT_S = 15.0
MAX_READ_BYTES = 16 * 1024 * 1024  # por leitura incremental do .jsonl
MAX_LINE_BYTES = 8 * 1024 * 1024
URL_TAIL_BYTES = 512 * 1024
EXCERPT_MAX = 1500
DIALOG_SCAN_LINES = 40
ENV_PROJECT = "CLAUDE_TG_PROJECT"
ENV_ACCOUNT = "CLAUDE_TG_ACCOUNT"

_NAME_RE = re.compile(r"cc-[0-9a-f]{8}")
_SEP_RE = re.compile(r"^\s*[─━═]{20,}\s*$")
_BOX_RE = re.compile(r"^[\s│┃|╭╮╰╯]+|[\s│┃|╭╮╰╯]+$")
_OPTION_RE = re.compile(r"^(?:❯\s*)?(\d{1,2})[.)]\s+(\S.*)$")
_HINT_RE = re.compile(
    r"(?i)enter to (?:select|confirm|submit|continue)|esc to (?:cancel|go back|exit)"
    r"|to navigate"
)
_FREE_RE = re.compile(r"(?i)^(?:type something|other\b|outro|outra|digite)")
_CTRL_RE = re.compile(
    r"[\x00-\x08\x0b-\x1f\x7f]"
)  # tira ESC: nada escapa do bracketed paste
_INTERRUPTED = "[Request interrupted by user"
_URL_RE = re.compile(r"https://claude\.ai/code/[A-Za-z0-9_?=&./-]+")
_PLACEHOLDER_RE = re.compile(r'Try "[^"\n]*"')

Runner = Callable[[list[str], "str | None"], tuple[int, str]]


def run_cmd(
    argv: list[str], input_text: str | None = None, timeout: float = CMD_TIMEOUT_S
) -> tuple[int, str]:
    """(código, saída). Exec-style, sem shell, com timeout; `input_text` vai pelo stdin."""
    try:
        p = subprocess.run(  # noqa: S603
            argv,
            input=input_text,
            stdin=subprocess.DEVNULL if input_text is None else None,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            env=child_env(),
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return 127, type(e).__name__
    return p.returncode, (p.stdout or "") + (p.stderr or "" if p.returncode else "")


def session_name(sid: str) -> str:
    return f"{PREFIX}{sid[:8].lower()}"


def rc_session_name(project: str, hostname: str | None = None) -> str:
    """Nome da sessão no claude.ai: <hostname>-<pasta do projeto>."""
    base = Path(str(project).rstrip("/") or "/").name or "root"
    return f"{hostname or socket.gethostname()}-{base}"


def claude_argv(
    claude_bin: str | Path, sid: str, new: bool, rc_name: str, mcp_config: str | Path
) -> list[str]:
    """TUI com Remote Control. `--remote-control=<nome>` (o valor é opcional na CLI: com `=` não
    há ambiguidade); `--mcp-config` (variádico) por último."""
    return [
        str(claude_bin),
        "--dangerously-skip-permissions",
        f"--remote-control={rc_name}",
        "--session-id" if new else "--resume",
        sid,
        "--mcp-config",
        str(mcp_config),
    ]


def clean_paste(text: str) -> str:
    """Texto que vai para o terminal: sem caracteres de controle (exceto \\n e \\t) e sem `!`/`#`
    no começo (no TUI viram modo bash/memória)."""
    text = _CTRL_RE.sub("", text.replace("\r\n", "\n"))
    return " " + text if text[:1] in ("!", "#") else text


class Tmux:
    """Servidor tmux próprio (`-L claude-tg`). Sessões novas sobem via `systemd-run --user --scope`:
    o servidor tmux fica fora do cgroup do bot (KillMode=control-group) e sobrevive ao reinício."""

    def __init__(
        self,
        runner: Runner | None = None,
        socket_name: str = SOCKET,
        tmux_bin: str = TMUX_BIN,
    ) -> None:
        self._runner = runner or (lambda argv, inp=None: run_cmd(argv, inp))
        self.socket = socket_name
        self.bin = tmux_bin

    def _t(self, *args: str, input_text: str | None = None) -> tuple[int, str]:
        return self._runner([self.bin, "-L", self.socket, *args], input_text)

    def start(
        self, name: str, project: str, env: Mapping[str, str], argv: list[str]
    ) -> tuple[bool, str]:
        if not _NAME_RE.fullmatch(name):
            return False, "nome inválido"
        cmd = [
            "systemd-run",
            "--user",
            "--scope",
            "--collect",
            "--quiet",
            "--",
            self.bin,
            "-L",
            self.socket,
            "new-session",
            "-d",
            "-s",
            name,
            "-x",
            str(WIDTH),
            "-y",
            str(HEIGHT),
            "-c",
            str(project),
        ]
        for k, v in env.items():
            cmd += ["-e", f"{k}={v}"]
        code, out = self._runner([*cmd, *argv], None)
        return code == 0, out.strip()[-200:]

    def exists(self, name: str) -> bool:
        return (
            bool(_NAME_RE.fullmatch(name))
            and self._t("has-session", "-t", name)[0] == 0
        )

    def names(self) -> list[str]:
        code, out = self._t("list-sessions", "-F", "#{session_name}")
        if code != 0:
            return []
        return [
            n for n in (x.strip() for x in out.splitlines()) if _NAME_RE.fullmatch(n)
        ]

    def env(self, name: str) -> dict[str, str]:
        if not _NAME_RE.fullmatch(name):
            return {}
        code, out = self._t("show-environment", "-t", name)
        if code != 0:
            return {}
        env = {}
        for line in out.splitlines():
            k, sep, v = line.partition("=")
            if sep and k and not k.startswith("-"):
                env[k] = v
        return env

    def capture(self, name: str) -> str:
        if not _NAME_RE.fullmatch(name):
            return ""
        code, out = self._t("capture-pane", "-p", "-t", name)
        return out if code == 0 else ""

    def paste(self, name: str, text: str) -> bool:
        """Cola via buffer (stdin) em bracketed paste: multi-linha não vira vários Enter."""
        if not _NAME_RE.fullmatch(name):
            return False
        buf = f"tg-{name}"
        if self._t("load-buffer", "-b", buf, "-", input_text=clean_paste(text))[0] != 0:
            return False
        return self._t("paste-buffer", "-p", "-d", "-b", buf, "-t", name)[0] == 0

    def keys(self, name: str, *keys: str, literal: bool = False) -> bool:
        if not _NAME_RE.fullmatch(name) or not keys:
            return False
        flags = ["-l"] if literal else []
        return self._t("send-keys", "-t", name, *flags, *keys)[0] == 0

    def kill(self, name: str) -> bool:
        """Encerra só sessões `cc-<8 hex>` deste servidor tmux (filhos do próprio bot)."""
        if not _NAME_RE.fullmatch(name):
            return False
        return self._t("kill-session", "-t", name)[0] == 0

    def pane_pid(self, name: str) -> int | None:
        if not _NAME_RE.fullmatch(name):
            return None
        code, out = self._t("list-panes", "-t", name, "-F", "#{pane_pid}")
        first = out.split()[0] if code == 0 and out.split() else ""
        return int(first) if first.isdigit() else None


# ---------- painel (capture-pane) ----------


@dataclass(frozen=True)
class Dialog:
    excerpt: str
    options: tuple[tuple[int, str], ...]
    free: int | None  # opção de texto livre ("Type something.")
    key: (
        str  # identidade do diálogo (sem o cursor ❯): uma mensagem por diálogo distinto
    )


def _unbox(line: str) -> str:
    return _BOX_RE.sub("", line)


def parse_dialog(pane: str) -> Dialog | None:
    """Seletor/diálogo do TUI (AskUserQuestion, permissão, confiança…): uma linha de dica
    ("Enter to select · ↑/↓ to navigate · Esc to cancel") e opções numeradas 1..N acima dela."""
    lines = pane.splitlines()
    hint = next(
        (i for i in range(len(lines) - 1, -1, -1) if _HINT_RE.search(lines[i])), None
    )
    if hint is None:
        return None
    start = max(0, hint - DIALOG_SCAN_LINES)
    block: list[str] = []
    for i in range(hint - 1, start - 1, -1):
        s = _unbox(lines[i])
        if s.startswith(("●", "✻", "⎿")) or (
            s.startswith("❯") and not _OPTION_RE.match(s)
        ):
            break  # saída do assistente / prompt no histórico: o diálogo começa abaixo
        if _SEP_RE.match(s) and any(
            (m := _OPTION_RE.match(x)) and m.group(1) == "1" for x in block
        ):
            break  # régua acima do cabeçalho (a opção 1 já passou)
        block.append(s)
    block.reverse()
    options: list[tuple[int, str]] = []
    for s in block:
        m = _OPTION_RE.match(s)
        if m and int(m.group(1)) == len(options) + 1:
            options.append((int(m.group(1)), m.group(2).strip()))
    if len(options) < 2:
        return None
    body = [s for s in block if s and not _SEP_RE.match(s)]
    body.append(_unbox(lines[hint]))
    excerpt = "\n".join(body)[:EXCERPT_MAX]
    free = next((n for n, label in options if _FREE_RE.match(label)), None)
    key_src = " ".join(re.sub(r"❯", " ", excerpt).split())
    key = hashlib.sha1(key_src.encode()).hexdigest()[:12]  # noqa: S324 - id, não segurança
    return Dialog(excerpt, tuple(options), free, key)


def input_box(pane: str) -> str | None:
    """Texto da caixa de entrada (`❯ …` entre as duas réguas de baixo); None se não há caixa."""
    lines = pane.splitlines()
    seps = [i for i, s in enumerate(lines) if _SEP_RE.match(s)]
    for lo, hi in zip(reversed(seps[:-1]), reversed(seps[1:]), strict=False):
        inner = lines[lo + 1 : hi]
        if (
            inner
            and inner[0].lstrip().startswith("❯")
            and not _OPTION_RE.match(inner[0].strip())
        ):
            first = inner[0].lstrip()[1:].strip()
            rest = [s.strip() for s in inner[1:]]
            if not rest and _PLACEHOLDER_RE.fullmatch(first):
                return ""  # sugestão esmaecida da caixa vazia, não é texto digitado
            return "\n".join([first, *rest]).strip()
    return None


def pane_state(pane: str) -> str:
    """dialog | busy | ready | starting."""
    if parse_dialog(pane) is not None:
        return "dialog"
    tail = [s for s in pane.splitlines() if s.strip()][-6:]
    if any("esc to interrupt" in s.lower() for s in tail):
        return "busy"
    return "ready" if input_box(pane) is not None else "starting"


# ---------- .jsonl ----------


@dataclass
class Ev:
    kind: str  # prompt | tool | text | end | url
    text: str | None = None
    raw: str | None = None  # prompt: texto cru (para casar com o que o bot colou)
    human: bool = False
    tool: str | None = None
    detail: str | None = None
    is_error: bool = False
    duration_ms: int | None = None


def _texts(content: object) -> list[str]:
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [
            b["text"]
            for b in content
            if isinstance(b, dict)
            and b.get("type") == "text"
            and isinstance(b.get("text"), str)
        ]
    return []


def _is_human(d: dict) -> bool:
    origin = d.get("origin")
    kind = origin.get("kind") if isinstance(origin, dict) else None
    if kind is not None:
        return kind == "human"
    if d.get("isMeta"):
        return False
    return d.get("promptSource") in (None, "typed", "queued") and d.get(
        "turnOrigin"
    ) in (None, "human")


@dataclass
class TurnTracker:
    """Linhas do .jsonl -> eventos de turno (prompt, ferramenta, texto, fim)."""

    in_turn: bool = False
    texts: list[str] = field(default_factory=list)  # textos desde a última ferramenta
    last_stop: str | None = None

    def reset(self) -> None:
        self.in_turn, self.texts, self.last_stop = False, [], None

    def force_end(self) -> list[Ev]:
        """Fim inferido (painel pronto e jsonl parado após `end_turn`): fecha com o texto atual."""
        if not self.in_turn:
            return []
        ev = Ev("end", text="\n\n".join(self.texts).strip())
        self.reset()
        return [ev]

    def _end(self, is_error: bool = False, duration_ms: int | None = None) -> list[Ev]:
        ev = Ev(
            "end",
            text="\n\n".join(self.texts).strip(),
            is_error=is_error,
            duration_ms=duration_ms,
        )
        self.reset()
        return [ev]

    def feed(self, d: object) -> list[Ev]:
        if not isinstance(d, dict) or d.get("isSidechain"):
            return []
        kind = d.get("type")
        if kind == "system":
            sub = d.get("subtype")
            if sub == "bridge_status":
                url = d.get("url")
                if isinstance(url, str) and _URL_RE.fullmatch(url):
                    return [Ev("url", text=url)]
                return []
            if sub in ("stop_hook_summary", "turn_duration") and self.in_turn:
                dur = d.get("durationMs")
                return self._end(
                    duration_ms=dur
                    if isinstance(dur, int) and not isinstance(dur, bool)
                    else None
                )
            return []
        msg = d.get("message")
        if not isinstance(msg, dict):
            return []
        content = msg.get("content")
        if kind == "user":
            if isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content
            ):
                return []
            texts = _texts(content)
            if not texts:
                return []
            raw = "\n".join(texts)
            if raw.lstrip().startswith(_INTERRUPTED):
                return self._end() if self.in_turn else []
            out = self._end() if self.in_turn else []  # prompt novo fecha o anterior
            self.in_turn = True
            human = _is_human(d)
            return [
                *out,
                Ev("prompt", text=clean_prompt(raw), raw=raw, human=human),
            ]
        if kind != "assistant":
            return []
        self.in_turn = True
        events: list[Ev] = []
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                name = block.get("name")
                self.texts = []
                events.append(
                    Ev(
                        "tool",
                        tool=name if isinstance(name, str) else None,
                        detail=_detail(block.get("input")),
                    )
                )
            elif block.get("type") == "text" and isinstance(block.get("text"), str):
                self.texts.append(block["text"])
                events.append(Ev("text", text=block["text"]))
        stop = msg.get("stop_reason")
        if isinstance(stop, str):
            self.last_stop = stop
        if d.get(
            "isApiErrorMessage"
        ):  # limite/login: o Claude Code não roda o Stop hook
            events += self._end(is_error=True)
        return events


class JsonlTail:
    """Leitura incremental do .jsonl a partir de um offset (só linhas completas)."""

    def __init__(self, path: Path, offset: int = 0) -> None:
        self.path = path
        self.offset = offset
        self._partial = b""

    def read(self) -> list[dict]:
        try:
            with open(self.path, "rb") as f:
                size = f.seek(0, 2)
                if size < self.offset:  # arquivo trocado/truncado: recomeça do fim
                    self.offset, self._partial = size, b""
                    return []
                f.seek(self.offset)
                chunk = f.read(MAX_READ_BYTES)
        except OSError:
            return []
        self.offset += len(chunk)
        data = self._partial + chunk
        lines = data.split(b"\n")
        self._partial = lines.pop()
        if len(self._partial) > MAX_LINE_BYTES:
            self._partial = b""
        out = []
        for raw in lines:
            if not raw.strip() or len(raw) > MAX_LINE_BYTES:
                continue
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            if isinstance(d, dict):
                out.append(d)
        return out


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def last_rc_url(path: Path) -> str | None:
    """Último link do Remote Control (`bridge_status`) no fim do .jsonl."""
    try:
        with open(path, "rb") as f:
            size = f.seek(0, 2)
            f.seek(max(0, size - URL_TAIL_BYTES))
            tail = f.read()
    except OSError:
        return None
    url = None
    for raw in tail.split(b"\n"):
        if b'"bridge_status"' not in raw:
            continue
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        u = d.get("url") if isinstance(d, dict) else None
        if isinstance(u, str) and _URL_RE.fullmatch(u):
            url = u
    return url


def prompt_matches(sent: str, raw: str) -> bool:
    """A linha `user` do .jsonl é o texto que o bot colou? (colagem longa vira
    `<pasted_content …>`; espaços normalizados; só o começo é comparado)."""
    if "<pasted_content" in raw[:300]:
        return True
    a = " ".join(clean_paste(sent).split())[:40]
    b = " ".join(raw.split())
    return bool(a) and b.startswith(a)


# ---------- objetos vivos (o bot orquestra; aqui só o estado) ----------


class HostedTurn:
    """Um prompt do Telegram na sessão hospedada. Quaca como `runner.Run` para o `run_prompt`:
    `events()` entrega os eventos (stream.Event) que o leitor do .jsonl empurra até o "result"."""

    returncode = None
    timed_out = False
    pid = None

    def __init__(self, text: str, sent_at: float = 0.0) -> None:
        self.text = text
        self.sent_at = sent_at
        self.bound = False  # a linha `user` deste prompt já apareceu no .jsonl
        self._queue: asyncio.Queue = asyncio.Queue()

    def stderr_tail(self, n: int = 15) -> str:
        return ""

    def push(self, ev) -> None:
        self._queue.put_nowait(ev)

    def close(self) -> None:
        self._queue.put_nowait(None)

    async def events(self):
        while True:
            ev = await self._queue.get()
            if ev is None:
                return
            yield ev
            if ev.kind == "result":
                return


class Host:
    """Sessão hospedada viva (uma por session_id). Fica no RunManager: `active` = turno em curso
    (do Telegram ou digitado no claude.ai); `stop()` interrompe (Esc) via `stop_cb` do bot."""

    hosted = True
    pid = None
    returncode = None
    timed_out = False

    def __init__(
        self,
        sid: str,
        project: str,
        account: str,
        config_dir: str | Path,
        tail: JsonlTail,
        rc_name: str = "",
    ) -> None:
        self.sid = sid
        self.name = session_name(sid)
        self.project = project
        self.account = account
        self.config_dir = Path(config_dir)
        self.tail = tail
        self.rc_name = rc_name
        self.rc_url: str | None = None
        self.url_announced = False
        self.tracker = TurnTracker()
        self.alive = True
        self.ready = False  # o TUI já mostrou a caixa de entrada
        self.ours: HostedTurn | None = None
        self.turn_ours = False
        self.dialog: Dialog | None = None
        self.dialog_seq = 0
        self.dialog_mid: int | None = None
        self.dialog_text = ""
        self.dialog_seen: set[str] = set()
        self.free_armed = False
        self.last_growth = 0.0
        self.last_probe = 0.0
        self.last_alive = 0.0
        self.stop_cb = None

    @property
    def active(self) -> bool:
        return self.alive and (self.tracker.in_turn or self.ours is not None)

    def pause_timeout(self) -> None:  # TUI não tem o timeout de 30 min do `claude -p`
        pass

    def resume_timeout(self) -> None:
        pass

    def stderr_tail(self, n: int = 15) -> str:
        return ""

    async def stop(self) -> None:
        if self.stop_cb is not None:
            await self.stop_cb(self)
