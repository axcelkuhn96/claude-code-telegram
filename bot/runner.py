"""Execução do `claude -p` como subprocesso, com stream de eventos e parada segura."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from collections import deque
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path

from bot.stream import Event, parse_line

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 30 * 60.0
TIMEOUT_TICK_S = 1.0  # granularidade do relógio do timeout (pausável)
STDERR_KEEP = 200
# linhas do stream-json podem ser grandes (resultado inteiro numa linha); o default do asyncio é 64 KiB
STDOUT_LIMIT = 16 * 1024 * 1024
# variáveis do bot que o claude filho não pode herdar (BOT_LOCAL_PORT não é segredo: o MCP a usa)
BOT_SECRET_VARS = frozenset(
    {"TELEGRAM_BOT_TOKEN", "OPENAI_API_KEY", "BOT_LOCAL_SECRET", "TOTP_SECRET"}
)


def _is_bot_secret(name: str) -> bool:
    return name in BOT_SECRET_VARS or name.endswith("_BOT_TOKEN")


def child_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """os.environ sem os segredos do bot + `extra`. O claude roda com
    --dangerously-skip-permissions e executa Bash: não pode herdar token/chaves."""
    env = {k: v for k, v in os.environ.items() if not _is_bot_secret(k)}
    env.update(extra or {})
    return env


MAX_SYSTEM_PROMPT_BYTES = 8 * 1024


def load_system_prompt(path: str | Path) -> str | None:
    """Texto do system prompt do Telegram; None se ausente, vazio, ilegível,
    não-UTF-8 ou maior que MAX_SYSTEM_PROMPT_BYTES."""
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_SYSTEM_PROMPT_BYTES + 1)
        if len(raw) > MAX_SYSTEM_PROMPT_BYTES:
            log.warning(
                "system prompt %s excede %d bytes; ignorado",
                path,
                MAX_SYSTEM_PROMPT_BYTES,
            )
            return None
        return raw.decode("utf-8").strip() or None
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as e:
        log.warning("system prompt %s ilegível: %s", path, e)
        return None


def build_args(
    claude_bin: str | Path,
    prompt: str,
    session_id: str,
    new: bool,
    extra_args: Sequence[str] = (),
    system_prompt: str | None = None,
) -> list[str]:
    """Argv do claude. O prompt vai por último, após `--`: não vira opção e
    `--mcp-config` (variádico) em extra_args não o engole. `system_prompt`
    (se não vazio) vai em `--append-system-prompt`, antes do `--`."""
    return [
        str(claude_bin),
        "-p",
        "--session-id" if new else "--resume",
        session_id,
        "--output-format",
        "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
        *extra_args,
        *(["--append-system-prompt", system_prompt] if system_prompt else []),
        "--",
        prompt,
    ]


class Run:
    """Um subprocesso claude. start() -> events() -> (stop() | timeout)."""

    def __init__(self, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self.timeout_s = timeout_s
        self.timed_out = False
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr: deque[str] = deque(maxlen=STDERR_KEEP)
        self._stderr_task: asyncio.Task[None] | None = None
        self._timeout_task: asyncio.Task[None] | None = None
        self._reading = False  # events() em andamento
        self._pauses = 0

    @property
    def active(self) -> bool:
        """Processo vivo ou events() ainda drenando (a sessão segue ocupada)."""
        return self._reading or (self._proc is None or self._proc.returncode is None)

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode if self._proc else None

    def stderr_tail(self, n: int = 15) -> str:
        return "\n".join(list(self._stderr)[-n:]) if n > 0 else ""

    async def start(
        self, args: Sequence[str], cwd: str | Path, env: Mapping[str, str] | None = None
    ) -> None:
        """`env` é mesclado sobre os.environ (sem os segredos do bot, ver `child_env`).
        Processo em grupo próprio (start_new_session)."""
        self._proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(cwd),
            env=child_env(env),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            limit=STDOUT_LIMIT,
        )
        log.info("claude iniciado pid=%s", self._proc.pid)
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        self._timeout_task = asyncio.create_task(self._watch_timeout())

    async def _drain_stderr(self) -> None:
        if self._proc is None or self._proc.stderr is None:
            return
        async for raw in self._proc.stderr:
            self._stderr.append(raw.decode("utf-8", errors="replace").rstrip("\n"))

    def pause_timeout(self) -> None:
        """Congela o relógio do timeout (ex.: enquanto a sessão espera resposta do dono)."""
        self._pauses += 1

    def resume_timeout(self) -> None:
        self._pauses = max(0, self._pauses - 1)

    async def _watch_timeout(self) -> None:
        loop = asyncio.get_running_loop()
        last = loop.time()
        deadline = last + self.timeout_s
        while True:
            now = loop.time()
            if self._pauses:
                deadline += now - last  # tempo pausado não conta
            last = now
            remaining = deadline - now
            if remaining <= 0:
                break
            await asyncio.sleep(min(remaining, TIMEOUT_TICK_S))
        if self.returncode is None:
            self.timed_out = True
            log.warning(
                "claude pid=%s estourou o timeout de %ss", self.pid, self.timeout_s
            )
            await asyncio.shield(self.stop())  # events() cancela esta task ao terminar

    async def events(self) -> AsyncIterator[Event]:
        """Eventos do stdout até EOF; ao fim, espera o processo sair."""
        proc = self._proc
        if proc is None or proc.stdout is None:
            raise RuntimeError("Run não iniciado")
        self._reading = True
        try:
            async for raw in proc.stdout:
                for ev in parse_line(raw.decode("utf-8", errors="replace")):
                    yield ev
            await proc.wait()
            if self._stderr_task is not None:
                await self._stderr_task
        finally:
            # consumidor saiu cedo (erro, cancelamento, linha > limite): não deixa o claude
            # rodando sem ninguém lendo o stdout nem watchdog
            try:
                if self._timeout_task is not None:
                    self._timeout_task.cancel()
                if proc.returncode is None:
                    await asyncio.shield(self.stop())
            finally:
                self._reading = False

    async def stop(self, grace_s: float = 5.0) -> None:
        """SIGTERM no grupo do filho; após grace_s (ou a saída do líder) SIGKILL no grupo,
        pegando netos que ignoraram o SIGTERM. No-op sem filho vivo."""
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        pgid = proc.pid  # start_new_session -> pgid == pid; só o grupo do próprio filho
        self._signal(pgid, signal.SIGTERM)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), grace_s)
        self._signal(
            pgid, signal.SIGKILL
        )  # ProcessLookupError (grupo já vazio) é ignorado
        await proc.wait()

    @staticmethod
    def _signal(pgid: int, sig: signal.Signals) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pgid, sig)


class RunManager:
    """Uma execução por sessão (sem fila)."""

    def __init__(self, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self.timeout_s = timeout_s
        self._runs: dict[str, Run] = {}

    def busy(self, session_id: str) -> bool:
        run = self._runs.get(session_id)
        return run is not None and run.active

    def get(self, session_id: str) -> Run | None:
        return self._runs.get(session_id)

    def running(self) -> list[str]:
        return [sid for sid in self._runs if self.busy(sid)]

    def attach(self, session_id: str, run) -> None:
        """Registra um objeto que quaca como `Run` (ex.: sessão hospedada no tmux): `busy`,
        `running`, `/parar` e o reinício seguro passam a enxergá-lo."""
        self._runs[session_id] = run

    def detach(self, session_id: str, run) -> None:
        if self._runs.get(session_id) is run:
            del self._runs[session_id]

    async def launch(
        self,
        session_id: str,
        args: Sequence[str],
        cwd: str | Path,
        env: Mapping[str, str] | None = None,
    ) -> Run:
        if self.busy(session_id):
            raise RuntimeError("busy")
        previous = self._runs.get(session_id)
        run = Run(self.timeout_s)
        self._runs[session_id] = (
            run  # reserva antes do await: lançamentos concorrentes veem busy
        )
        try:
            await run.start(args, cwd, env)
        except BaseException:
            if previous is None:
                del self._runs[session_id]
            else:
                self._runs[session_id] = previous
            raise
        return run
