"""Barra de status fixada no topo do chat privado do dono, mantida atualizada.

Uma mensagem silenciosa, fixada, editada no lugar (com rate limit e coalescência).
Nada aqui levanta para o chamador: falha de rede/Telegram é logada só pelo tipo.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

from telegram.error import BadRequest, RetryAfter

from bot.usage import AccountUsage

log = logging.getLogger(__name__)

MIN_EDIT_INTERVAL_S = 3.0
MAX_RETRY_AFTER_S = 60.0
TITLE_MAX = 40
LINE_MAX = 120
PLACEHOLDER_MAX = 64
SEP = " · "

_NOT_FOUND = "message to edit not found"
_NOT_MODIFIED = "message is not modified"


def _clip(s: str, limit: int) -> str:
    return s if len(s) <= limit else s[: max(limit - 1, 0)] + "…"


def _percent_livre(usage: AccountUsage | None) -> int | None:
    """100 − max(5h, 7d); janelas nulas ignoradas; nenhuma janela -> None."""
    if usage is None:
        return None
    vals = [
        w.percent
        for w in (usage.five_hour, usage.seven_day)
        if w is not None and w.percent is not None
    ]
    if not vals:
        return None
    return round(min(max(100 - max(vals), 0), 100))


def status_line(
    project_label: str | None,
    title: str | None,
    account: str | None,
    usage: AccountUsage | None,
    working: int = 0,
    locked: bool = False,
    quiet: str = "",
) -> str:
    """Ex.: `📍 project-a · Refatorar autenticação · work (91% livre) 🔕final ⏳ 2`; `🔒` na frente
    se travado; `🔕<modo>` quando o modo silêncio não é `off` (`quiet` já vem como rótulo)."""
    parts: list[str] = []
    if project_label:
        parts.append(project_label)
    if title:
        parts.append(_clip(title, TITLE_MAX))
    if account:
        livre = _percent_livre(usage)
        parts.append(account if livre is None else f"{account} ({livre}% livre)")
    prefix = "🔒 " if locked else ""
    suffix = (f" 🔕{quiet}" if quiet else "") + (
        f" ⏳ {working}" if working > 0 else ""
    )
    body = "📍 " + (SEP.join(parts) if parts else "sem projeto")
    return prefix + _clip(body, LINE_MAX - len(prefix) - len(suffix)) + suffix


def placeholder(
    project_label: str | None,
    title: str | None,
    account: str | None,
    usage: AccountUsage | None,
    working: int = 0,
    locked: bool = False,
) -> str:
    """Versão compacta (`project-a · work · 91%`) para `input_field_placeholder`. `title` é ignorado."""
    parts: list[str] = []
    if project_label:
        parts.append(project_label)
    if account:
        parts.append(account)
    livre = _percent_livre(usage)
    if livre is not None:
        parts.append(f"{livre}%")
    prefix = "🔒 " if locked else ""
    suffix = f" ⏳{working}" if working > 0 else ""
    body = SEP.join(parts) if parts else "Mensagem"
    return prefix + _clip(body, PLACEHOLDER_MAX - len(prefix) - len(suffix)) + suffix


def _seconds(v: object) -> float:
    total = getattr(v, "total_seconds", None)
    return float(total() if callable(total) else v)  # type: ignore[arg-type]


class PinnedStatus:
    def __init__(
        self,
        bot,
        chat_id: int,
        store_path: Path,
        min_interval: float = MIN_EDIT_INTERVAL_S,
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.store_path = Path(store_path)
        self.min_interval = min_interval
        self._message_id: int | None = None
        self._text: str | None = None  # último texto sabidamente na mensagem
        self._pending: str | None = None
        self._flush_task: asyncio.Task | None = None
        self._last_edit = float("-inf")
        self._not_before = float("-inf")  # RetryAfter
        self._lock = asyncio.Lock()
        self._loaded = False

    # --- persistência ----------------------------------------------------

    def _load(self) -> None:
        self._loaded = True
        try:
            data = json.loads(self.store_path.read_text())
        except (OSError, ValueError):
            return
        if not isinstance(data, dict) or data.get("chat_id") != self.chat_id:
            return
        mid, text = data.get("message_id"), data.get("text")
        if isinstance(mid, int) and not isinstance(mid, bool):
            self._message_id = mid
            self._text = text if isinstance(text, str) else None

    def _save(self) -> None:
        if self._message_id is None:
            return
        try:
            self.store_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            tmp = self.store_path.with_name(self.store_path.name + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(
                        {
                            "chat_id": self.chat_id,
                            "message_id": self._message_id,
                            "text": self._text,
                        },
                        f,
                    )
                    f.flush()
                    os.fsync(f.fileno())
                os.chmod(tmp, 0o600)
                os.replace(tmp, self.store_path)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
        except OSError as e:
            log.warning("pinned: falha ao gravar store (%s)", type(e).__name__)

    # --- API pública -----------------------------------------------------

    async def update(self, text: str) -> None:
        try:
            async with self._lock:
                if not self._loaded:
                    self._load()
                if self._message_id is not None and text == self._text:
                    self._pending = None  # voltou ao que já está lá
                    return
                if self._message_id is None:
                    await self._send(text)
                    return
                wait = max(
                    self._last_edit + self.min_interval - time.monotonic(),
                    self._not_before - time.monotonic(),
                )
                if wait <= 0:
                    self._pending = None
                    await self._edit(text)
                else:
                    self._pending = text
                    self._schedule_flush(wait)
        except Exception as e:  # noqa: BLE001 - nunca propaga
            log.warning("pinned: update falhou (%s)", type(e).__name__)

    async def close(self) -> None:
        task, self._flush_task = self._flush_task, None
        self._pending = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    # --- internos --------------------------------------------------------

    def _schedule_flush(self, delay: float) -> None:
        if self._flush_task is not None and not self._flush_task.done():
            return
        self._flush_task = asyncio.get_running_loop().create_task(self._flush(delay))

    async def _flush(self, delay: float) -> None:
        try:
            await asyncio.sleep(max(delay, 0))
            async with self._lock:
                self._flush_task = None
                text, self._pending = self._pending, None
                if text is None or text == self._text:
                    return
                wait = max(
                    self._last_edit + self.min_interval - time.monotonic(),
                    self._not_before - time.monotonic(),
                )
                if wait > 0:  # RetryAfter chegou entre o agendamento e agora
                    self._pending = text
                    self._schedule_flush(wait)
                    return
                await self._edit(text)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("pinned: flush falhou (%s)", type(e).__name__)

    async def _send(self, text: str) -> None:
        try:
            msg = await self.bot.send_message(
                self.chat_id, text, disable_notification=True
            )
        except Exception as e:  # noqa: BLE001
            log.warning("pinned: envio falhou (%s)", type(e).__name__)
            return
        self._message_id = msg.message_id
        self._text = text
        self._pending = None
        self._last_edit = time.monotonic()
        self._save()
        try:
            await self.bot.pin_chat_message(
                self.chat_id, self._message_id, disable_notification=True
            )
        except Exception as e:  # noqa: BLE001
            log.warning("pinned: fixar falhou (%s)", type(e).__name__)

    async def _edit(self, text: str) -> None:
        """Chamar com o lock tomado."""
        try:
            await self.bot.edit_message_text(
                text, chat_id=self.chat_id, message_id=self._message_id
            )
        except RetryAfter as e:
            wait = min(_seconds(e.retry_after), MAX_RETRY_AFTER_S)
            self._not_before = time.monotonic() + wait
            self._pending = text
            self._schedule_flush(wait)
            return
        except BadRequest as e:
            msg = str(e).lower()
            if _NOT_MODIFIED in msg:
                self._text = text
                self._last_edit = time.monotonic()
                self._save()
            elif _NOT_FOUND in msg:
                self._message_id = None
                self._text = None
                await self._send(text)
            else:
                log.warning("pinned: edição recusada (%s)", type(e).__name__)
            return
        except Exception as e:  # noqa: BLE001
            log.warning("pinned: edição falhou (%s)", type(e).__name__)
            return
        self._text = text
        self._last_edit = time.monotonic()
        self._save()
