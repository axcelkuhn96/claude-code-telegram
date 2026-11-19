"""Handlers do bot Telegram: auth, contas, projetos, sessões, execução do claude, voz, perguntas do MCP."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import html
import inspect
import logging
import os
import re
import signal
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    MenuButtonWebApp,
    ReplyKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from bot import files as files_mod
from bot import hosted as hosted_mod
from bot import pinned as pinned_mod
from bot import quiet as quiet_mod
from bot import rc as rc_mod
from bot.pinned import PinnedStatus
from bot import slots as slots_mod
from bot import sessions as sessions_mod  # histórico (C1) é opcional até o W2 landar
from bot import state as state_mod
from bot import totp
from bot.auth import contact_ok, is_authorized
from bot.config import Config
from bot.localapi import Asks
from bot.power import power_status, read_power_raw
from bot.runfeed import FeedStore
from bot.runner import RunManager, build_args
from bot.stream import Event
from bot.sessions import (
    SessionInfo,
    copy_session_to,
    encode_cwd,
    list_recent,
    load_projects,
)
from bot.state import State
from bot.textsplit import split
from bot.tgformat import md_to_tg_html, split_html
from bot.usage import (
    AccountUsage,
    Profile,
    button_label,
    format_usage,
    get_all_usage,
    load_profiles,
    pick_best,
    sort_usage,
)
from bot.voice import VoiceError, transcribe

log = logging.getLogger(__name__)

PROGRESS_INTERVAL_S = 5.0
TIMEOUT_MIN = 30
MAX_AUDIO_BYTES = 20 * 1024 * 1024  # limite de download da Bot API
MAX_ASK_TEXT = 4096
MAX_UPDATE_AGE_S = 600
STALE_NOTICE_DELAY_S = 3.0
ERROR_NOTICE_EVERY_S = 60.0
STDERR_CHARS = 1500
REPO = Path(__file__).resolve().parent.parent
MCP_CONFIG = REPO / "deploy" / "mcp.json"
SYSTEM_PROMPT_FILE = REPO / "deploy" / "telegram-system-prompt.md"
HOSTED_PROMPT_FILE = REPO / "deploy" / "telegram-system-prompt-hosted.md"


def load_system_prompt(path: Path = SYSTEM_PROMPT_FILE) -> str | None:
    """Texto do --append-system-prompt das sessões do bot (lido uma vez no start)."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        log.warning("system prompt do Telegram ausente: %s", path.name)
        return None
    return text or None


HELP = (
    "Comandos:\n"
    "/contas — uso 5h/7d e conta ativa\n"
    "/conta <nome> — troca de conta (mantém a sessão)\n"
    "/projetos — escolhe o projeto\n"
    "/sessoes — lista e alterna sessões\n"
    "/nova — nova sessão\n"
    "/encerrar — encerra a sessão ativa (confirma antes)\n"
    "/parar — interrompe a execução da sessão ativa\n"
    "/historico [n] — últimas mensagens da sessão\n"
    "/status — estado do bot e da máquina\n"
    "/silencio [off|final|total] — modo silêncio: final = só resultados finais; "
    "total = só o que precisa de você (o resto vai pro /resumo)\n"
    "/resumo — entrega e limpa o que ficou pendente no modo total\n"
    "/menu — mostra o teclado fixo\n"
    "/rc — 🛰 RC: sessões hospedadas (personal, também no claude.ai) e servidores; "
    "/rc server liga um servidor avulso; /rc parar encerra os servidores\n"
    '/fixar [n] · /desafixar n — atalhos 1-4 ("2: texto" fala com o atalho 2)\n'
    "/destravar <código> · /trancar — trava TOTP (se configurada)\n"
    "Texto, áudio ou arquivo vira prompt da sessão ativa."
)
COMMANDS = [
    ("contas", "uso por conta"),
    ("conta", "trocar de conta"),
    ("projetos", "escolher projeto"),
    ("sessoes", "listar sessões"),
    ("nova", "nova sessão"),
    ("historico", "últimas mensagens da sessão"),
    ("parar", "interromper execução"),
    ("status", "estado do bot"),
    ("menu", "teclado fixo"),
    ("silencio", "modo silêncio (off/final/total)"),
    ("resumo", "resumo do que ficou pendente"),
    ("rc", "Remote Control (personal)"),
    ("encerrar", "encerrar a sessão ativa"),
    ("fixar", "fixar a sessão ativa num atalho"),
    ("desafixar", "remover um atalho"),
    ("destravar", "liberar com código TOTP"),
    ("trancar", "trancar agora"),
]
DEFAULT_UNLOCK_HOURS = 12.0
MAX_TOTP_FAILS = 5
HARD_LOCK_S = 15 * 60.0
# teclado fixo: o texto do botão chega como mensagem e vira o comando (não vira prompt)
KEYBOARD_ROWS = [
    ["📊 Contas", "🗂 Sessões", "🆕 Nova"],
    ["📁 Projetos", "📜 Histórico", "⏹ Parar", "ℹ️ Status"],
    ["📌 Fixar", "🔒 Trancar"],
]
KEYBOARD_ACTIONS = {
    "📊 Contas": "cmd_contas",
    "🗂 Sessões": "cmd_sessoes",
    "🆕 Nova": "cmd_nova",
    "📁 Projetos": "cmd_projetos",
    "📜 Histórico": "cmd_historico",
    "⏹ Parar": "cmd_parar",
    "ℹ️ Status": "cmd_status",
    "🔒 Trancar": "cmd_trancar",
    "📌 Fixar": "cmd_fixar",
}
RESUMO_CARD_CHARS = (
    1500  # corpo de cada cartão do /resumo (o texto inteiro está no painel)
)
KB_REFRESH_MIN_S = 20.0
STATUS_EVERY_S = 300.0  # barra fixada: renova uso/⏳/trava a cada 5 min
ASK_ECHO_CHARS = 60
_OTHER_RE = re.compile(
    r"\s*(?:outro|outra|other)\b", re.I
)  # opção "Outro…" = texto livre
DRAIN_MESSAGE = "⏳ reiniciando em instantes — mande de novo em 1 min"
DRAIN_CAP_S = 30 * 60.0  # espera máxima por execuções antes de reiniciar mesmo assim
DRAIN_POLL_S = 1.0
RC_URL_WAIT_S = 20.0  # espera o link do ambiente aparecer no journal da unit
RC_URL_POLL_S = 1.0
# sessões hospedadas (TUI no tmux, ver bot/hosted.py)
HOST_POLL_S = 0.5  # leitura do .jsonl
HOST_IDLE_S = 6.0  # .jsonl parado há isso com turno aberto: olha o painel (diálogo?)
HOST_PROBE_EVERY_S = 2.0
HOST_END_IDLE_S = 10.0  # painel pronto + .jsonl parado há isso: fim de turno inferido
HOST_ALIVE_EVERY_S = 5.0
HOST_READY_S = 60.0  # TUI subindo até mostrar a caixa de entrada
HOST_DIALOG_WAIT_S = 600.0  # com diálogo na subida (confiança, retomada), espera o dono
HOST_READY_POLL_S = 0.5
HOST_BIND_TIMEOUT_S = 45.0  # o prompt colado tem que aparecer no .jsonl
HOST_KEY_SETTLE_S = 0.8
HOST_TYPE_PAUSE_S = 0.3
HOST_INTERRUPT_SETTLE_S = 1.5
HOST_PROMPT_CHARS = 1000
VIA_RC = "🛰 (via claude.ai)"
INTERRUPT_CODES = (143, -15)  # SIGTERM (shell / subprocess)
INTERRUPTED_KEEP_S = 24 * 3600.0
MAX_RESEND_PROMPT = 20000
MAX_INTERRUPTED_KEEP = 20
LONG_REPLY_CHARS = 12000  # acima disso o texto completo também vai como .md
MAX_OPEN_BUTTONS = 10  # 1️⃣…🔟
CARD_PROMPT_CHARS = 45
WORKING_WINDOW_S = 20.0  # sessão do PC "trabalhando": .jsonl mexeu há ≤ 20 s
TERMINATE_WAIT_S = 5.0
HISTORY_DEFAULT_N = 6
HISTORY_MAX_N = 30
LONG_RUN_NOTICE_S = 120.0
RETRY_KEEP = 20
EXCHANGE_PROMPT_CHARS = 300
EXCHANGE_REPLY_CHARS = 800
INBOX_PROMPT_DEFAULT = "Arquivo enviado pelo dono"
ROLE_ICON = {"user": "👤", "assistant": "🤖", "tool": "🔧"}
ROLE_CHARS = {"user": 1000, "tool": 100}  # a resposta do assistente vai inteira


# ---------- funções puras ----------


def is_uuid(s: str) -> bool:
    try:
        return str(uuid.UUID(s)) == s.lower()
    except (ValueError, AttributeError, TypeError):
        return False


def short(sid: str) -> str:
    return sid[:8]


def _base(cwd: str) -> str:
    return Path(cwd.rstrip("/") or "/").name or "/"


def prefix(cwd: str, sid: str) -> str:
    return f"[{_base(cwd)} · {short(sid)}]"


def origin_prefix(origem: dict) -> str:
    """Prefixo das mensagens vindas do MCP; `session_id` nulo -> só o projeto."""
    cwd, sid = origem.get("cwd"), origem.get("session_id")
    if not isinstance(cwd, str) or not cwd.strip():
        return "[?]"
    name = _base(cwd)[:40]
    if isinstance(sid, str) and sid:
        return f"[{name} · {short(sid)}]"
    return f"[{name}]"


def age(delta: timedelta) -> str:
    s = delta.total_seconds()
    if s < 60:
        return "agora"
    if s < 3600:
        return f"{int(s // 60)}min"
    if s < 86400:
        return f"{int(s // 3600)}h"
    return f"{int(s // 86400)}d"


KEYCAPS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]


def age_since(delta: timedelta) -> str:
    """agora · há 5 min · há 3 h · há 2 d (para os cartões de sessão)."""
    s = delta.total_seconds()
    if s < 60:
        return "agora"
    if s < 3600:
        return f"há {int(s // 60)} min"
    if s < 86400:
        return f"há {int(s // 3600)} h"
    return f"há {int(s // 86400)} d"


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# mensagens reais do Claude Code ao esgotar a conta: "5-hour limit reached ∙ resets 3pm",
# "Claude usage limit reached", "You've hit your limit", "rate_limit_error", "Credit balance is
# too low", "quota exceeded"...
_LIMIT_RE = re.compile(
    r"usage[ _-]?limit|rate[ _-]?limit|limit[ _-]?(?:reached|hit|exceeded)|usage\W+(?:\w+\W+)?exceed"
    r"|hit your (?:\w+ )?limit|\bquota\b|credit balance|\bresets? (?:at|in|on)? ?\d"
    r"|out of (?:extra )?usage",
    re.I,
)
# erros que parecem limite mas não são da conta
_NOT_LIMIT_RE = re.compile(
    r"max(?:imum)?[ _-]?(?:number of )?turns|context (?:window|limit|length)", re.I
)


def is_limit_error(text: str | None, is_error: bool) -> bool:
    return bool(
        is_error and text and _LIMIT_RE.search(text) and not _NOT_LIMIT_RE.search(text)
    )


def fmt_duration(ms: float) -> str:
    """4.5s · 42s · 4m12s · 1h02m."""
    s = max(0.0, ms / 1000)
    if s < 10:
        return f"{s:.1f}s"
    secs = int(s + 0.5)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m{secs % 60:02d}s"
    return f"{secs // 3600}h{secs % 3600 // 60:02d}m"


def fmt_cost(usd: float) -> str:
    """US$ 0,31 (4 casas quando < 1 centavo)."""
    digits = 4 if 0 < usd < 0.01 else 2
    return f"US$ {usd:.{digits}f}".replace(".", ",")


def html_to_plain(part: str) -> str:
    """HTML do Telegram -> texto puro (fallback quando o Telegram recusa as entidades)."""
    return html.unescape(re.sub(r"<[^>]+>", "", part))


def format_turns(turns: list) -> str:
    """Turnos em HTML do Telegram, em ordem cronológica: 👤 pergunta (até 1000 chars) ·
    🤖 resposta COMPLETA (Markdown convertido) · 🔧 ferramenta em uma linha."""
    out = []
    for t in turns:
        role = getattr(t, "role", "")
        text = getattr(t, "text", "") or ""
        icon = ROLE_ICON.get(role, "•")
        if role == "assistant":
            out.append(f"{icon} {md_to_tg_html(text)}")
        elif role == "tool":
            tool = getattr(t, "tool", None)
            head = f"{tool}: " if tool else ""
            line = _clip(text, ROLE_CHARS["tool"])
            out.append(f"{icon} <i>{html.escape(head + line, quote=False)}</i>")
        else:
            out.append(
                f"{icon} {html.escape(_clip_keep_lines(text, ROLE_CHARS['user']), quote=False)}"
            )
    return "\n\n".join(out)


def _clip_md(text: str, limit: int) -> str:
    """Corta o Markdown preservando quebras de linha (o `_clip` achata espaços)."""
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _clip_keep_lines(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def format_final(
    text: str | None,
    is_error: bool,
    cost: float | None,
    duration_ms: int | None,
    pfx: str,
    stderr: str,
    returncode: int | None,
    timed_out: bool,
    stopped: bool,
) -> str:
    if timed_out:
        body = f"⏱ Passou de {TIMEOUT_MIN} min; execução encerrada."
    elif stopped:
        body = "⏹ Execução interrompida."
    elif text is not None:
        body = ("❌ " if is_error else "") + (text or "(sem texto)")
    elif returncode in INTERRUPT_CODES:
        body = "⏸ interrompido (o processo recebeu SIGTERM)."
    elif returncode not in (0, None):
        body = f"❌ claude saiu com código {returncode} sem resultado."
    else:
        body = "❌ erro sem detalhes." if is_error else "(sem resposta)"
    if not (timed_out or stopped) and (is_error or text is None):
        if returncode not in (0, None) and stderr.strip():
            body += f"\n\nstderr:\n{stderr.strip()[-STDERR_CHARS:]}"
    foot = []
    if duration_ms is not None:
        foot.append(fmt_duration(duration_ms))
    if cost is not None:
        foot.append(fmt_cost(cost))
    if foot and not (timed_out or stopped):
        body += "\n\n⏱ " + " · ".join(foot)
    return f"{pfx} {body}" if pfx else body


def session_exists(sid: str, cwd: str, profiles: list[Profile]) -> Path | None:
    """Config dir da primeira conta que tem o `.jsonl` da sessão, senão None."""
    if not is_uuid(sid):
        return None
    best: tuple[float, Path] | None = None
    for p in profiles:
        f = Path(p.config_dir) / "projects" / encode_cwd(cwd) / f"{sid}.jsonl"
        try:
            mtime = f.resolve().stat().st_mtime
        except OSError:
            continue
        if (
            best is None or mtime > best[0]
        ):  # empate (symlink compartilhado): a 1ª conta
            best = (mtime, Path(p.config_dir))
    return best[1] if best else None


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _seconds(x: object) -> float:
    return x.total_seconds() if isinstance(x, timedelta) else float(x)  # type: ignore[arg-type]


SEND_ATTEMPTS = 3
SEND_BACKOFF_S = (1.0, 3.0)  # espera antes da 2ª e da 3ª tentativa
MAX_RETRY_AFTER_S = 30.0
_TOKEN_RE = re.compile(r"(?:bot)?\d{6,}:[A-Za-z0-9_-]{20,}")


def _safe_err(e: BaseException) -> str:
    """`Tipo: mensagem` curto e sem token (a URL do Telegram leva `bot<token>`)."""
    return f"{type(e).__name__}: {_TOKEN_RE.sub('***', str(e))[:200]}"


async def tg_call(fn, *args, honor_retry_after: bool = True, **kwargs):
    """Chamada ao Telegram com até SEND_ATTEMPTS tentativas em falha de rede transitória
    (NetworkError/TimedOut, backoff 1 s e 3 s) e RetryAfter respeitado. BadRequest, Forbidden,
    InvalidToken etc. propagam na hora (repetir não adianta). O erro final propaga ao chamador."""
    for attempt in range(SEND_ATTEMPTS):
        last = attempt == SEND_ATTEMPTS - 1
        try:
            return await fn(*args, **kwargs)
        except RetryAfter as e:
            if last or not honor_retry_after:
                raise
            wait = min(_seconds(e.retry_after), MAX_RETRY_AFTER_S)
        except BadRequest:  # é subclasse de NetworkError, mas não é transitório
            raise
        except NetworkError as e:
            if last:
                raise
            wait = SEND_BACKOFF_S[min(attempt, len(SEND_BACKOFF_S) - 1)]
            log.warning(
                "telegram: %s (tentativa %d/%d, repete em %.0fs)",
                _safe_err(e),
                attempt + 1,
                SEND_ATTEMPTS,
                wait,
            )
        await asyncio.sleep(wait)


class Progress:
    """Mensagem "⏳ trabalhando…" editada no máx. a cada PROGRESS_INTERVAL_S."""

    def __init__(
        self,
        tg,
        chat_id: int,
        clock: Callable[[], float] = time.monotonic,
        sleep=asyncio.sleep,
    ):
        self.tg, self.chat_id, self.clock, self.sleep = tg, chat_id, clock, sleep
        self.message_id: int | None = None
        self.shown = ""
        self.pending: str | None = None
        self.last_edit = 0.0
        self.retry_until = 0.0
        self._timer: asyncio.Task | None = None

    async def start(self, text: str) -> None:
        try:
            msg = await tg_call(self.tg.send_message, self.chat_id, text)
        except TelegramError as e:
            log.warning("progresso: envio falhou (%s)", _safe_err(e))
            return
        self.message_id, self.shown, self.last_edit = msg.message_id, text, self.clock()

    async def update(self, text: str) -> None:
        self.pending = text
        await self._flush()

    def _schedule(self, delay: float) -> None:
        """Garante um flush do texto pendente quando a janela abrir (o último tool_use não fica
        preso se nenhum evento novo chegar)."""
        if self._timer is None or self._timer.done():
            self._timer = asyncio.create_task(self._timer_run(delay))

    async def _timer_run(self, delay: float) -> None:
        await self.sleep(delay)
        self._timer = None  # permite reagendar se a janela ainda não abriu
        await self._flush()

    async def _flush(self) -> None:
        text = self.pending
        if self.message_id is None or text is None or text == self.shown:
            return
        now = self.clock()
        wait = max(self.last_edit + PROGRESS_INTERVAL_S - now, self.retry_until - now)
        if wait > 0:
            self._schedule(wait)
            return
        try:
            await tg_call(
                self.tg.edit_message_text,
                text,
                chat_id=self.chat_id,
                message_id=self.message_id,
                honor_retry_after=False,  # o Progress agenda a próxima tentativa sozinho
            )
        except RetryAfter as e:
            self.retry_until = now + _seconds(e.retry_after)
            self._schedule(_seconds(e.retry_after))
            return
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                log.warning("progresso: edição recusada")
        except TelegramError as e:
            log.warning("progresso: edição falhou (%s)", _safe_err(e))
        self.shown, self.last_edit = text, now

    async def finish(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        if self.message_id is None:
            return
        with contextlib.suppress(TelegramError):
            await self.tg.delete_message(self.chat_id, self.message_id)


def owner_only(fn):
    """Só o dono, em chat privado, depois do vínculo; qualquer outro é ignorado sem resposta."""

    @functools.wraps(fn)
    async def wrapper(self, update, ctx):
        if not self.authorized(update):
            return
        self._lazy_menu_button()
        if self.is_locked():  # TOTP: trancado, só /destravar passa
            await self.locked_reply(update)
            return
        await fn(self, update, ctx)

    return wrapper


# ---------- bot ----------


class Bot:
    def __init__(
        self,
        cfg: Config,
        st: State,
        runs: RunManager | None = None,
        client: httpx.AsyncClient | None = None,
        totp_secret: str | None = None,
        unlock_hours: float = DEFAULT_UNLOCK_HOURS,
    ) -> None:
        self.cfg = cfg
        self.state = st
        self.proc_root = Path("/proc")  # injetável nos testes
        self.draining = False  # reinício seguro pedido: não aceita execuções novas
        self._shutting_down = False
        self._drain_task: asyncio.Task | None = None
        self.exit_fn: Callable[[], None] = lambda: os.kill(os.getpid(), signal.SIGTERM)
        self.pinned_enabled = False  # a barra fixada só liga no main (post_init)
        self._pinned: PinnedStatus | None = None
        self._status_snap: dict = {}
        self._status_task: asyncio.Task | None = None
        self._status_dirty = False
        self._kb_sent: tuple[str, ...] = tuple(slots_mod.keyboard_row(st.slots, set()))
        self._kb_last = -KB_REFRESH_MIN_S
        self._kb_task: asyncio.Task | None = None
        self.system_prompt = load_system_prompt()
        self.hosted_prompt = load_system_prompt(HOSTED_PROMPT_FILE)
        self.totp_secret = totp_secret or None  # sem segredo = sem trava (como antes)
        self.unlock_hours = unlock_hours
        self._totp_fails = 0
        self._hard_lock_until = 0.0
        self._menu_set = False
        self._menu_lazy_tried = False
        self.runs = runs or RunManager()
        self.client = client
        self.tg = None  # telegram.Bot, definido no post_init
        self.asks = Asks()
        self.started = time.monotonic()
        self.bot_sessions: dict[str, SessionInfo] = {}
        self.session_accounts: dict[str, str] = {}
        self._tasks: set[asyncio.Task] = set()
        self._stopped: set[str] = set()
        self._rc_inflight: set[tuple[str, str]] = set()
        self._rc_warned: set[tuple[str, str]] = set()
        self.tmux = hosted_mod.Tmux()  # injetável nos testes
        self.hosts: dict[str, hosted_mod.Host] = {}  # sid -> sessão hospedada viva
        self._host_locks: dict[str, asyncio.Lock] = {}
        self.feeds = FeedStore()
        # token -> (sid, projeto, prompt) do último pedido que bateu no limite da conta
        self._retry: OrderedDict[str, tuple[str, str, str]] = OrderedDict()
        self._closed_asks: deque[int] = deque(
            maxlen=200
        )  # message_id de asks encerrados
        self._stale = 0
        self.clock: Callable[[], float] = time.monotonic
        self._last_error_notice = -ERROR_NOTICE_EVERY_S
        self.now: Callable[[], datetime] = lambda: datetime.now(UTC)

    # -- infra --

    @property
    def horizon(self) -> Path:
        return self.cfg.home / ".horizon"

    def profiles(self) -> list[Profile]:
        return load_profiles(self.horizon / "config.yaml", home=self.cfg.home)

    async def usage(self) -> list[AccountUsage]:
        return await get_all_usage(
            self.profiles(),
            self.horizon / "claude-usage-cache.json",
            self.client,
            datetime.now().astimezone(),
            home=self.cfg.home,
            state_dir=self.cfg.state_dir,  # guarda o último uso conhecido (conta "ociosa")
        )

    def save(self) -> None:
        state_mod.save(self.cfg.state_dir / "state.json", self.state)
        self.request_status_refresh()  # projeto/sessão/conta/trava mudaram: atualiza a barra

    # -- barra de status fixada (bot/pinned.py) --

    def _pinned_obj(self) -> PinnedStatus | None:
        if (
            self._pinned is None
            and self.tg is not None
            and self.state.owner_id is not None
        ):
            self._pinned = PinnedStatus(
                self.tg, self.state.owner_id, self.cfg.state_dir / "pinned.json"
            )
        return self._pinned

    async def _status_snapshot(self) -> dict:
        st = self.state
        title = ""
        if st.session_id and st.project:
            title = await asyncio.to_thread(self._title_sync, st.session_id, st.project)
        usage_item = None
        if st.account:
            try:
                usage_item = next(
                    (i for i in await self.usage() if i.name == st.account), None
                )
            except Exception as e:  # noqa: BLE001 - a barra funciona sem o uso
                log.warning("barra: uso indisponível (%s)", type(e).__name__)
        working = len(await asyncio.to_thread(self._working_sync))
        self._status_snap = {
            "title": title,
            "usage": usage_item,
            "working": working,
        }
        return self._status_snap

    def _quiet_label(self) -> str:
        """`final`, `total` ou `total·3` (3 itens na fila); vazio com o silêncio desligado."""
        mode = self.quiet_mode
        if mode == "off":
            return ""
        n = len(self.state.digest)
        return f"{mode}·{n}" if mode == "total" and n else mode

    async def refresh_status(self) -> None:
        """Edita a mensagem fixada (📍 projeto · título · conta (% livre) ⏳ n; 🔒 se trancado)."""
        pinned = self._pinned_obj() if self.pinned_enabled else None
        if pinned is None:
            return
        snap = await self._status_snapshot()
        st = self.state
        await pinned.update(
            pinned_mod.status_line(
                self._label(st.project),
                snap["title"],
                st.account,
                snap["usage"],
                snap["working"],
                self.is_locked(),
                self._quiet_label(),
            )
        )

    def request_status_refresh(self) -> None:
        """Pede a atualização da barra sem esperar (coalesce vários pedidos em uma tarefa)."""
        if not self.pinned_enabled or self.tg is None or self.state.owner_id is None:
            return
        if self._status_task is not None and not self._status_task.done():
            self._status_dirty = True
            return
        try:
            self._status_task = asyncio.create_task(self._status_job())
        except RuntimeError:  # sem loop (chamada fora do asyncio)
            return
        self._tasks.add(self._status_task)
        self._status_task.add_done_callback(self._tasks.discard)

    async def _status_job(self) -> None:
        while True:
            self._status_dirty = False
            await self.refresh_status()
            if not self._status_dirty:
                return

    async def status_loop(self) -> None:
        """Atualiza a barra a cada STATUS_EVERY_S (uso fresco; também apanha a expiração da trava)."""
        await self.refresh_status()
        while True:
            await asyncio.sleep(STATUS_EVERY_S)
            await self.refresh_status()

    def authorized(self, update: Update) -> bool:
        u, c = update.effective_user, update.effective_chat
        return is_authorized(self.state, u.id if u else None, c.type if c else "")

    @property
    def quiet_mode(self) -> str:
        return quiet_mod.valid_mode(self.state.quiet)

    def _quiet_extra(self, silent: bool) -> dict:
        """`disable_notification` só no modo silêncio e só para mensagens não urgentes."""
        return (
            {"disable_notification": True}
            if silent and self.quiet_mode != "off"
            else {}
        )

    async def send(
        self, chat_id: int, text: str, markup=None, parse_mode=None, silent=False
    ):
        extra = {"parse_mode": parse_mode} if parse_mode else {}
        extra.update(self._quiet_extra(silent))
        try:
            return await tg_call(
                self.tg.send_message, chat_id, text, reply_markup=markup, **extra
            )
        except TelegramError as e:
            log.warning("envio falhou (%s)", _safe_err(e))
            return None

    async def send_long(self, chat_id: int, text: str, silent: bool = False) -> bool:
        ok = True
        for part in split(text) or [text]:
            ok = (await self.send(chat_id, part, silent=silent)) is not None and ok
        return ok

    async def send_html(
        self, chat_id: int, html_text: str, markup=None, silent: bool = False
    ) -> bool:
        """HTML já convertido, partido em ≤ 4096 sem quebrar tags; o markup vai na última parte.
        Parte que o Telegram recusar ("can't parse entities") é reenviada como texto puro."""
        parts = split_html(html_text)
        ok = True
        for i, part in enumerate(parts):
            mk = markup if i == len(parts) - 1 else None
            ok = await self._send_html_part(chat_id, part, mk, silent) and ok
        return ok

    async def _send_html_part(
        self, chat_id: int, part: str, markup, silent: bool = False
    ) -> bool:
        try:
            await tg_call(
                self.tg.send_message,
                chat_id,
                part,
                reply_markup=markup,
                parse_mode="HTML",
                **self._quiet_extra(silent),
            )
            return True
        except BadRequest as e:
            if "parse entities" not in str(e).lower():
                log.warning("envio HTML recusado (%s)", _safe_err(e))
                return False
            log.warning("HTML recusado pelo Telegram; reenviando como texto")
            return (
                await self.send(chat_id, html_to_plain(part), markup, silent=silent)
            ) is not None
        except TelegramError as e:
            log.warning("envio HTML falhou (%s)", _safe_err(e))
            return False

    async def send_markdown(
        self, chat_id: int, md: str, markup=None, silent: bool = False
    ) -> bool:
        """Markdown do Claude -> HTML do Telegram -> envio (split e fallback incluídos)."""
        return await self.send_html(chat_id, md_to_tg_html(md), markup, silent)

    async def send_reply_file(
        self, chat_id: int, text: str, sid: str, label: str
    ) -> None:
        """Resposta muito longa: além das partes no chat, o texto completo vai como .md."""
        name = f"resposta-{short(sid)}-{self.now().astimezone().strftime('%H%M')}.md"
        try:
            await tg_call(
                self.tg.send_document,
                chat_id,
                document=text.encode("utf-8"),
                filename=name,
                caption=f"📎 resposta completa {label}"[: files_mod.CAPTION_MAX],
            )
        except TelegramError as e:
            log.warning("envio do .md falhou (%s)", _safe_err(e))

    async def _edit(self, chat_id: int, message_id: int, text: str) -> None:
        with contextlib.suppress(TelegramError):
            await tg_call(
                self.tg.edit_message_text,
                text[:MAX_ASK_TEXT],
                chat_id=chat_id,
                message_id=message_id,
            )

    async def _edit_ask(self, chat_id: int, mid: int, text: str, suffix: str) -> None:
        """Marca o desfecho no fim da pergunta, cortando a pergunta (não o desfecho)."""
        tail = f"\n\n{suffix}"
        await self._edit(chat_id, mid, text[: MAX_ASK_TEXT - len(tail)] + tail)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -- reinício seguro e execuções interrompidas --

    async def restart_when_idle(self) -> dict:
        """POST /restart-when-idle: para de aceitar execuções, espera as em andamento (até 30 min)
        e sai com código 0 — o systemd (Restart=always) sobe o bot de novo."""
        n = len(self.runs.running())
        if not self.draining:
            self.draining = True
            log.info("reinício seguro pedido (%d execução(ões) em andamento)", n)
            self._drain_task = asyncio.create_task(self._drain_then_exit())
        return {"ok": True, "draining": True, "running": n}

    async def _drain_then_exit(self) -> None:
        waited = 0.0
        while self.runs.running() and waited < DRAIN_CAP_S:
            await asyncio.sleep(DRAIN_POLL_S)
            waited += DRAIN_POLL_S
        if self.runs.running():
            log.warning(
                "reinício seguro: limite de espera; saindo com execuções ativas"
            )
        await asyncio.sleep(0.5)  # a resposta HTTP e as mensagens finais saem antes
        log.info("reinício seguro: saindo")
        self.exit_fn()

    def _track_run(self, sid: str, project: str, account: str, prompt: str) -> str:
        """Grava a execução no state: o que sobrar no próximo start foi interrompido."""
        rid = uuid.uuid4().hex[:8]
        st = self.state
        st.interrupted = [e for e in st.interrupted if e["session_id"] != sid][
            -(MAX_INTERRUPTED_KEEP - 1) :
        ] + [
            {
                "id": rid,
                "session_id": sid,
                "project": project,
                "account": account,
                "prompt": prompt[:MAX_RESEND_PROMPT],
                "ts": time.time(),
                "posted": False,
            }
        ]
        self.save()
        return rid

    def _untrack_run(self, rid: str) -> None:
        before = len(self.state.interrupted)
        self.state.interrupted = [e for e in self.state.interrupted if e["id"] != rid]
        if len(self.state.interrupted) != before:
            self.save()

    async def _post_interrupted_entry(self, entry: dict, reason: str) -> None:
        owner = self.state.owner_id
        if owner is None:
            return
        title = await asyncio.to_thread(
            self._title_sync, entry["session_id"], entry["project"]
        )
        label = html.escape(title or self._label(entry["project"]) or "?", quote=False)
        markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔁 Reenviar", callback_data=f"i:{entry['id']}")]]
        )
        msg = await self.send(
            owner, f"⏸ interrompido {reason}: <b>{label}</b>", markup, parse_mode="HTML"
        )
        if msg is not None:
            entry["posted"] = True
            self.save()

    async def post_interrupted(self) -> None:
        """No start: avisa de cada execução que ficou pela metade (reinício forçado, queda…)."""
        now = time.time()
        st = self.state
        # sessão hospedada readotada: o turno seguiu no tmux e o leitor entrega o resultado
        keep = [
            e
            for e in st.interrupted
            if now - e["ts"] <= INTERRUPTED_KEEP_S and e["session_id"] not in self.hosts
        ]
        if len(keep) != len(st.interrupted):
            st.interrupted = keep
            self.save()
        for entry in list(st.interrupted):
            if not entry["posted"]:
                await self._post_interrupted_entry(entry, "pelo reinício")

    async def resend_interrupted(self, chat_id: int, rid: str) -> None:
        if self.draining:  # o pedido continua guardado para depois do reinício
            await self.send(chat_id, DRAIN_MESSAGE)
            return
        entry = next((e for e in self.state.interrupted if e["id"] == rid), None)
        if entry is None:
            await self.send(chat_id, "Esse pedido já foi reenviado ou expirou.")
            return
        self.state.interrupted = [e for e in self.state.interrupted if e["id"] != rid]
        self.save()
        await self.send(chat_id, "🔁 Reenviando…")
        self._spawn(self._safe_run(chat_id, entry["prompt"], target=entry))

    async def shutdown(self) -> None:
        self._shutting_down = (
            True  # as execuções que morrerem agora ficam como "interrompidas"
        )
        for sid in self.runs.running():
            run = self.runs.get(sid)
            if run is not None and not getattr(run, "hosted", False):
                await (
                    run.stop()
                )  # sessão hospedada (tmux) segue viva: é readotada no start
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def register(self, app: Application) -> None:
        for name, fn in (
            ("start", self.cmd_start),
            ("contas", self.cmd_contas),
            ("conta", self.cmd_conta),
            ("projetos", self.cmd_projetos),
            ("sessoes", self.cmd_sessoes),
            ("nova", self.cmd_nova),
            ("parar", self.cmd_parar),
            ("status", self.cmd_status),
            ("historico", self.cmd_historico),
            ("menu", self.cmd_menu),
            ("silencio", self.cmd_silencio),
            ("resumo", self.cmd_resumo),
            ("rc", self.cmd_rc),
            ("destravar", self.cmd_destravar),
            ("trancar", self.cmd_trancar),
            ("encerrar", self.cmd_encerrar),
            ("fixar", self.cmd_fixar),
            ("desafixar", self.cmd_desafixar),
        ):
            app.add_handler(CommandHandler(name, fn))
        app.add_handler(TypeHandler(Update, self.drop_stale), group=-1)
        app.add_handler(MessageHandler(filters.CONTACT, self.on_contact))
        app.add_handler(MessageHandler(filters.VOICE, self.on_voice))
        app.add_handler(
            MessageHandler(
                filters.PHOTO | filters.Document.ALL | filters.VIDEO | filters.AUDIO,
                self.on_file,
            )
        )
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        app.add_handler(CallbackQueryHandler(self.on_callback))
        app.add_error_handler(self.on_error)

    async def drop_stale(self, update: Update, ctx) -> None:
        """Mensagem mais velha que MAX_UPDATE_AGE_S (replay após restart) não vira prompt.
        Avisa o dono uma vez por lote, para não parecer perda silenciosa. Callbacks não entram:
        a data deles é a da mensagem do botão, não a do clique."""
        msg = update.message
        if msg is None or msg.date is None:
            return
        if (self.now() - msg.date).total_seconds() <= MAX_UPDATE_AGE_S:
            return
        if self.authorized(update):
            self._stale += 1
            if self._stale == 1:
                self._spawn(self._notify_stale(update.effective_chat.id))
        raise ApplicationHandlerStop

    async def _notify_stale(self, chat_id: int) -> None:
        await asyncio.sleep(STALE_NOTICE_DELAY_S)  # junta o lote de updates do start
        n, self._stale = self._stale, 0
        await self.send(
            chat_id,
            f"⚠️ {n} mensagem(ns) com mais de {MAX_UPDATE_AGE_S // 60} min ignorada(s) "
            "(enviadas com o bot fora do ar). Reenvie se ainda precisar.",
        )

    async def on_error(self, update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        err = ctx.error
        # traceback completo; o RedactToken (main.py) tira o token das URLs de rede antes de gravar
        log.error("erro em handler (%s)", type(err).__name__, exc_info=err)
        if (
            getattr(update, "effective_chat", None)
            is not None  # é um Update de verdade
            and not isinstance(err, NetworkError)  # rede instável é esperado, não é bug
            and self.authorized(update)
            and self.clock() - self._last_error_notice >= ERROR_NOTICE_EVERY_S
        ):
            self._last_error_notice = self.clock()
            await self.send(
                update.effective_chat.id, f"⚠️ erro interno: {type(err).__name__}"
            )

    # -- auth --

    async def cmd_start(self, update: Update, ctx) -> None:
        chat = update.effective_chat
        if self.state.owner_id is None:
            if chat is None or chat.type != "private":
                return
            kb = ReplyKeyboardMarkup(
                [[KeyboardButton("📱 Compartilhar meu contato", request_contact=True)]],
                one_time_keyboard=True,
                resize_keyboard=True,
            )
            await self.send(
                chat.id, "Para vincular este bot, compartilhe o seu contato.", kb
            )
        elif self.authorized(update):
            self._lazy_menu_button()
            if self.is_locked():
                await self.locked_reply(update)
            else:
                await self.send(chat.id, HELP, self.main_keyboard())

    def _slot_labels(self) -> tuple[str, ...]:
        return tuple(slots_mod.keyboard_row(self.state.slots, set(self.runs.running())))

    def main_keyboard(self) -> ReplyKeyboardMarkup:
        """Teclado fixo: 1ª linha = atalhos fixados (se houver), depois os botões de sempre."""
        row = list(self._slot_labels())
        snap = self._status_snap
        hint = pinned_mod.placeholder(  # texto do campo de mensagem: projeto · conta · % livre
            self._label(self.state.project),
            snap.get("title"),
            self.state.account,
            snap.get("usage"),
            snap.get("working", 0),
            self.is_locked(),
        )
        return ReplyKeyboardMarkup(
            ([row] if row else []) + KEYBOARD_ROWS,
            is_persistent=True,
            resize_keyboard=True,
            input_field_placeholder=hint,
        )

    async def send_keyboard(self, chat_id: int, text: str):
        """Manda `text` com o teclado atual e registra o que o cliente está mostrando."""
        labels = self._slot_labels()
        msg = await self.send(chat_id, text, self.main_keyboard())
        if msg is not None:
            self._kb_sent, self._kb_last = labels, self.clock()
        return msg

    def request_keyboard_refresh(self) -> None:
        """Slots ou ⏳ mudaram? Manda uma mensagem mínima com o teclado novo — só se mudou, no
        máx. 1 a cada KB_REFRESH_MIN_S (o resto espera) e nunca trancado."""
        if (
            self.tg is None
            or self.state.owner_id is None
            or quiet_mod.policy("echo", self.quiet_mode) == "drop"
            or (self._kb_task is not None and not self._kb_task.done())
            or self._slot_labels() == self._kb_sent
        ):
            return
        self._kb_task = asyncio.create_task(self._refresh_keyboard())
        self._tasks.add(self._kb_task)
        self._kb_task.add_done_callback(self._tasks.discard)

    async def _refresh_keyboard(self) -> None:
        wait = self._kb_last + KB_REFRESH_MIN_S - self.clock()
        if wait > 0:
            await asyncio.sleep(wait)
        owner = self.state.owner_id
        if (
            owner is None
            or self.is_locked()
            or quiet_mod.policy("echo", self.quiet_mode) == "drop"
            or self._slot_labels() == self._kb_sent
        ):
            return
        await self.send_keyboard(owner, "⌨️")

    # -- modo silêncio (/silencio, /resumo; política em bot/quiet.py) --

    def _echo_ok(self) -> bool:
        """Eco/progresso/aviso intermediário só sai com o silêncio desligado."""
        return quiet_mod.policy("echo", self.quiet_mode) != "drop"

    async def _enqueue(
        self, kind: str, text: str, sid: str | None, project: str | None
    ) -> None:
        """Guarda na fila do /resumo (em memória + state.json, últimos 50)."""
        title = ""
        if sid and project:
            title = await asyncio.to_thread(self._title_sync, sid, project)
        item = quiet_mod.make_item(
            time.time(), self._label(project), title, kind, text, sid
        )
        self.state.digest = quiet_mod.enqueue(self.state.digest, item)
        self.save()  # persiste e atualiza a barra (🔕total·n)

    async def _deliver(
        self,
        kind: str,
        md: str,
        sid: str | None,
        project: str | None,
        chat_id: int | None = None,
    ) -> bool:
        """Entrega `md` (markdown) conforme a política do modo: envia (com "📖 Ver no painel"),
        envia sem notificar ou vai à fila. True = foi ao chat; False = fila/sem destino."""
        owner = chat_id if chat_id is not None else self.state.owner_id
        if owner is None:
            return False
        action = quiet_mod.policy(kind, self.quiet_mode)
        if action == "queue":
            await self._enqueue(kind, md, sid, project)
            return False
        await self.send_markdown(
            owner, md, self.panel_markup(sid), silent=action == "silent"
        )
        return True

    _QUIET_INFO = {
        "off": "tudo chega como sempre.",
        "final": "só resultados finais e o que precisa de você (perguntas, diálogos, "
        "limites, erros). Sem progresso nem ecos.",
        "total": "só o que precisa de você; resultados e avisos ficam na fila — /resumo "
        "entrega e limpa.",
    }

    def _quiet_text(self, head: str) -> str:
        pend = len(self.state.digest)
        extra = f"\n📬 {pend} pendente(s) — /resumo" if pend else ""
        return (
            f"🔕 {head}: {self.quiet_mode} — {self._QUIET_INFO[self.quiet_mode]}{extra}"
        )

    async def set_quiet(self, chat_id: int, mode: str) -> None:
        self.state.quiet = quiet_mod.valid_mode(mode)
        self.save()
        self.request_keyboard_refresh()
        await self.send(chat_id, self._quiet_text("Silêncio agora"))

    @owner_only
    async def cmd_silencio(self, update: Update, ctx) -> None:
        chat_id = update.effective_chat.id
        args = list(getattr(ctx, "args", None) or [])
        if not args:
            markup = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Off", callback_data="sl:off"),
                        InlineKeyboardButton("Final", callback_data="sl:final"),
                        InlineKeyboardButton("Total", callback_data="sl:total"),
                    ]
                ]
            )
            await self.send(chat_id, self._quiet_text("Silêncio"), markup)
            return
        mode = args[0].strip().lower()
        if mode not in quiet_mod.MODES:
            await self.send(chat_id, "Uso: /silencio off | final | total")
            return
        await self.set_quiet(chat_id, mode)

    @owner_only
    async def cmd_resumo(self, update: Update, ctx) -> None:
        chat_id = update.effective_chat.id
        items = list(self.state.digest)
        if not items:
            await self.send(chat_id, "📭 Nada pendente.")
            return
        await self.send(
            chat_id, f"📬 Resumo: {len(items)} item(ns), do mais antigo ao mais novo"
        )
        done: set[int] = set()
        for it in items:
            if await self._send_card(chat_id, it):
                done.add(id(it))
        self.state.digest = [e for e in self.state.digest if id(e) not in done]
        self.save()

    async def _send_card(self, chat_id: int, it: dict) -> bool:
        def plain(s: str) -> str:
            return re.sub(r"[*_`\[\]]", "", s)

        when = datetime.fromtimestamp(it["ts"]).astimezone().strftime("%H:%M")
        head = " · ".join(
            p
            for p in (
                plain(it["project_label"]),
                plain(_clip(it["title"], 60)),
                when,
                "🔔" if it["kind"] == "notify" else "",
            )
            if p
        )
        body = _clip_keep_lines(it["text"], RESUMO_CARD_CHARS)
        return await self.send_markdown(
            chat_id,
            f"**{head}**\n\n{body}",
            self.panel_markup(it["session_id"]),
            silent=True,
        )

    @owner_only
    async def cmd_menu(self, update: Update, ctx) -> None:
        await self.send_keyboard(update.effective_chat.id, "Menu 👇")

    # -- atalhos (slots 1-4) --

    async def pin_active(self, chat_id: int, n: int | None) -> None:
        st = self.state
        if not st.session_id or not st.project:
            await self.send(
                chat_id, "Sem sessão ativa para fixar. Veja /sessoes ou /nova."
            )
            return
        entry = {
            "session_id": st.session_id,
            "project": st.project,
            "account": st.account or "",
        }
        new, outcome = slots_mod.pin(st.slots, n, entry)
        if outcome == "full":
            rows = [
                [
                    InlineKeyboardButton(
                        f"Substituir {k}·{_base(st.slots[k]['project'])}",
                        callback_data=f"f:{k}:{st.session_id}",
                    )
                ]
                for k in slots_mod.SLOT_KEYS
            ]
            await self.send(
                chat_id,
                "📌 Os 4 atalhos estão em uso. Qual substituir pela sessão ativa?",
                InlineKeyboardMarkup(rows),
            )
            return
        st.slots = new
        self.save()
        slot = next(k for k, v in new.items() if v["session_id"] == st.session_id)
        msg = {
            "pinned": f"📌 Fixada no atalho {slot}.",
            "replaced": f"📌 Atalho {slot} substituído.",
            "already": f"📌 Esta sessão já é o atalho {slot}.",
        }[outcome]
        await self.send_keyboard(
            chat_id,
            f"{msg} Dica: «{slot}: sua mensagem» fala com ela sem trocar de sessão.",
        )

    @owner_only
    async def cmd_fixar(self, update: Update, ctx) -> None:
        args = getattr(ctx, "args", None) or []
        n = None
        if args:
            if args[0] not in slots_mod.SLOT_KEYS:
                await self.send(update.effective_chat.id, "uso: /fixar [1-4]")
                return
            n = int(args[0])
        await self.pin_active(update.effective_chat.id, n)

    @owner_only
    async def cmd_desafixar(self, update: Update, ctx) -> None:
        chat_id = update.effective_chat.id
        args = getattr(ctx, "args", None) or []
        if not args or args[0] not in slots_mod.SLOT_KEYS:
            await self.send(chat_id, "uso: /desafixar <1-4>")
            return
        if args[0] not in self.state.slots:
            await self.send(chat_id, f"O atalho {args[0]} já está vazio.")
            return
        self.state.slots = slots_mod.unpin(self.state.slots, int(args[0]))
        self.save()
        await self.send_keyboard(chat_id, f"📌 Atalho {args[0]} removido.")

    async def switch_to_slot(self, chat_id: int, n: int) -> None:
        st = self.state
        entry = st.slots.get(str(n))
        if entry is None:
            await self.send(chat_id, f"Atalho {n} vazio. Veja /fixar.")
            return
        st.project, st.session_id = entry["project"], entry["session_id"]
        if entry["account"] and any(
            p.name == entry["account"] for p in self.profiles()
        ):
            st.account = entry["account"]
        self.save()
        await self.send(
            chat_id,
            f"🔵 atalho {n} · {_base(st.project)} ativo"
            + (f" (conta {st.account})" if st.account else ""),
        )
        await self.show_last_exchange(chat_id, st.session_id, st.project)

    async def apply_menu_button(self) -> bool:
        """Botão "Painel" (Mini App) ao lado do campo de texto, se WEBAPP_URL estiver definido.
        True = aplicado ou nada a fazer; False = falhou/sem dono (tenta de novo mais tarde)."""
        url = os.environ.get("WEBAPP_URL", "").strip()
        owner = self.state.owner_id
        if owner is None:
            return False
        if not url.startswith("https://"):
            self._menu_set = True
            return True
        try:
            await tg_call(
                self.tg.set_chat_menu_button,
                chat_id=owner,
                menu_button=MenuButtonWebApp(
                    text="Painel", web_app=WebAppInfo(url=url)
                ),
            )
        except TelegramError as e:
            log.warning("menu button falhou (%s)", _safe_err(e))
            return False
        self._menu_set = True
        return True

    def panel_markup(
        self, sid: str | None, rows: list | None = None
    ) -> InlineKeyboardMarkup | None:
        """`rows` (botões que a mensagem já tinha) + "📖 Ver no painel" (Mini App aberto direto
        na conversa da sessão: `<WEBAPP_URL>/#s=<uuid>`), se WEBAPP_URL for https e o sid válido."""
        rows = list(rows or [])
        url = os.environ.get("WEBAPP_URL", "").strip().rstrip("/")
        if url.startswith("https://") and isinstance(sid, str) and is_uuid(sid):
            rows.append(
                [
                    InlineKeyboardButton(
                        "📖 Ver no painel", web_app=WebAppInfo(url=f"{url}/#s={sid}")
                    )
                ]
            )
        return InlineKeyboardMarkup(rows) if rows else None

    def _lazy_menu_button(self) -> None:
        """Se o botão não pôde ser aplicado no start (ex.: o dono ainda não abriu este bot),
        tenta de novo UMA vez, no primeiro update autorizado."""
        if not self._menu_set and not self._menu_lazy_tried:
            self._menu_lazy_tried = True
            self._spawn(self.apply_menu_button())

    # -- trava TOTP --

    def is_locked(self) -> bool:
        if not self.totp_secret:
            return False
        now = self.now().timestamp()
        if now < self._hard_lock_until:
            return True
        until = self.state.unlocked_until
        return not (until is not None and now < until)

    async def locked_reply(self, update: Update) -> None:
        q = update.callback_query
        if q is not None:
            with contextlib.suppress(TelegramError):
                await q.answer("🔒 trancado")
        wait = self._hard_lock_until - self.now().timestamp()
        if wait > 0:
            text = f"🔒 bloqueado por tentativas inválidas — tente em {int(wait // 60) + 1} min"
        else:
            text = "🔒 trancado — /destravar <código>"
        await self.send(update.effective_chat.id, text)

    def _until_label(self) -> str:
        when = datetime.fromtimestamp(self.state.unlocked_until or 0).astimezone()
        same_day = when.date() == self.now().astimezone().date()
        return when.strftime("%H:%M" if same_day else "%d/%m %H:%M")

    async def cmd_destravar(self, update: Update, ctx) -> None:
        if not self.authorized(
            update
        ):  # estranho: silêncio; dono: passa mesmo trancado
            return
        chat_id, msg = update.effective_chat.id, update.message
        if msg is not None and getattr(msg, "message_id", None) is not None:
            with contextlib.suppress(
                TelegramError
            ):  # o código não fica no chat, valendo ou não
                await self.tg.delete_message(chat_id, msg.message_id)
        if not self.totp_secret:
            await self.send(chat_id, "🔓 sem TOTP configurado")
            return
        now = self.now().timestamp()
        if now < self._hard_lock_until:
            await self.locked_reply(update)
            return
        args = getattr(ctx, "args", None) or []
        if not args:
            await self.send(chat_id, "uso: /destravar <código de 6 dígitos>")
            return
        step = totp.verify(self.totp_secret, args[0], now, self.state.totp_last_step)
        if step is None:
            self._totp_fails += 1
            log.warning(
                "código TOTP inválido (%d/%d)", self._totp_fails, MAX_TOTP_FAILS
            )
            if self._totp_fails >= MAX_TOTP_FAILS:
                self._totp_fails = 0
                self._hard_lock_until = now + HARD_LOCK_S
                self.state.unlocked_until = None
                self.save()
                await self.send(
                    chat_id,
                    f"🚨 {MAX_TOTP_FAILS} códigos inválidos — bloqueado por {int(HARD_LOCK_S // 60)} min",
                )
            else:
                await self.send(
                    chat_id, f"Código inválido ({self._totp_fails}/{MAX_TOTP_FAILS})"
                )
            return
        self._totp_fails = 0
        self.state.totp_last_step = step
        self.state.unlocked_until = now + self.unlock_hours * 3600
        self.save()
        await self.send(chat_id, f"🔓 liberado até {self._until_label()}")

    @owner_only
    async def cmd_trancar(self, update: Update, ctx) -> None:
        chat_id = update.effective_chat.id
        if not self.totp_secret:
            await self.send(chat_id, "🔓 sem TOTP configurado — nada a trancar")
            return
        self.state.unlocked_until = None
        self.save()
        await self.send(chat_id, "🔒 trancado")  # não interrompe execuções em andamento

    async def on_contact(self, update: Update, ctx) -> None:
        if self.state.owner_id is not None:
            return  # depois do vínculo, contato nunca revincula
        user, chat, msg = update.effective_user, update.effective_chat, update.message
        contact = msg.contact if msg else None
        if user is None or chat is None or chat.type != "private" or contact is None:
            return
        if contact_ok(
            contact.user_id,
            user.id,
            contact.phone_number or "",
            self.cfg.allowed_phone,
        ):
            self.state.owner_id = user.id
            self.save()
            log.info("dono vinculado")
            await self.send(chat.id, "✅ Vinculado.\n\n" + HELP, self.main_keyboard())
            await self.apply_menu_button()
        else:
            log.warning("contato recusado")
            await self.send(chat.id, "Contato recusado.")

    # -- contas --

    @owner_only
    async def cmd_contas(self, update: Update, ctx) -> None:
        chat_id = update.effective_chat.id
        items = await self.usage()
        now = datetime.now().astimezone()
        active = self.state.account
        rows = [
            [InlineKeyboardButton(button_label(i, active), callback_data=f"a:{i.name}")]
            for i in sort_usage(items)
            if len(f"a:{i.name}".encode()) <= 64  # limite do Telegram p/ callback_data
        ]
        await self.send(
            chat_id,
            format_usage(items, active, now),
            InlineKeyboardMarkup(rows) if rows else None,
            parse_mode="HTML",
        )

    @owner_only
    async def cmd_conta(self, update: Update, ctx) -> None:
        args = getattr(ctx, "args", None) or []
        if not args:
            await self.cmd_contas(update, ctx)
            return
        await self.switch_account(update.effective_chat.id, args[0])

    def apply_account(self, name: str) -> tuple[str | None, str | None, bool]:
        """Troca a conta ativa (copiando a sessão ativa para ela, spec 9b).
        Devolve (erro, mensagem de sucesso, sessão_acompanhou)."""
        profiles = self.profiles()
        prof = next((p for p in profiles if p.name == name), None)
        if prof is None:
            return f"Conta desconhecida: {name[:40]}", None, False
        st = self.state
        sid = st.session_id
        if sid and st.project:
            if self.runs.busy(sid):
                return (
                    "Sessão em execução; use /parar antes de trocar de conta.",
                    None,
                    False,
                )
            src = session_exists(sid, st.project, profiles)
            if src is not None:
                try:
                    copy_session_to(sid, st.project, src, prof.config_dir)
                except (OSError, ValueError) as e:
                    log.warning("cópia de sessão falhou (%s)", type(e).__name__)
                    return "Falha ao copiar a sessão para a nova conta.", None, False
                st.account = name
                self.session_accounts[sid] = name
                self.save()
                self._host_release(sid, name)
                return None, f"🔀 sessão {short(sid)} agora na conta {name}", True
        st.account = name
        self.save()
        return None, f"Conta ativa: {name}", False

    def _host_release(self, sid: str, account: str) -> None:
        """Sessão hospedada que mudou de conta (ociosa: a troca recusa sessão ocupada): encerra o
        tmux já; a próxima mensagem segue no `claude -p --resume` ou re-hospeda na conta nova."""
        host = self.hosts.get(sid)
        if host is None or host.account == account:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._spawn(self._host_kill(host))

    async def switch_account(self, chat_id: int, name: str) -> None:
        err, msg, with_session = self.apply_account(name)
        await self.send(chat_id, err or msg)
        if with_session and self.state.session_id:
            await self.show_last_exchange(chat_id, self.state.session_id)

    # -- resumo da conversa e histórico --

    def _last_exchange_sync(
        self, sid: str, cwd: str | None
    ) -> tuple[str | None, str | None]:
        find = getattr(sessions_mod, "session_file", None)
        last = getattr(sessions_mod, "last_exchange", None)
        cwd = cwd or self.state.project
        if find is None or last is None or not cwd:
            return None, None
        try:
            path = find(cwd, sid, self.profiles())
            return last(path) if path else (None, None)
        except (OSError, ValueError) as e:
            log.warning("resumo da sessão falhou (%s)", type(e).__name__)
            return None, None

    async def last_exchange_for(
        self, sid: str, cwd: str | None = None
    ) -> tuple[str | None, str | None]:
        """(último prompt, última resposta) da sessão; (None, None) se não der para ler.
        Em thread: o .jsonl pode ter dezenas de MB e não pode travar o loop do bot."""
        return await asyncio.to_thread(self._last_exchange_sync, sid, cwd)

    def _title_sync(self, sid: str, cwd: str | None) -> str:
        """Título da sessão (custom/ai-title), "" se indisponível."""
        find = getattr(sessions_mod, "session_file", None)
        tail = getattr(sessions_mod, "tail_meta", None)
        cwd = cwd or self.state.project
        if find is None or tail is None or not cwd:
            return ""
        try:
            path = find(cwd, sid, self.profiles())
            return tail(path)[0] if path else ""
        except (OSError, ValueError):
            return ""

    async def show_last_exchange(
        self, chat_id: int, sid: str, cwd: str | None = None
    ) -> None:
        prompt, reply = await self.last_exchange_for(sid, cwd)
        if not prompt and not reply:
            return
        title = await asyncio.to_thread(self._title_sync, sid, cwd)
        parts = [f"🔵 <b>{html.escape(title, quote=False)}</b>"] if title else []
        if prompt:
            parts.append(
                "Você: "
                + html.escape(_clip(prompt, EXCHANGE_PROMPT_CHARS), quote=False)
            )
        if reply:
            parts.append(
                "Claude: " + md_to_tg_html(_clip_md(reply, EXCHANGE_REPLY_CHARS))
            )
        await self.send_html(chat_id, "\n".join(parts), self.panel_markup(sid))

    def _read_turns_sync(
        self, sid: str, limit: int, before: int | None, cwd: str | None
    ) -> tuple[list, int | None] | None:
        find = getattr(sessions_mod, "session_file", None)
        read = getattr(sessions_mod, "read_history", None)
        cwd = cwd or self.state.project
        if find is None or read is None or not cwd:
            return None
        try:
            path = find(cwd, sid, self.profiles())
            return read(path, limit=limit, before=before) if path else None
        except (OSError, ValueError) as e:
            log.warning("histórico falhou (%s)", type(e).__name__)
            return None

    async def read_turns(
        self, sid: str, limit: int, before: int | None, cwd: str | None = None
    ) -> tuple[list, int | None] | None:
        """Turnos da sessão (cronológicos) + cursor da página anterior; None se indisponível.
        Em thread (ver last_exchange_for)."""
        return await asyncio.to_thread(self._read_turns_sync, sid, limit, before, cwd)

    async def send_history(
        self, chat_id: int, sid: str, n: int, before: int | None
    ) -> None:
        got = await self.read_turns(sid, n, before)
        if got is None:
            await self.send(chat_id, "Histórico indisponível para esta sessão.")
            return
        turns, cursor = got
        body = format_turns(turns) or "<i>(sem mensagens)</i>"
        if before is not None:  # página mais antiga, mandada abaixo da mais nova
            body = "<i>⬆️ mensagens anteriores</i>\n\n" + body
        rows = []
        if cursor is not None:
            rows.append(
                [
                    InlineKeyboardButton(
                        "⬅️ anteriores", callback_data=f"h:{sid}:{cursor}:{n}"
                    )
                ]
            )
        await self.send_html(chat_id, body, self.panel_markup(sid, rows))

    @owner_only
    async def cmd_historico(self, update: Update, ctx) -> None:
        chat_id = update.effective_chat.id
        sid = self.state.session_id
        if not sid:
            await self.send(chat_id, "Sem sessão ativa. Veja /sessoes ou /nova.")
            return
        args = getattr(ctx, "args", None) or []
        n = HISTORY_DEFAULT_N
        if args and args[0].isascii() and args[0].isdigit():
            n = min(max(int(args[0]), 1), HISTORY_MAX_N)
        await self.send_history(chat_id, sid, n, None)

    # -- conta esgotada --

    async def _suggest_accounts(
        self, chat_id: int, current: str, retry: tuple[str, str, str] | None = None
    ) -> None:
        """Conta no limite: oferece as contas utilizáveis. Com `retry` (sid, projeto, prompt),
        tocar numa conta troca E reenvia o mesmo prompt."""
        try:
            rest = [i for i in await self.usage() if i.name != current]
        except Exception as e:  # noqa: BLE001 - sugestão é opcional
            log.warning("sugestão de conta falhou (%s)", type(e).__name__)
            return
        token = None
        if retry is not None:
            token = uuid.uuid4().hex[:8]
            self._retry[token] = retry
            while len(self._retry) > RETRY_KEEP:
                self._retry.popitem(last=False)
        rows = []
        for _ in range(3):
            best = pick_best(rest)
            if best is None:
                break
            item = next(i for i in rest if i.name == best)
            rest = [i for i in rest if i.name != best]
            data = f"r:{token}:{best}" if token else f"a:{best}"
            if len(data.encode()) > 64:  # limite do Telegram p/ callback_data
                continue
            pcts = [
                p
                for p in (item.five_hour.percent, item.seven_day.percent)
                if p is not None
            ]
            pct = f" · {int(max(pcts) + 0.5)}%" if pcts else ""
            label = f"continuar na {best}{pct}" if token else f"🔀 {best}"
            rows.append([InlineKeyboardButton(label, callback_data=data)])
        title = f"⚠️ conta {current} esgotada"
        if rows:
            await self.send(
                chat_id,
                f"{title}. Trocar de conta mantém a sessão:",
                InlineKeyboardMarkup(rows),
            )
        else:
            await self.send(chat_id, f"{title} e não há outra conta utilizável agora.")

    async def retry_with_account(self, chat_id: int, token: str, name: str) -> None:
        item = self._retry.get(token)
        if item is None:
            await self.send(chat_id, "Esse pedido expirou; reenvie o prompt.")
            return
        sid, project, text = item
        st = self.state
        if st.session_id != sid or st.project != project:
            await self.send(chat_id, "A sessão ativa mudou; reenvie o prompt.")
            return
        err, msg, _ = self.apply_account(name)
        if err:
            await self.send(chat_id, err)
            return
        self._retry.pop(token, None)
        await self.send(chat_id, f"{msg} — reenviando o pedido.")
        self._spawn(self._safe_run(chat_id, text))

    # -- projetos e sessões --

    def _extra_projects(self) -> list[str]:
        """`<state_dir>/projects.txt`: um caminho por linha (`#` comenta, `~` = home);
        só diretórios absolutos que existem."""
        try:
            text = (self.cfg.state_dir / "projects.txt").read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return []
        out = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line == "~" or line.startswith("~/"):
                line = str(self.cfg.home / line[2:])
            if line.startswith("/") and Path(line).is_dir():
                out.append(line)
        return out

    def project_list(self) -> list[str]:
        """Home do dono sempre primeiro; depois os workspaces do Horizon e o projects.txt (sem duplicar)."""
        out = [str(self.cfg.home)]
        horizon = load_projects(self.horizon / "config.yaml", self.cfg.home)
        for p in [*horizon, *self._extra_projects()]:
            p = p.rstrip("/") or "/"
            if p not in out:
                out.append(p)
        return out

    @owner_only
    async def cmd_projetos(self, update: Update, ctx) -> None:
        home = str(self.cfg.home)
        rows = [
            [
                InlineKeyboardButton(
                    ("🔵 " if p == self.state.project else "📁 ")
                    + ("~ (home)" if p == home else _base(p)),
                    callback_data=f"p:{i}",
                )
            ]
            for i, p in enumerate(self.project_list())
        ]
        await self.send(
            update.effective_chat.id, "Escolha o projeto:", InlineKeyboardMarkup(rows)
        )

    def session_list(self) -> list[SessionInfo]:
        st = self.state
        profiles = self.profiles()
        infos = list_recent(
            st.project, profiles, self.horizon / "claude-session-accounts.json"
        )
        infos = [
            i
            if i.account
            else replace(i, account=self.session_accounts.get(i.session_id))
            for i in infos
        ]
        have = {i.session_id for i in infos}
        extra = [
            s
            for s in self.bot_sessions.values()
            if s.cwd == st.project and s.session_id not in have
        ]
        return extra + infos

    # -- sessões abertas agora (terminal/Horizon + as do bot) --

    def _registry_open(self) -> list:
        """Sessões vivas do registro do Claude Code (`sessions.list_open`)."""
        list_open = getattr(sessions_mod, "list_open", None)
        if list_open is None:
            return []
        if self.proc_root == Path("/proc"):
            return list_open(self.profiles())
        return list_open(self.profiles(), self.proc_root)  # raiz falsa nos testes

    def _open_core_sync(self) -> dict[str, dict]:
        """sid -> {cwd, account, mtime, pc, bot} das sessões abertas: registro vivo do Claude Code
        (terminal/Horizon) ∪ as que o bot roda/rodou neste processo. `pc` = aberta no PC e não é
        execução do próprio bot."""
        running = set(self.runs.running())
        out: dict[str, dict] = {}
        for o in self._registry_open():
            out[o.session_id] = {
                "cwd": o.cwd,
                "account": o.account,
                "pid": o.pid,
                "title": getattr(o, "title", "") or "",
                "mtime": _aware(o.started_at),
                # sessão hospedada (tmux do bot) não é "do PC": o bot a controla
                "pc": o.session_id not in running and o.session_id not in self.hosts,
                "bot": o.session_id in running,
                "hosted": o.session_id in self.hosts,
            }
        for s in self.bot_sessions.values():
            e = out.get(s.session_id)
            if e is not None:
                e["bot"] = e["bot"] or s.session_id in running
                continue
            out[s.session_id] = {
                "cwd": s.cwd,
                "account": s.account or self.session_accounts.get(s.session_id),
                "mtime": _aware(s.mtime),
                "pc": False,
                "bot": s.session_id
                in running,  # rodou aqui; só trabalha se ainda está rodando
                "hosted": s.session_id in self.hosts,
            }
        for (
            sid,
            h,
        ) in self.hosts.items():  # readotadas no start: sem registro nem bot_sessions
            if sid not in out:
                out[sid] = {
                    "cwd": h.project,
                    "account": h.account,
                    "mtime": self.now(),
                    "pc": False,
                    "bot": sid in running,  # turno aberto (do Telegram ou do claude.ai)
                    "hosted": True,
                }
        return out

    def _open_sessions_sync(self) -> list[dict]:
        """`_open_core_sync` + último prompt e mtime reais do .jsonl; mais recentes primeiro."""
        profiles = self.profiles()
        find = getattr(sessions_mod, "session_file", None)
        last = getattr(sessions_mod, "last_prompt", None)
        tail = getattr(
            sessions_mod, "tail_meta", None
        )  # (título, último prompt) numa leitura só
        items = []
        for sid, e in self._open_core_sync().items():
            e = {**e, "session_id": sid, "last_prompt": "", "has_file": False}
            e.setdefault("title", "")
            try:
                path = find(e["cwd"], sid, profiles) if find else None
                if path:
                    e["mtime"] = datetime.fromtimestamp(Path(path).stat().st_mtime, UTC)
                    e["has_file"] = True
                    if tail is not None:
                        title, prompt = tail(path)
                        e["title"] = e["title"] or title
                        e["last_prompt"] = prompt
                    elif last is not None:
                        e["last_prompt"] = last(path)
            except (OSError, ValueError) as err:
                log.warning("sessão aberta ilegível (%s)", type(err).__name__)
            first = (
                self.bot_sessions[sid].first_prompt if sid in self.bot_sessions else ""
            )
            e["first_prompt"] = first or e["last_prompt"]
            e["last_prompt"] = e["last_prompt"] or first
            e["working"] = self._is_working(e)
            items.append(e)
        return sorted(items, key=lambda e: e["mtime"], reverse=True)

    def _is_working(self, e: dict) -> bool:
        """Trabalhando: execução do bot viva OU sessão do PC cujo .jsonl mexeu há ≤ 20 s."""
        if e["bot"]:
            return True
        return bool(
            e["pc"]
            and e.get("has_file")
            and 0 <= (self.now() - e["mtime"]).total_seconds() <= WORKING_WINDOW_S
        )

    # -- encerrar sessões --

    def _is_our_claude(self, pid: int) -> bool:
        """O processo é do nosso uid e é o `claude` (binário ou `node …/claude`)? Nunca o próprio bot."""
        root = self.proc_root / str(pid)
        try:
            if root.stat().st_uid != os.getuid() or pid in (os.getpid(), 1):
                return False
            argv = [
                a.decode("utf-8", "replace")
                for a in (root / "cmdline").read_bytes().split(b"\0")
                if a
            ]
        except OSError:
            return False
        names = [Path(argv[0]).name] if argv else []
        try:
            names.append(Path(os.readlink(root / "exe")).name)
        except OSError:
            pass
        if any(n.startswith("claude") for n in names):
            return True
        # runtime JS: `node /…/claude/cli.js`
        return (
            bool(argv)
            and Path(argv[0]).name in ("node", "nodejs", "bun")
            and any(
                re.search(r"(?:^|/)claude(?:-code)?(?:/|\.|$)", a) for a in argv[1:4]
            )
        )

    def _terminate_pc_sync(self, sid: str) -> str:
        """SIGTERM numa sessão aberta no PC, só depois de conferir o processo: pid do registro
        vivo (starttime == procStart), nosso uid e executável `claude`. Nunca SIGKILL.
        Devolve "terminated" | "sigterm_sent" | "not_running" | "refused"."""
        entry = next((o for o in self._registry_open() if o.session_id == sid), None)
        if entry is None:
            return "not_running"
        pid = entry.pid
        starttime = getattr(sessions_mod, "_proc_starttime", None)
        t0 = starttime(self.proc_root, pid) if starttime else None
        if t0 is None or not self._is_our_claude(pid):
            log.warning("encerrar sessão recusado: o processo não confere")
            return "refused"
        if (
            starttime(self.proc_root, pid) != t0
        ):  # pid reaproveitado entre a checagem e o kill
            return "refused"
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return "terminated"
        except OSError:
            return "refused"
        for _ in range(int(TERMINATE_WAIT_S / 0.1)):
            if starttime(self.proc_root, pid) != t0:
                return "terminated"
            time.sleep(0.1)
        return "sigterm_sent"

    def _card(self, n: int, e: dict, with_project: bool) -> str:
        """Cartão HTML de uma sessão (sem id): 1️⃣ 🖥 <b>título</b> / “prompt” · há 1 d · conta."""
        marks = (
            ("🔵" if e["session_id"] == self.state.session_id else "")
            + ("⏳" if e.get("working") else "")
            + ("🖥" if e.get("pc") else "")
        )
        title = (
            e.get("title")
            or e.get("last_prompt")
            or e.get("first_prompt")
            or "(sem título)"
        )
        head = f"{KEYCAPS[n - 1]} " + (f"{marks} " if marks else "")
        parts = []
        if e.get("last_prompt"):
            parts.append(
                f"“{html.escape(_clip(e['last_prompt'], CARD_PROMPT_CHARS), quote=False)}”"
            )
        parts.append(age_since(self.now() - e["mtime"]))
        parts.append(html.escape(e.get("account") or "?", quote=False))
        if with_project and e.get("cwd"):
            parts.append(html.escape(self._label(e["cwd"]) or "", quote=False))
        return (
            f"{head}<b>{html.escape(_clip(title, 80), quote=False)}</b>\n"
            f"     {' · '.join(parts)}"
        )

    def _cards_message(
        self, header: str, items: list[dict], with_project: bool, extra_row: bool
    ) -> tuple[str, InlineKeyboardMarkup]:
        items = items[:MAX_OPEN_BUTTONS]
        text = header + "".join(
            f"\n\n{self._card(i + 1, e, with_project)}" for i, e in enumerate(items)
        )
        keys = [
            InlineKeyboardButton(KEYCAPS[i], callback_data=f"s:{e['session_id']}")
            for i, e in enumerate(items)
        ]
        rows = [keys[i : i + 5] for i in range(0, len(keys), 5)]
        if extra_row:
            rows.append(
                [
                    InlineKeyboardButton("📂 Retomar…", callback_data="o:r"),
                    InlineKeyboardButton("🆕 Nova", callback_data="o:n"),
                ]
            )
        return text, InlineKeyboardMarkup(rows)

    @owner_only
    async def cmd_sessoes(self, update: Update, ctx) -> None:
        chat_id = update.effective_chat.id
        items = await asyncio.to_thread(self._open_sessions_sync)
        head = (
            "🗂 <b>Abertas agora</b>\n🖥 = aberta no PC — evite usar ao mesmo tempo"
            if items
            else "🗂 <b>Nenhuma sessão aberta agora</b>"
        )
        text, markup = self._cards_message(
            head, items, with_project=True, extra_row=True
        )
        await self.send(chat_id, text, markup, parse_mode="HTML")

    async def send_resume_list(self, chat_id: int) -> None:
        """ "Retomar…": sessões recentes do projeto ativo (só botões)."""
        project = self.state.project
        if not project:
            await self.send(chat_id, "Escolha um projeto com /projetos.")
            return
        infos = await asyncio.to_thread(self.session_list)
        if not infos:
            await self.send(chat_id, "Nenhuma sessão neste projeto.")
            return
        running = set(self.runs.running())
        core = await asyncio.to_thread(self._open_core_sync)
        items = [
            {
                "session_id": i.session_id,
                "title": i.title,
                "last_prompt": i.last_prompt or i.first_prompt,
                "mtime": _aware(i.last_activity),
                "account": i.account,
                "pc": bool(core.get(i.session_id, {}).get("pc")),
                "working": i.session_id in running,
            }
            for i in infos
        ]
        head = f"📂 <b>Recentes em {html.escape(self._label(project) or '?', quote=False)}</b> — toque para ativar"
        text, markup = self._cards_message(
            head, items, with_project=False, extra_row=False
        )
        await self.send(chat_id, text, markup, parse_mode="HTML")

    def _find_session_sync(self, sid: str) -> tuple[str | None, str | None] | None:
        """(cwd, conta) da sessão: entre as abertas (qualquer projeto) ou as recentes do projeto ativo."""
        e = self._open_core_sync().get(sid)
        if e is not None:
            return e["cwd"], e["account"]
        if not self.state.project:
            return None
        info = next((i for i in self.session_list() if i.session_id == sid), None)
        return (self.state.project, info.account) if info else None

    async def _activate_session(self, sid: str) -> str | None:
        """Aponta projeto/conta/sessão ativos para `sid` (aberta em qualquer projeto ou recente do
        projeto ativo). Devolve o erro ("sessão inválida"/"sessão não encontrada") ou None."""
        if not isinstance(sid, str) or not is_uuid(sid):
            return "sessão inválida"
        found = await asyncio.to_thread(self._find_session_sync, sid)
        if found is None:
            return "sessão não encontrada"
        cwd, account = found
        st = self.state
        if cwd:
            st.project = cwd  # sessão aberta de outro projeto: o projeto acompanha
        st.session_id = sid
        if account and any(p.name == account for p in self.profiles()):
            st.account = account
        self.save()
        return None

    def _activation_text(self, sid: str) -> str:
        st = self.state
        return (
            f"🔵 ativa: {self._label(st.project) or '?'} · {st.account or '?'} · "
            f"{short(sid)} — mande a mensagem aqui"
        )

    async def pick_session(self, chat_id: int, sid: str) -> None:
        err = await self._activate_session(sid)
        if err is not None:
            await self.send(
                chat_id, "Sessão não encontrada." if "encontrada" in err else err
            )
            return
        await self.send(chat_id, self._activation_text(sid))
        await self.show_last_exchange(chat_id, sid, self.state.project)

    @owner_only
    async def cmd_encerrar(self, update: Update, ctx) -> None:
        """Encerra a sessão ativa (com confirmação): execução do bot ou processo aberto no PC."""
        chat_id, sid = update.effective_chat.id, self.state.session_id
        if not sid:
            await self.send(chat_id, "Sem sessão ativa. Veja /sessoes.")
            return
        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("⏹ Encerrar", callback_data=f"e:{sid}"),
                    InlineKeyboardButton("Cancelar", callback_data="e:x"),
                ]
            ]
        )
        await self.send(
            chat_id,
            f"Encerrar a sessão {short(sid)} ({self._label(self.state.project) or '?'})?\n"
            "Se ela estiver aberta no PC, o processo recebe SIGTERM (nunca SIGKILL).",
            markup,
        )

    async def confirm_end(self, chat_id: int, sid: str) -> None:
        res = await self._end_session(sid, "chat")
        if not res.get("ok"):
            await self.send(chat_id, f"❌ {res.get('error', 'falhou')}")
        elif res["outcome"] == "not_running":
            await self.send(chat_id, f"A sessão {short(sid)} não está rodando agora.")

    async def new_session_chat(self, chat_id: int) -> None:
        if not self.state.project:
            await self.send(chat_id, "Escolha um projeto com /projetos.")
            return
        sid = str(uuid.uuid4())
        self.state.session_id = sid
        self.save()
        await self.send(chat_id, f"🆕 sessão {short(sid)}. Mande a primeira mensagem.")

    @owner_only
    async def cmd_nova(self, update: Update, ctx) -> None:
        await self.new_session_chat(update.effective_chat.id)

    @owner_only
    async def cmd_parar(self, update: Update, ctx) -> None:
        chat_id = update.effective_chat.id
        sid = self.state.session_id
        run = self.runs.get(sid) if sid else None
        if run is None or not self.runs.busy(sid):
            await self.send(chat_id, "Nada em execução nesta sessão.")
            return
        self._stopped.add(sid)
        await self.send(chat_id, "⏹ Parando…")
        await run.stop()

    @owner_only
    async def cmd_status(self, update: Update, ctx) -> None:
        st = self.state
        running = [short(s) for s in self.runs.running()]
        up = timedelta(seconds=time.monotonic() - self.started)
        lines = [
            f"Projeto: {st.project or '—'}",
            f"Conta: {st.account or '— (automática)'}",
            f"Sessão: {short(st.session_id) if st.session_id else '—'}",
            f"Em execução: {', '.join(running) if running else 'nada'}",
            f"No ar há: {age(up) if up.total_seconds() >= 60 else 'menos de 1min'}",
            f"🔓 liberado até {self._until_label()}"
            if self.totp_secret
            else "🔓 sem TOTP configurado",
            await power_status(),
        ]
        if self.quiet_mode != "off":
            pend = len(st.digest)
            lines.append(
                f"🔕 Silêncio: {self.quiet_mode}"
                + (f" · {pend} pendente(s) — /resumo" if pend else "")
            )
        login = await self._login_line()
        if login:
            lines.insert(3, login)
        rc_items = await asyncio.to_thread(rc_mod.status)
        if rc_items or self.hosts or rc_mod.accounts():
            lines.insert(
                len(lines) - 1,
                f"🛰 RC: {len(self.hosts)} hospedada(s) · "
                f"{sum(1 for i in rc_items if i['active'])} servidor(es)",
            )
        await self.send(update.effective_chat.id, "\n".join(lines))

    async def _login_line(self) -> str | None:
        """ "Login da conta X vence em N d" (refreshTokenExpiresAt), só quando falta ≤ 30 dias."""
        if not self.state.account:
            return None
        try:
            item = next(
                (i for i in await self.usage() if i.name == self.state.account), None
            )
        except Exception as e:  # noqa: BLE001 - /status funciona sem o uso
            log.warning("status: uso indisponível (%s)", type(e).__name__)
            return None
        exp = getattr(item, "login_expires_at", None)
        if exp is None:
            return None
        left = _aware(exp) - self.now()
        if left.total_seconds() < 0:
            return f"⚠️ Login da conta {item.name} venceu — refaça o login"
        if left > timedelta(days=30):
            return None
        return f"⚠️ Login da conta {item.name} vence em {max(int(left.total_seconds() // 86400), 0)} d"

    # -- Remote Control (personal) --

    def rc_ensure(self) -> None:
        """`/rc server` (manual): garante o servidor Remote Control AVULSO da conta/projeto ativos
        em segundo plano. As contas de RC_ACCOUNTS usam a sessão hospedada (a mesma do chat) e
        não sobem servidor sozinhas. Sem loop rodando (chamada síncrona) não faz nada."""
        account, project = self.state.account, self.state.project
        if not account or not project:
            return
        key = (account, project)
        if key in self._rc_inflight:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._rc_inflight.add(key)
        self._spawn(self._rc_ensure_task(account, project))

    async def _rc_ensure_task(self, account: str, project: str) -> None:
        key = (account, project)
        try:
            prof = next((p for p in self.profiles() if p.name == account), None)
            owner = self.state.owner_id
            if prof is None or owner is None:
                return
            res = await asyncio.to_thread(
                rc_mod.start, account, prof.config_dir, project, self.cfg.claude_bin
            )
            if res.status == "untrusted":
                if key not in self._rc_warned:
                    self._rc_warned.add(key)
                    await self.send(
                        owner,
                        f"⚠️ Remote Control: projeto não confiado na {account} — "
                        f"abra `{account}` uma vez em {project} no PC",
                    )
            elif res.status == "started":
                self._rc_warned.discard(key)
                url = await self._rc_wait_url(res.unit)
                text = f"🛰 Remote Control ligado em {html.escape(self._label(project) or project)} ({html.escape(account)})"
                if url:
                    text += f'\n<a href="{html.escape(url, quote=True)}">{html.escape(url)}</a>'
                await self.send(owner, text, parse_mode="HTML")
            elif res.status == "already":
                await self.send(
                    owner,
                    f"🛰 Remote Control já está ligado em {self._label(project) or project} "
                    f"({account}). Veja /rc.",
                )
            elif res.status == "error":
                log.warning("remote control: falha ao iniciar %s", res.unit)
                await self.send(
                    owner, "❌ Remote Control: falha ao iniciar o servidor."
                )
        except Exception as e:  # noqa: BLE001 - RC é acessório: nunca derruba o fluxo do chat
            log.warning("remote control: erro (%s)", type(e).__name__)
        finally:
            self._rc_inflight.discard(key)

    async def _rc_wait_url(self, unit: str) -> str | None:
        waited = 0.0
        while True:
            items = await asyncio.to_thread(rc_mod.status)
            url = next(
                (
                    i["env_url"]
                    for i in items
                    if i["unit"].removesuffix(".service") == unit and i["env_url"]
                ),
                None,
            )
            if url or waited >= RC_URL_WAIT_S:
                return url
            await asyncio.sleep(RC_URL_POLL_S)
            waited += RC_URL_POLL_S

    def _rc_markup(self, items: list[dict]) -> InlineKeyboardMarkup:
        rows = [
            [
                InlineKeyboardButton(
                    f"⏹ {self._label(i['project']) or i['unit'][:30]}",
                    callback_data="rc:"
                    + i["unit"].removesuffix(".service").rsplit("-", 1)[-1],
                )
            ]
            for i in items
        ]
        if len(items) > 1:
            rows.append([InlineKeyboardButton("⏹ todos", callback_data="rc:all")])
        return InlineKeyboardMarkup(rows)

    def _rc_text(self, items: list[dict]) -> str:
        if not items:
            return "🛰 Nenhum servidor Remote Control rodando."
        lines = ["🛰 Remote Control:"]
        for i in items:
            label = html.escape(self._label(i["project"]) or i["unit"])
            line = f"• {label} · {html.escape(i['account'] or '?')}"
            if not i["active"]:
                line += " · ⏳ reiniciando"
            if i["env_url"]:
                line += (
                    f' · <a href="{html.escape(i["env_url"], quote=True)}">abrir</a>'
                )
            if i["capacity"]:
                line += f" · cap {html.escape(i['capacity'])}"
            lines.append(line)
        return "\n".join(lines)

    async def rc_stop_units(self, units: list[str]) -> int:
        results = [await asyncio.to_thread(rc_mod.stop, u) for u in units]
        for u in units:  # parou de propósito: o próximo "ligado" vale de novo
            self._rc_warned = {
                k
                for k in self._rc_warned
                if rc_mod.unit_name(*k) != u.removesuffix(".service")
            }
        return sum(results)

    @owner_only
    async def cmd_rc(self, update: Update, ctx) -> None:
        chat_id = update.effective_chat.id
        args = getattr(ctx, "args", None) or []
        items = await asyncio.to_thread(rc_mod.status)
        if args and args[0].lower() in ("parar", "stop"):
            n = await self.rc_stop_units([i["unit"] for i in items])
            await self.send(chat_id, f"⏹ Remote Control: {n} servidor(es) parado(s).")
            return
        if args and args[0].lower() in ("server", "servidor"):
            if not self.state.account or not self.state.project:
                await self.send(
                    chat_id, "Escolha a conta (/contas) e o projeto (/projetos)."
                )
                return
            await self.send(chat_id, "🛰 ligando o servidor Remote Control avulso…")
            self.rc_ensure()
            return
        hosted_text = await self._rc_hosted_text()
        await self.send(
            chat_id,
            f"{hosted_text}\n\n{self._rc_text(items)}",
            self._rc_markup(items) if items else None,
            parse_mode="HTML",
        )

    async def _rc_hosted_text(self) -> str:
        """Sessões hospedadas (as mesmas do chat, também no claude.ai): projeto · título · nome RC."""
        if not self.hosts:
            return "🛰 Nenhuma sessão hospedada."
        lines = ["🛰 Sessões hospedadas (Telegram + claude.ai):"]
        for h in list(self.hosts.values()):
            title = await asyncio.to_thread(self._title_sync, h.sid, h.project)
            line = (
                f"• {html.escape(self._label(h.project) or h.project)} · "
                f"{html.escape(title or short(h.sid))} · {html.escape(h.rc_name or '?')}"
            )
            if h.active:
                line += " · ⏳"
            if h.rc_url:
                line += f' · <a href="{html.escape(h.rc_url, quote=True)}">abrir</a>'
            lines.append(line)
        return "\n".join(lines)

    async def rc_callback(self, chat_id: int, which: str) -> None:
        items = await asyncio.to_thread(rc_mod.status)
        if which == "all":
            units = [i["unit"] for i in items]
        else:
            units = [
                i["unit"]
                for i in items
                if i["unit"].removesuffix(".service").endswith("-" + which)
            ]
        if not units:
            await self.send(
                chat_id, "Esse servidor Remote Control já não está rodando."
            )
            return
        n = await self.rc_stop_units(units)
        await self.send(chat_id, f"⏹ Remote Control: {n} servidor(es) parado(s).")

    # -- sessões hospedadas (TUI no tmux com Remote Control, ver bot/hosted.py) --

    def _host_prefix(self, host: hosted_mod.Host) -> str:
        return (
            ""
            if host.sid == self.state.session_id
            else prefix(host.project, host.sid) + " "
        )

    def _host_register(
        self,
        sid: str,
        project: str,
        account: str,
        config_dir: str | Path,
        offset: int,
        rc_name: str,
    ) -> hosted_mod.Host:
        path = Path(config_dir) / "projects" / encode_cwd(project) / f"{sid}.jsonl"
        host = hosted_mod.Host(
            sid,
            project,
            account,
            config_dir,
            hosted_mod.JsonlTail(path, offset),
            rc_name,
        )
        host.stop_cb = self._host_interrupt
        now = self.clock()
        host.last_growth = host.last_alive = now
        self.hosts[sid] = host
        self.runs.attach(sid, host)
        self.session_accounts[sid] = account
        self._spawn(self._host_watch(host))
        return host

    async def _rc_stop_for_project(self, project: str) -> None:
        """A sessão hospedada substitui o servidor RC avulso do mesmo projeto."""
        items = await asyncio.to_thread(rc_mod.status)
        units = [i["unit"] for i in items if i["project"] == project and i["active"]]
        if units:
            n = await self.rc_stop_units(units)
            log.info("sessão hospedada: %d servidor(es) RC avulso(s) parado(s)", n)

    async def _host_ensure(
        self,
        chat_id: int,
        sid: str,
        project: str,
        prof: Profile,
        new: bool,
        env: dict[str, str],
    ) -> hosted_mod.Host | None:
        host = self.hosts.get(sid)
        if host is not None and host.alive:
            return await self._host_ready_or_warn(chat_id, host)
        if not await asyncio.to_thread(rc_mod.is_trusted, prof.config_dir, project):
            await self.send(
                chat_id,
                f"⚠️ Sessão hospedada: projeto não confiado na {prof.name} — "
                f"abra `{prof.name}` uma vez em {project} no PC",
            )
            return None
        await self._rc_stop_for_project(project)
        name = hosted_mod.session_name(sid)
        path = Path(prof.config_dir) / "projects" / encode_cwd(project) / f"{sid}.jsonl"
        offset = await asyncio.to_thread(hosted_mod.file_size, path)
        rc_name = hosted_mod.rc_session_name(project)
        if not await asyncio.to_thread(self.tmux.exists, name):
            tmux_env = {
                **env,
                hosted_mod.ENV_PROJECT: project,
                hosted_mod.ENV_ACCOUNT: prof.name,
            }
            argv = hosted_mod.claude_argv(
                self.cfg.claude_bin,
                sid,
                new,
                rc_name,
                MCP_CONFIG,
                self.hosted_prompt,
            )
            ok, _ = await asyncio.to_thread(
                self.tmux.start, name, project, tmux_env, argv
            )
            if not ok:
                log.warning("sessão hospedada: tmux não subiu")
                await self.send(
                    chat_id, "❌ Falha ao iniciar a sessão hospedada (tmux)."
                )
                return None
            log.info("sessão hospedada iniciada: %s", name)
        host = self._host_register(
            sid, project, prof.name, prof.config_dir, offset, rc_name
        )
        return await self._host_ready_or_warn(chat_id, host)

    async def _host_ready_or_warn(
        self, chat_id: int, host: hosted_mod.Host
    ) -> hosted_mod.Host | None:
        if host.ready or await self._host_wait_ready(host):
            host.ready = True
            return host
        if host.alive:
            await self.send(
                chat_id, "❌ A sessão hospedada não ficou pronta a tempo. Veja /rc."
            )
        return None

    async def _host_wait_ready(self, host: hosted_mod.Host) -> bool:
        """Espera a caixa de entrada do TUI; diálogo na subida vai ao dono (e a espera estica)."""
        began = self.clock()
        deadline = began + HOST_READY_S
        while self.clock() < deadline and host.alive:
            pane = await asyncio.to_thread(self.tmux.capture, host.name)
            state = hosted_mod.pane_state(pane)
            if state == "ready":
                return True
            if state == "dialog":
                await self._host_dialog(host, hosted_mod.parse_dialog(pane))
                deadline = max(deadline, began + HOST_DIALOG_WAIT_S)
            elif not await asyncio.to_thread(self.tmux.exists, host.name):
                await self._host_gone(host)
                return False
            await asyncio.sleep(HOST_READY_POLL_S)
        return False

    async def _hosted_submit(
        self,
        chat_id: int,
        sid: str,
        project: str,
        prof: Profile,
        new: bool,
        text: str,
        env: dict[str, str],
    ) -> hosted_mod.HostedTurn | None:
        """Cola o prompt no TUI da sessão (sobe o tmux se preciso). Ocupada -> RuntimeError;
        falha do tmux -> OSError; falha já avisada ao dono -> None."""
        lock = self._host_locks.setdefault(sid, asyncio.Lock())
        async with lock:
            host = await self._host_ensure(chat_id, sid, project, prof, new, env)
            if host is None:
                return None
            if host.active:
                raise RuntimeError("busy")
            turn = hosted_mod.HostedTurn(text, self.clock())
            host.ours, host.turn_ours = turn, False
            if not await asyncio.to_thread(self._host_type, host, text):
                host.ours = None
                raise OSError("tmux")
            return turn

    def _host_clear_input(self, host: hosted_mod.Host) -> None:
        """Esvazia a caixa de entrada (Esc no começo do turno devolve o prompt para ela)."""
        for _ in range(10):
            box = hosted_mod.input_box(self.tmux.capture(host.name))
            if not box:
                return
            self.tmux.keys(host.name, "C-u", "BSpace", "C-u", "BSpace")
            time.sleep(HOST_TYPE_PAUSE_S)

    def _host_type(self, host: hosted_mod.Host, text: str) -> bool:
        self._host_clear_input(host)
        if not self.tmux.paste(host.name, text):
            return False
        time.sleep(HOST_TYPE_PAUSE_S)  # o TUI processa a colagem antes do Enter
        return self.tmux.keys(host.name, "Enter")

    async def _host_watch(self, host: hosted_mod.Host) -> None:
        """Lê o .jsonl (~0,5 s), entrega eventos, confere se o tmux vive e olha o painel quando
        o turno está aberto e o .jsonl parado (diálogo? fim inferido?)."""
        while host.alive:
            await asyncio.sleep(HOST_POLL_S)
            try:
                rows = await asyncio.to_thread(host.tail.read)
                now = self.clock()
                if rows:
                    host.last_growth = now
                for d in rows:
                    for ev in host.tracker.feed(d):
                        await self._host_event(host, ev)
                if not host.alive:
                    return
                if now - host.last_alive >= HOST_ALIVE_EVERY_S:
                    host.last_alive = now
                    if not await asyncio.to_thread(self.tmux.exists, host.name):
                        await self._host_gone(host)
                        return
                if (
                    host.active
                    and now - host.last_growth >= HOST_IDLE_S
                    and now - host.last_probe >= HOST_PROBE_EVERY_S
                ):
                    host.last_probe = now
                    pane = await asyncio.to_thread(self.tmux.capture, host.name)
                    await self._host_probe(host, pane, now)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - o leitor não pode morrer por uma linha ruim
                log.warning("sessão hospedada: leitor falhou (%s)", type(e).__name__)

    async def _host_event(self, host: hosted_mod.Host, ev: hosted_mod.Ev) -> None:
        owner = self.state.owner_id
        turn = host.ours
        if ev.kind == "url":
            host.rc_url = ev.text
            if not host.url_announced and owner is not None and self._echo_ok():
                host.url_announced = True
                url = html.escape(ev.text or "", quote=True)
                await self.send(
                    owner,
                    f"🛰 {html.escape(self._host_prefix(host))}sessão também no claude.ai "
                    f'({html.escape(host.rc_name)}): <a href="{url}">abrir</a>',
                    parse_mode="HTML",
                )
            return
        if ev.kind == "prompt":
            if (
                turn is not None
                and not turn.bound
                and ev.human
                and hosted_mod.prompt_matches(turn.text, ev.raw or "")
            ):
                turn.bound = host.turn_ours = True
                return
            host.turn_ours = False
            self.feeds.start(
                host.sid
            )  # turno de fora (claude.ai…): o painel acompanha ao vivo
            if ev.human and ev.text and owner is not None and self._echo_ok():
                await self.send(
                    owner,
                    f"{VIA_RC} {self._host_prefix(host)}👤 "
                    f"{_clip_keep_lines(ev.text, HOST_PROMPT_CHARS)}",
                )
            self.request_keyboard_refresh()
            self.request_status_refresh()
            return
        if host.turn_ours and turn is not None:
            if ev.kind == "tool":
                turn.push(Event("tool", tool=ev.tool, detail=ev.detail))
            elif ev.kind == "text":
                turn.push(Event("text", text=ev.text))
            elif ev.kind == "end":
                host.ours, host.turn_ours = None, False
                self._host_dialog_clear(host)
                turn.push(
                    Event(
                        "result",
                        text=ev.text or "",
                        is_error=ev.is_error,
                        duration_ms=ev.duration_ms,
                    )
                )
            return
        if ev.kind == "tool":  # turno de fora: só alimenta o painel (sem mensagem)
            self.feeds.add(host.sid, "tool", tool=ev.tool, detail=ev.detail)
        elif ev.kind == "text":
            self.feeds.add(host.sid, "text", text=ev.text)
        if ev.kind == "end":  # turno que não veio do Telegram (claude.ai, notificação…)
            self.feeds.add(host.sid, "result", text=ev.text)
            self.feeds.finish(
                host.sid,
                {
                    "text": ev.text or "",
                    "cost_usd": None,
                    "duration_ms": ev.duration_ms,
                    "is_error": ev.is_error,
                },
            )
            self._host_dialog_clear(host)
            if ev.text and owner is not None:
                await self._deliver(
                    "urgent" if ev.is_error else "final",
                    f"🛰 {self._host_prefix(host)}{'❌ ' if ev.is_error else ''}{ev.text}",
                    host.sid,
                    host.project,
                )
            self.request_keyboard_refresh()
            self.request_status_refresh()

    async def _host_probe(self, host: hosted_mod.Host, pane: str, now: float) -> None:
        dialog = hosted_mod.parse_dialog(pane)
        if dialog is not None:
            await self._host_dialog(host, dialog)
            return
        self._host_dialog_clear(host)
        turn = host.ours
        if turn is not None and not turn.bound:
            if (
                now - turn.sent_at >= HOST_BIND_TIMEOUT_S
            ):  # o prompt não chegou ao .jsonl
                host.ours = None
                turn.push(
                    Event(
                        "result",
                        text="o terminal não recebeu a mensagem; reenvie (veja /rc).",
                        is_error=True,
                    )
                )
            return
        if (
            host.tracker.in_turn
            and now - host.last_growth >= HOST_END_IDLE_S
            and hosted_mod.pane_state(pane) == "ready"
        ):  # sem marcador de fim (ex.: interrompido no meio): o painel pronto fecha o turno
            for ev in host.tracker.force_end():
                await self._host_event(host, ev)

    async def _host_dialog(self, host: hosted_mod.Host, dialog) -> None:
        """Um diálogo distinto do TUI -> uma mensagem ao dono com botões 1..N, ↵ e ⎋."""
        owner = self.state.owner_id
        if dialog is None or dialog.key in host.dialog_seen or owner is None:
            return
        host.dialog_seen.add(dialog.key)
        host.dialog = dialog
        host.dialog_seq += 1
        host.free_armed = False
        sid8 = host.sid[:8]
        nums = [
            InlineKeyboardButton(
                str(n), callback_data=f"hd:{sid8}:{host.dialog_seq}:{n}"
            )
            for n, _ in dialog.options[:MAX_OPEN_BUTTONS]
        ]
        rows = [nums[i : i + 5] for i in range(0, len(nums), 5)]
        rows.append(
            [
                InlineKeyboardButton(
                    "↵ Enter", callback_data=f"hd:{sid8}:{host.dialog_seq}:ent"
                ),
                InlineKeyboardButton(
                    "⎋ Esc", callback_data=f"hd:{sid8}:{host.dialog_seq}:esc"
                ),
            ]
        )
        text = f"❓ {self._host_prefix(host)}o terminal pergunta:\n\n{dialog.excerpt}"[
            :MAX_ASK_TEXT
        ]
        msg = await self.send(owner, text, InlineKeyboardMarkup(rows))
        host.dialog_mid = msg.message_id if msg is not None else None
        host.dialog_text = text

    def _host_dialog_clear(self, host: hosted_mod.Host) -> None:
        host.dialog = None
        host.dialog_seen.clear()
        host.free_armed = False

    async def _host_dialog_done(self, host: hosted_mod.Host, suffix: str) -> None:
        mid, text = host.dialog_mid, host.dialog_text
        host.dialog, host.free_armed, host.dialog_mid = None, False, None
        owner = self.state.owner_id
        if mid is not None and owner is not None:
            await self._edit_ask(owner, mid, text, suffix)

    def _dialog_host(self) -> hosted_mod.Host | None:
        """Sessão hospedada com diálogo aberto (a ativa primeiro)."""
        active = self.hosts.get(self.state.session_id or "")
        if active is not None and active.dialog is not None:
            return active
        return next((h for h in self.hosts.values() if h.dialog is not None), None)

    def _host_free_answer(self, host: hosted_mod.Host, text: str) -> None:
        """Texto livre no diálogo: escolhe a opção "Type something." (se houver), cola e Enter."""
        dialog = host.dialog
        if dialog is not None and dialog.free is not None and not host.free_armed:
            self.tmux.keys(host.name, str(dialog.free), literal=True)
            time.sleep(HOST_TYPE_PAUSE_S)
        self.tmux.paste(host.name, text)
        time.sleep(HOST_TYPE_PAUSE_S)
        self.tmux.keys(host.name, "Enter")

    def _host_pick(self, host: hosted_mod.Host, dialog, n: int) -> None:
        """Dígito escolhe a opção; se o diálogo continuar igual (o dígito só moveu o cursor), Enter."""
        self.tmux.keys(host.name, str(n), literal=True)
        time.sleep(HOST_KEY_SETTLE_S)
        after = hosted_mod.parse_dialog(self.tmux.capture(host.name))
        if after is not None and after.key == dialog.key:
            self.tmux.keys(host.name, "Enter")

    async def host_answer(self, chat_id: int, sid8: str, seq: int, key: str) -> None:
        host = next((h for h in self.hosts.values() if h.sid.startswith(sid8)), None)
        dialog = host.dialog if host is not None else None
        if host is None or dialog is None or seq != host.dialog_seq:
            await self.send(chat_id, "Esse diálogo já foi encerrado.")
            return
        if key == "esc":
            await asyncio.to_thread(self.tmux.keys, host.name, "Escape")
            await self._host_dialog_done(host, "⎋ Esc")
            return
        if key == "ent":
            await asyncio.to_thread(self.tmux.keys, host.name, "Enter")
            await self._host_dialog_done(host, "↵ Enter")
            return
        n = int(key)
        label = dict(dialog.options).get(n)
        if label is None:
            return
        if n == dialog.free:  # "Type something.": o próximo texto/voz é a resposta
            await asyncio.to_thread(self.tmux.keys, host.name, str(n), literal=True)
            host.free_armed = True
            await self.send(
                chat_id, f"✍️ Digite sua resposta ({_clip(label, ASK_ECHO_CHARS)})"
            )
            return
        await asyncio.to_thread(self._host_pick, host, dialog, n)
        await self._host_dialog_done(host, f"✅ {n}. {_clip(label, ASK_ECHO_CHARS)}")

    async def _host_interrupt(self, host: hosted_mod.Host) -> None:
        """/parar: Esc no TUI (interrompe o turno); a caixa de entrada volta vazia."""
        await asyncio.to_thread(self.tmux.keys, host.name, "Escape")
        await asyncio.sleep(HOST_INTERRUPT_SETTLE_S)
        await asyncio.to_thread(self._host_clear_input, host)
        turn = host.ours
        host.ours, host.turn_ours = None, False
        host.tracker.reset()
        self._host_dialog_clear(host)
        if turn is not None:
            turn.close()
        self.request_keyboard_refresh()
        self.request_status_refresh()

    def _host_forget(self, host: hosted_mod.Host, reason: str | None) -> None:
        host.alive = False
        turn = host.ours
        host.ours = None
        if turn is not None:
            if reason is None:
                turn.close()
            else:
                turn.push(Event("result", text=reason, is_error=True))
        if self.hosts.get(host.sid) is host:
            del self.hosts[host.sid]
        self.runs.detach(host.sid, host)
        self.request_keyboard_refresh()
        self.request_status_refresh()

    async def _host_kill(self, host: hosted_mod.Host) -> None:
        """Encerra a sessão hospedada (kill-session do tmux do próprio bot)."""
        self._host_forget(host, None)
        await asyncio.to_thread(self.tmux.kill, host.name)
        log.info("sessão hospedada encerrada: %s", host.name)

    async def _host_gone(self, host: hosted_mod.Host) -> None:
        """O TUI saiu sozinho (ex.: /exit no claude.ai)."""
        if not host.alive:
            return
        self._host_forget(host, "a sessão hospedada (tmux) encerrou.")
        owner = self.state.owner_id
        if owner is not None:
            await self.send(
                owner,
                f"⏹ sessão hospedada {short(host.sid)} ({self._label(host.project) or '?'}) encerrou.",
            )

    async def adopt_hosts(self) -> int:
        """No start: readota as sessões `cc-*` do tmux do bot (sobrevivem ao reinício)."""
        try:
            names = await asyncio.to_thread(self.tmux.names)
        except Exception as e:  # noqa: BLE001 - sem tmux o bot segue
            log.warning("sessões hospedadas: listagem falhou (%s)", type(e).__name__)
            return 0
        n = 0
        for name in names:
            env = await asyncio.to_thread(self.tmux.env, name)
            sid = env.get("CLAUDE_SESSION_ID", "")
            project = env.get(hosted_mod.ENV_PROJECT, "")
            account = env.get(hosted_mod.ENV_ACCOUNT, "")
            config_dir = env.get("CLAUDE_CONFIG_DIR", "")
            if (
                not is_uuid(sid)
                or hosted_mod.session_name(sid) != name
                or not project.startswith("/")
                or not config_dir.startswith("/")
                or sid in self.hosts
            ):
                continue
            path = Path(config_dir) / "projects" / encode_cwd(project) / f"{sid}.jsonl"
            offset = await asyncio.to_thread(hosted_mod.file_size, path)
            host = self._host_register(
                sid,
                project,
                account,
                config_dir,
                offset,
                hosted_mod.rc_session_name(project),
            )
            host.rc_url = await asyncio.to_thread(hosted_mod.last_rc_url, path)
            host.url_announced = host.ready = True
            n += 1
        if n:
            log.info("sessões hospedadas readotadas: %d", n)
        return n

    # -- callbacks --

    @owner_only
    async def on_callback(self, update: Update, ctx) -> None:
        q = update.callback_query
        if q is None:
            return
        with contextlib.suppress(TelegramError):
            await q.answer()
        chat_id = update.effective_chat.id
        data = q.data or ""
        if m := re.fullmatch(r"p:(\d{1,3})", data):
            projects = self.project_list()
            idx = int(m.group(1))
            if idx < len(projects):
                if projects[idx] != self.state.project:
                    self.state.session_id = None
                self.state.project = projects[idx]
                self.save()
                await self.send(chat_id, f"📁 Projeto: {projects[idx]}")
        elif m := re.fullmatch(r"a:(.+)", data):
            await self.switch_account(chat_id, m.group(1))
        elif m := re.fullmatch(r"s:(.+)", data):
            if is_uuid(m.group(1)):
                await self.pick_session(chat_id, m.group(1))
        elif m := re.fullmatch(r"i:([0-9a-f]{1,16})", data):
            await self.resend_interrupted(chat_id, m.group(1))
        elif m := re.fullmatch(r"rc:(all|[0-9a-f]{6})", data):
            await self.rc_callback(chat_id, m.group(1))
        elif m := re.fullmatch(r"hd:([0-9a-f]{8}):(\d{1,6}):(\d{1,2}|ent|esc)", data):
            await self.host_answer(chat_id, m.group(1), int(m.group(2)), m.group(3))
        elif m := re.fullmatch(r"sl:(off|final|total)", data):
            await self.set_quiet(chat_id, m.group(1))
        elif data == "e:x":
            await self.send(chat_id, "Cancelado.")
        elif m := re.fullmatch(r"e:([0-9a-f-]{36})", data):
            if is_uuid(m.group(1)):
                await self.confirm_end(chat_id, m.group(1))
        elif data == "o:r":
            await self.send_resume_list(chat_id)
        elif data == "o:n":
            await self.new_session_chat(chat_id)
        elif m := re.fullmatch(r"f:([1-4]):([0-9a-f-]{36})", data):
            if self.state.session_id == m.group(
                2
            ):  # a sessão ativa ainda é a do pedido
                await self.pin_active(chat_id, int(m.group(1)))
            else:
                await self.send(chat_id, "A sessão ativa mudou; use /fixar de novo.")
        elif m := re.fullmatch(r"q:([0-9a-f]{8}):(\d{1,2})", data):
            ask_id, idx = m.group(1), int(m.group(2))
            option = self.asks.option_text(ask_id, idx)
            if option is not None and _OTHER_RE.match(option):
                # "Outro…": não resolve; a próxima mensagem de texto/voz responde esta pergunta
                if self.asks.await_text(ask_id):
                    q = _clip(self.asks.question_of(ask_id), ASK_ECHO_CHARS)
                    await self.send(
                        chat_id, f"✍️ Digite sua resposta para: {q or '(pergunta)'}"
                    )
            else:
                self.asks.answer_button(ask_id, idx)
        elif m := re.fullmatch(r"r:([0-9a-f]{8}):(.+)", data):
            await self.retry_with_account(chat_id, m.group(1), m.group(2))
        elif m := re.fullmatch(r"h:([0-9a-f-]{36}):(\d{1,7}):(\d{1,2})", data):
            if is_uuid(m.group(1)):
                n = min(max(int(m.group(3)), 1), HISTORY_MAX_N)
                await self.send_history(chat_id, m.group(1), n, int(m.group(2)))

    # -- texto e voz --

    @owner_only
    async def on_text(self, update: Update, ctx) -> None:
        msg = update.message
        if msg is None or not msg.text:
            return
        action = KEYBOARD_ACTIONS.get(msg.text.strip())
        if action:  # botão do teclado fixo: vira o comando, não vira prompt
            await getattr(self, action)(update, ctx)
            return
        slot = slots_mod.slot_for_button(msg.text.strip(), self.state.slots)
        if slot is not None:  # botão de atalho "1·proj": troca para a sessão fixada
            await self.switch_to_slot(update.effective_chat.id, slot)
            return
        reply = msg.reply_to_message
        await self.handle_text(
            update.effective_chat.id, msg.text, reply.message_id if reply else None
        )

    async def handle_text(self, chat_id: int, text: str, reply_to: int | None) -> None:
        if reply_to is not None:
            if self.asks.answer_reply(reply_to, text):
                return  # resposta a uma pergunta do MCP, não é prompt
            if reply_to in self._closed_asks or self.asks.has_message(reply_to):
                await self.send(chat_id, "Essa pergunta já foi encerrada.")
                return
        # "2: texto" (digitado ou ditado) fala com o atalho 2 sem trocar a sessão ativa
        slot, rest = slots_mod.parse_prefix(text)
        if slot is not None and str(slot) in self.state.slots:
            self._spawn(self._safe_run(chat_id, rest, slot))
            return
        # com pergunta (telegram_perguntar) em aberto, o texto livre/ditado É a resposta: nunca
        # vira prompt (a sessão está esperando, "ocupada"); só sem pergunta vale o fluxo normal
        question = self.asks.answer_free_text(text)
        if question is not None:
            await self.send(
                chat_id,
                f"↩️ respondido: {_clip(question, ASK_ECHO_CHARS) or '(pergunta)'}",
            )
            return
        # diálogo aberto no terminal de uma sessão hospedada: o texto é a resposta
        host = self._dialog_host()
        if host is not None:
            await asyncio.to_thread(self._host_free_answer, host, text)
            await self._host_dialog_done(host, f"✅ {_clip(text, ASK_ECHO_CHARS)}")
            await self.send(chat_id, "↩️ respondido no terminal")
            return
        self._spawn(self._safe_run(chat_id, text))

    async def _safe_run(
        self,
        chat_id: int,
        text: str,
        slot: int | None = None,
        target: dict | None = None,
    ) -> None:
        try:
            if target is not None:
                await self.run_prompt(chat_id, text, target=target)
            elif slot is None:
                await self.run_prompt(chat_id, text)
            else:
                await self.run_prompt(chat_id, text, slot=slot)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - o dono precisa saber que falhou
            log.warning("execução falhou (%s)", type(e).__name__)
            await self.send(chat_id, f"❌ erro interno ({type(e).__name__}).")

    @owner_only
    async def on_voice(self, update: Update, ctx) -> None:
        msg = update.message
        media = (msg.voice or msg.audio) if msg else None
        if media is None:
            return
        chat_id = update.effective_chat.id
        if (media.file_size or 0) > MAX_AUDIO_BYTES:
            await self.send(chat_id, "🎙️ Áudio grande demais (máx. 20 MB).")
            return
        name = "voice.oga" if msg.voice else (media.file_name or "audio.mp3")
        try:
            tgfile = await tg_call(self.tg.get_file, media.file_id)
            data = bytes(await tg_call(tgfile.download_as_bytearray))
            text = await transcribe(data, name, self.cfg.openai_key, self.client)
        except VoiceError as e:
            await self.send(chat_id, f"🎙️ Falha na transcrição: {str(e)[:200]}")
            return
        except TelegramError:
            await self.send(chat_id, "🎙️ Falha ao baixar o áudio.")
            return
        if not text.strip():
            await self.send(chat_id, "🎙️ (áudio sem fala reconhecida)")
            return
        await self.send(chat_id, f"🎙️ {text}")
        reply = msg.reply_to_message
        await self.handle_text(chat_id, text, reply.message_id if reply else None)

    # -- execução do claude --

    async def _pick_profile(
        self, chat_id: int, profiles: list[Profile]
    ) -> Profile | None:
        prof = next((p for p in profiles if p.name == self.state.account), None)
        if prof is not None:
            return prof
        name = pick_best(await self.usage())
        if name is None:
            await self.send(
                chat_id,
                "Nenhuma conta utilizável agora (expiradas ou no limite). Veja /contas.",
            )
            return None
        prof = next((p for p in profiles if p.name == name), None)
        if prof is None:  # perfil sumiu entre usage() e profiles()
            await self.send(chat_id, "Conta indisponível agora. Veja /contas.")
        return prof

    async def run_prompt(
        self,
        chat_id: int,
        text: str,
        slot: int | None = None,
        target: dict | None = None,
    ) -> None:
        """Roda o prompt na sessão ativa — ou, com `slot` (prefixo "2: ...") ou `target`
        ({session_id, project, account}: "🔁 Reenviar"), na sessão indicada SEM mudar
        projeto/conta/sessão ativos."""
        if self.draining:  # reinício seguro em andamento: não começa nada novo
            await self.send(chat_id, DRAIN_MESSAGE)
            return
        st = self.state
        profiles = self.profiles()
        entry = target
        if slot is not None:
            entry = st.slots.get(str(slot))
            if entry is None:
                await self.send(chat_id, f"Atalho {slot} vazio. Veja /fixar.")
                return
        if entry is not None:
            project, sid = entry["project"], entry["session_id"]
            prof = next(
                (p for p in profiles if p.name == entry["account"]), None
            ) or await self._pick_profile(chat_id, profiles)
        else:
            project = st.project
            if not project:
                await self.send(chat_id, "Escolha um projeto com /projetos.")
                return
            prof = None
        if not Path(project).is_dir():
            await self.send(
                chat_id,
                "O projeto do atalho não existe mais. Veja /desafixar."
                if slot is not None
                else "O projeto não existe mais. Veja /projetos."
                if entry is not None
                else "O projeto ativo não existe mais. Veja /projetos.",
            )
            return
        if entry is None:
            prof = await self._pick_profile(chat_id, profiles)
        if prof is None:
            return
        if entry is None:
            if not st.session_id:
                st.session_id = str(uuid.uuid4())
                self.save()
            sid = st.session_id
        if self.runs.busy(sid):
            await self.send(chat_id, "⏳ Sessão ocupada; use /parar para cancelar.")
            return
        src = session_exists(sid, project, profiles)
        new = src is None
        if src is not None and src != Path(prof.config_dir):
            try:
                copy_session_to(sid, project, src, prof.config_dir)
            except (OSError, ValueError) as e:
                log.warning("cópia de sessão falhou (%s)", type(e).__name__)
                await self.send(
                    chat_id, "Falha ao copiar a sessão para a conta escolhida."
                )
                return
        args = build_args(
            self.cfg.claude_bin,
            text,
            sid,
            new,
            ["--mcp-config", str(MCP_CONFIG)],
            system_prompt=self.system_prompt,
        )
        env = {
            "CLAUDE_CONFIG_DIR": str(prof.config_dir),
            "CLAUDE_SESSION_ID": sid,
            "BOT_LOCAL_PORT": str(self.cfg.local_port),
            "CLAUDE_TELEGRAM_STATE_DIR": str(self.cfg.state_dir),
        }
        host = self.hosts.get(sid)
        if (
            host is not None and host.account != prof.name
        ):  # trocou de conta: sai do tmux
            await self._host_kill(host)
        # conta com Remote Control: a sessão vive como TUI no tmux (a mesma do claude.ai)
        hosted = sid in self.hosts or prof.name in rc_mod.accounts()
        try:
            if hosted:
                run = await self._hosted_submit(
                    chat_id, sid, project, prof, new, text, env
                )
                if run is None:
                    return
            else:
                run = await self.runs.launch(sid, args, project, env)
        except RuntimeError:
            await self.send(chat_id, "⏳ Sessão ocupada; use /parar para cancelar.")
            return
        except OSError as e:
            await self.send(
                chat_id, f"❌ Falha ao iniciar o claude ({type(e).__name__})."
            )
            return
        rid = self._track_run(sid, project, prof.name, text)
        prev = self.bot_sessions.get(sid)
        self.bot_sessions[sid] = SessionInfo(
            sid,
            prof.name,
            Path(prof.config_dir),
            project,
            datetime.now(UTC),
            prev.first_prompt if prev else text[:50],
        )
        self.session_accounts[sid] = prof.name
        self.request_keyboard_refresh()  # ⏳ no atalho desta sessão
        self.request_status_refresh()  # ⏳ n na barra

        progress = Progress(self.tg, chat_id)
        if self._echo_ok():  # no silêncio não há mensagem de progresso (nem edições)
            await progress.start("⏳ trabalhando…")
        result = None
        real_sid = None
        began = self.clock()
        self.feeds.start(sid)
        try:
            async for ev in run.events():
                if ev.kind == "init" and ev.session_id:
                    real_sid = ev.session_id
                    self.feeds.add(sid, "init")
                elif ev.kind == "tool":
                    label = ev.tool or "ferramenta"
                    if ev.detail:
                        label += f": {ev.detail}"
                    self.feeds.add(sid, "tool", tool=ev.tool, detail=ev.detail)
                    await progress.update(f"⏳ trabalhando… ({label})")
                elif ev.kind == "text":
                    self.feeds.add(sid, "text", text=ev.text)
                elif ev.kind == "result":
                    result = ev
                    real_sid = ev.session_id or real_sid
                    self.feeds.add(sid, "result", text=ev.text)
        finally:
            await progress.finish()
            self.feeds.finish(
                sid,
                {
                    "text": result.text,
                    "cost_usd": result.cost_usd,
                    "duration_ms": result.duration_ms,
                    "is_error": result.is_error,
                }
                if result
                else None,
                alias=real_sid if real_sid and is_uuid(real_sid) else None,
            )

        if real_sid and real_sid != sid and is_uuid(real_sid):
            self.session_accounts[real_sid] = prof.name
            self.bot_sessions[real_sid] = replace(
                self.bot_sessions[sid], session_id=real_sid
            )
            changed = False
            if st.session_id == sid:
                st.session_id = real_sid
                changed = True
            for entry in st.slots.values():  # o atalho continua apontando para a sessão
                if entry["session_id"] == sid:
                    entry["session_id"] = real_sid
                    changed = True
            if changed:
                self.save()
        stopped = sid in self._stopped
        self._stopped.discard(sid)
        rc = run.returncode
        killed = (
            result is None
            and not stopped
            and not run.timed_out
            and rc is not None
            and (rc in INTERRUPT_CODES or (self._shutting_down and rc < 0))
        )
        if killed:
            # SIGTERM de fora (reinício do serviço, kill): nada de "saiu com código 143". A entrada
            # fica no state; avisa agora (se o bot ainda está de pé) ou no próximo start.
            self.request_status_refresh()
            if not self._shutting_down:
                entry = next(
                    (e for e in self.state.interrupted if e["id"] == rid), None
                )
                if entry is not None:
                    await self._post_interrupted_entry(
                        entry, "(o processo recebeu SIGTERM)"
                    )
            return
        self._untrack_run(rid)
        pfx_label = (
            f"[{slot}·{_base(project)}]" if slot is not None else prefix(project, sid)
        )
        not_active = slot is not None or st.session_id not in (sid, real_sid)
        pfx = pfx_label if not_active else ""
        final = format_final(
            result.text if result else None,
            bool(result and result.is_error),
            result.cost_usd if result else None,
            result.duration_ms if result else None,
            pfx,
            run.stderr_tail(),
            run.returncode,
            run.timed_out,
            stopped,
        )
        view_sid = real_sid if real_sid and is_uuid(real_sid) else sid
        # resultado normal = "final" (silêncio: pode ir à fila); erro, timeout e /parar = urgente
        kind = (
            "final"
            if result and not result.is_error and not (stopped or run.timed_out)
            else "urgent"
        )
        delivered = await self._deliver(kind, final, view_sid, project, chat_id)
        if delivered and result and result.text and len(result.text) > LONG_REPLY_CHARS:
            await self.send_reply_file(chat_id, result.text, sid, pfx_label)
        self.request_keyboard_refresh()  # a sessão deixou de estar ⏳
        self.request_status_refresh()
        if self._echo_ok() and result and not (stopped or run.timed_out):
            took_ms = (
                result.duration_ms
                if result.duration_ms is not None
                else (self.clock() - began) * 1000
            )
            if took_ms > LONG_RUN_NOTICE_S * 1000 or not_active:
                # mensagem curta e separada: gera notificação no celular
                verb = "terminou com erro" if result.is_error else "terminou"
                await self.send(
                    chat_id,
                    f"{'⚠️' if result.is_error else '✅'} {verb} "
                    f"{pfx_label} em {fmt_duration(took_ms)}",
                )
        if result and is_limit_error(result.text, result.is_error):
            use_sid = real_sid if real_sid and is_uuid(real_sid) else sid
            await self._suggest_accounts(
                chat_id,
                prof.name,
                # o reenvio automático só vale para a sessão ativa (troca a conta global)
                retry=None if slot is not None else (use_sid, project, text),
            )

    # -- MCP --

    async def notify(self, texto: str, origem: dict) -> bool:
        owner = self.state.owner_id
        if owner is None:
            return False
        text = f"{origin_prefix(origem)} {texto}"
        action = quiet_mod.policy("notify", self.quiet_mode)
        if action == "queue":
            cwd, sid = origem.get("cwd"), origem.get("session_id")
            await self._enqueue(
                "notify",
                texto,
                sid if isinstance(sid, str) else None,
                cwd if isinstance(cwd, str) else None,
            )
            return True
        return await self.send_long(owner, text, silent=action == "silent")

    async def ask(
        self, pergunta: str, opcoes: list[str], timeout_s: float, origem: dict
    ) -> str | None:
        owner = self.state.owner_id
        if owner is None:
            return None
        text = f"{origin_prefix(origem)} ❓ {pergunta}"[:MAX_ASK_TEXT]
        ask_id, fut = self.asks.create(opcoes, pergunta)
        mid: int | None = None
        try:
            markup = (
                InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton(o[:60], callback_data=f"q:{ask_id}:{i}")]
                        for i, o in enumerate(opcoes)
                    ]
                )
                if opcoes
                else None
            )
            msg = await self.send(owner, text, markup)
            if msg is None:
                return None
            mid = msg.message_id
            self.asks.bind_message(ask_id, mid)
            # a execução do bot não pode estourar o timeout (30 min) enquanto espera o dono
            sid = origem.get("session_id")
            run = self.runs.get(sid) if isinstance(sid, str) else None
            if run is not None:
                run.pause_timeout()
            try:
                answer = await asyncio.wait_for(fut, timeout_s)
            except TimeoutError:
                await self._edit_ask(owner, mid, text, "⌛ sem resposta")
                return None
            except asyncio.CancelledError:
                await self._edit_ask(owner, mid, text, "❎ cancelada")
                raise
            finally:
                if run is not None:
                    run.resume_timeout()
            await self._edit_ask(owner, mid, text, f"✅ {answer[:300]}")
            return answer
        finally:
            if mid is not None:
                self._closed_asks.append(mid)
            self.asks.drop(ask_id)

    # -- arquivos --

    async def send_file(
        self, caminho: object, legenda: str | None, origem: dict
    ) -> tuple[bool, str | None]:
        """/send_file da API local: manda um arquivo ao dono. (ok, erro seguro p/ devolver)."""
        owner = self.state.owner_id
        if owner is None:
            return False, "bot sem dono vinculado"
        if self.is_locked():  # /notify e /ask seguem (só falam com o dono); arquivo não
            return False, "bot trancado (TOTP)"
        try:  # checa e lê no mesmo fd (O_NOFOLLOW + revalidação): sem janela de corrida
            data, filename = await asyncio.to_thread(
                files_mod.read_sendable, caminho, self.cfg.home, self.cfg.state_dir
            )
        except files_mod.FileDenied as e:
            log.warning("send_file recusado (%s)", e)
            return False, str(e)
        caption = f"{origin_prefix(origem)} {legenda or ''}".strip()[
            : files_mod.CAPTION_MAX
        ]
        try:
            await tg_call(
                self.tg.send_document,
                owner,
                document=data,
                filename=filename,
                caption=caption,
            )
        except TelegramError as e:
            log.warning("send_file falhou (%s)", _safe_err(e))
            return False, "falha ao enviar pelo Telegram"
        return True, None

    @owner_only
    async def on_file(self, update: Update, ctx) -> None:
        """foto/documento/vídeo/áudio -> inbox (0600) e vira prompt com o caminho anexado."""
        msg = update.message
        if msg is None:
            return
        chat_id = update.effective_chat.id
        if msg.photo:
            media, name = msg.photo[-1], f"foto-{msg.photo[-1].file_unique_id}.jpg"
        else:
            media = msg.document or msg.video or msg.audio
            if media is None:
                return
            kind = "video" if msg.video else "audio" if msg.audio else "arquivo"
            name = getattr(media, "file_name", None) or f"{kind}-{media.file_unique_id}"
        if (media.file_size or 0) > files_mod.MAX_RECV_BYTES:
            await self.send(chat_id, "📎 Arquivo grande demais (máx. 20 MB).")
            return
        try:
            tgfile = await tg_call(self.tg.get_file, media.file_id)
            data = bytes(await tg_call(tgfile.download_as_bytearray))
            if len(data) > files_mod.MAX_RECV_BYTES:  # file_size pode vir ausente
                await self.send(chat_id, "📎 Arquivo grande demais (máx. 20 MB).")
                return
            path = await asyncio.to_thread(
                files_mod.save_inbox, self.cfg.home, self.now().astimezone(), name, data
            )
        except TelegramError:
            await self.send(chat_id, "📎 Falha ao baixar o arquivo.")
            return
        except OSError as e:
            log.warning("inbox falhou (%s)", type(e).__name__)
            await self.send(chat_id, "📎 Falha ao salvar o arquivo.")
            return
        await self.send(chat_id, f"📎 salvo em {path}")
        prompt = f"{msg.caption or INBOX_PROMPT_DEFAULT}\n\n[arquivo anexado: {path}]"
        reply = msg.reply_to_message
        await self.handle_text(chat_id, prompt, reply.message_id if reply else None)

    # -- fachada para o Mini App (sem Telegram no caminho) --

    def _label(self, project: str | None) -> str | None:
        if not project:
            return None
        return "~ (home)" if project == str(self.cfg.home) else _base(project)

    def _working_sync(self) -> list[dict]:
        """Sessões trabalhando agora (execução do bot, ou do PC com .jsonl mexendo há ≤ 20 s).
        Só `stat` nos arquivos: barato o bastante para o poll do painel."""
        find = getattr(sessions_mod, "session_file", None)
        profiles = self.profiles()
        out = []
        for sid, e in self._open_core_sync().items():
            e = {**e, "has_file": False}
            if e["pc"] and find:
                try:
                    path = find(e["cwd"], sid, profiles)
                    if path:
                        e["mtime"] = datetime.fromtimestamp(
                            Path(path).stat().st_mtime, UTC
                        )
                        e["has_file"] = True
                except (OSError, ValueError):
                    pass
            if self._is_working(e):
                out.append(
                    {
                        "session_id": sid,
                        "short": short(sid),
                        "project_label": self._label(e["cwd"]),
                        "pc": e["pc"],
                        "hosted": bool(e.get("hosted")),
                        "since": e["mtime"].isoformat(),
                    }
                )
        return out

    async def api_state(self) -> dict:
        st = self.state
        home = str(self.cfg.home)
        ac, battery = read_power_raw()
        working = await asyncio.to_thread(self._working_sync)
        return {
            "working": working,
            "project": st.project,
            "project_label": None
            if not st.project
            else ("~ (home)" if st.project == home else _base(st.project)),
            "account": st.account,
            "session_id": st.session_id,
            "running": self.runs.running(),
            "hosted": list(self.hosts),  # sessões hospedadas (Remote Control) vivas
            "quiet": self.quiet_mode,
            "power": {"ac": ac, "battery": battery},
            "slots": {
                k: {
                    "session_id": v["session_id"],
                    "project": v["project"],
                    "project_label": "~ (home)"
                    if v["project"] == home
                    else _base(v["project"]),
                    "account": v["account"] or None,
                    "running": v["session_id"] in set(self.runs.running()),
                }
                for k, v in st.slots.items()
            },
        }

    async def api_accounts(self) -> list[dict]:
        active = self.state.account

        def window(w) -> dict:
            return {
                "percent": w.percent,
                "resets_at": w.resets_at.isoformat() if w.resets_at else None,
            }

        return [
            {
                "name": i.name,
                "five_hour": window(i.five_hour),
                "seven_day": window(i.seven_day),
                "severity": i.severity,
                "state": i.state,
                "active": i.name == active,
                "usable": pick_best([i]) is not None,
            }
            for i in sort_usage(await self.usage())
        ]

    async def select_account(self, name: str) -> str | None:
        err, _, with_session = self.apply_account(name)
        if err is None and with_session and self.state.owner_id is not None:
            await self.show_last_exchange(self.state.owner_id, self.state.session_id)
        return err

    def api_projects(self) -> list[dict]:
        home = str(self.cfg.home)
        return [
            {
                "path": p,
                "label": "~ (home)" if p == home else _base(p),
                "active": p == self.state.project,
            }
            for p in self.project_list()
        ]

    def select_project(self, path: str) -> str | None:
        if not isinstance(path, str) or path not in self.project_list():
            return "projeto desconhecido"
        if path != self.state.project:
            self.state.session_id = None
        self.state.project = path
        self.save()
        return None

    def _session_item(
        self,
        sid: str,
        project: str | None,
        account: str | None,
        mtime: datetime,
        last_prompt: str,
        first_prompt: str,
        running: set[str],
        core: dict,
        title: str = "",
    ) -> dict:
        e = core.get(sid)
        pc = bool(e and e["pc"])
        is_bot = sid in running
        # trabalhando: execução do bot viva OU sessão do PC com .jsonl mexendo há ≤ 20 s
        working = is_bot or (
            pc and 0 <= (self.now() - mtime).total_seconds() <= WORKING_WINDOW_S
        )
        return {
            "session_id": sid,
            "short": short(sid),
            "project": project,
            "project_label": self._label(project),
            "account": account,
            "title": title or last_prompt or first_prompt,
            "mtime": mtime.isoformat(),
            "age_s": max(0, int((self.now() - mtime).total_seconds())),
            "last_prompt": last_prompt,
            "first_prompt": first_prompt,
            "active": sid == self.state.session_id,
            "running": is_bot,
            "pc": pc,
            "hosted": bool(e and e.get("hosted")),
            "working": working,
            "open": e is not None,
        }

    def _api_sessions_sync(self, scope: str) -> list[dict]:
        running = set(self.runs.running())
        if scope == "open":
            return [
                self._session_item(
                    e["session_id"],
                    e["cwd"],
                    e["account"],
                    e["mtime"],
                    e["last_prompt"],
                    e["first_prompt"],
                    running,
                    {e["session_id"]: e},
                    e.get("title", ""),
                )
                | {"working": e["working"]}
                for e in self._open_sessions_sync()
            ]
        if not self.state.project:
            return []
        core = self._open_core_sync()
        return [
            self._session_item(
                i.session_id,
                self.state.project,
                i.account,
                _aware(i.mtime),
                i.last_prompt or i.first_prompt,
                i.first_prompt,
                running,
                core,
                i.title,
            )
            for i in self.session_list()
        ]

    async def api_sessions(self, scope: str = "recent") -> list[dict]:
        """scope "recent": recentes do projeto ativo; "open": abertas agora (terminal/Horizon + bot)
        de qualquer projeto. `pc`: aberta no PC (não é execução do bot); `working`: o bot roda OU
        o .jsonl do PC mexeu há ≤ 20 s. Leitura de arquivos em thread."""
        return await asyncio.to_thread(self._api_sessions_sync, scope)

    async def select_session(self, sid: str) -> str | None:
        """Ativa a sessão (projeto/conta vêm dela; sessão do PC usa a conta do registro) e posta no
        chat "🔵 ativa: …" + o resumo da conversa. Devolve o erro ou None."""
        err = await self._activate_session(sid)
        owner = self.state.owner_id
        if err is None and owner is not None:
            await self.send(owner, self._activation_text(sid))
            await self.show_last_exchange(owner, sid, self.state.project)
        return err

    def new_session(
        self, project: str | None = None, account: str | None = None
    ) -> str:
        """Nova sessão (uuid). Com `project`/`account` (do painel) já aponta o projeto/conta ativos;
        valor inválido -> texto do erro (não é uuid) e nada muda."""
        st = self.state
        if project is not None and project not in self.project_list():
            return "projeto desconhecido"
        if account is not None and not any(p.name == account for p in self.profiles()):
            return "conta desconhecida"
        if project is not None:
            st.project = project
        if account is not None:
            st.account = account
        sid = str(uuid.uuid4())
        st.session_id = sid
        self.save()
        return sid

    async def use_session(self, sid: str) -> dict:
        err = await self.select_session(sid)
        return {"error": err} if err else {"ok": True}

    async def _end_session(self, sid: str, via: str) -> dict:
        if not isinstance(sid, str) or not is_uuid(sid):
            return {"ok": False, "error": "sessão inválida"}
        run = self.runs.get(sid)
        host = self.hosts.get(sid)
        if host is not None:  # sessão hospedada: o tmux é filho do bot -> kill-session
            if host.active:
                self._stopped.add(sid)
            await self._host_kill(host)
            outcome = "stopped"
        elif run is not None and self.runs.busy(
            sid
        ):  # execução do BOT: SIGTERM→SIGKILL no filho
            self._stopped.add(sid)
            await run.stop()
            outcome = "stopped"
        else:  # sessão aberta no PC: SIGTERM só após conferir o processo; nunca SIGKILL
            outcome = await asyncio.to_thread(self._terminate_pc_sync, sid)
            if outcome == "refused":
                return {
                    "ok": False,
                    "error": "o processo não confere; nada foi encerrado",
                }
        owner = self.state.owner_id
        if outcome != "not_running" and owner is not None:
            text = (
                f"⏹ SIGTERM enviado à sessão {short(sid)} (ainda encerrando)"
                if outcome == "sigterm_sent"
                else f"⏹ sessão {short(sid)} encerrada (pelo {via})"
            )
            await self.send(owner, text)
        return {"ok": True, "outcome": outcome}

    async def end_session(self, sid: str) -> dict:
        """Encerra a sessão a pedido do painel: "stopped" (execução do bot), "terminated"/
        "sigterm_sent" (sessão do PC) ou "not_running"."""
        return await self._end_session(sid, "painel")

    async def api_history(
        self, sid: str, limit: int = 30, before: int | None = None
    ) -> dict:
        if not isinstance(sid, str) or not is_uuid(sid):
            return {"turns": [], "before": None}
        got = await self.read_turns(sid, min(max(int(limit), 1), 200), before)
        if got is None:
            return {"turns": [], "before": None}
        turns, cursor = got
        return {
            "turns": [
                {
                    "role": t.role,
                    "text": t.text,
                    "tool": t.tool,
                    "ts": t.ts.isoformat() if t.ts else None,
                }
                for t in turns
            ],
            "before": cursor,
        }

    def run_feed(self, sid: str, after_seq: int = 0) -> dict:
        return self.feeds.snapshot(sid, after_seq)

    async def stop_active(self) -> bool:
        sid = self.state.session_id
        run = self.runs.get(sid) if sid else None
        if run is None or not self.runs.busy(sid):
            return False
        self._stopped.add(sid)
        await run.stop()
        return True


def _facade_locked(empty: Callable[[], object]):
    """Trancado (TOTP): a fachada do painel não age nem revela nada (defesa em profundidade;
    o webapp também responde 423 antes de chamar)."""

    def deco(fn):
        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def awrapper(self, *a, **k):
                return empty() if self.is_locked() else await fn(self, *a, **k)

            return awrapper

        @functools.wraps(fn)
        def wrapper(self, *a, **k):
            return empty() if self.is_locked() else fn(self, *a, **k)

        return wrapper

    return deco


for _name, _empty in {
    "api_state": lambda: {"locked": True},
    "api_accounts": list,
    "select_account": lambda: "locked",
    "api_projects": list,
    "select_project": lambda: "locked",
    "api_sessions": list,
    "select_session": lambda: "locked",
    "new_session": str,
    "api_history": lambda: {"turns": [], "before": None},
    "run_feed": lambda: {"running": False, "events": [], "result": None},
    "use_session": lambda: {"error": "locked"},
    "end_session": lambda: {"ok": False, "error": "locked"},
    "stop_active": lambda: False,
}.items():
    setattr(Bot, _name, _facade_locked(_empty)(getattr(Bot, _name)))
del _name, _empty
