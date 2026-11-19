"""Buffer circular por execução: eventos recentes do stream + resultado final (para o Mini App)."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

MAX_EVENTS = 300
KEEP_AFTER_FINISH_S = 30 * 60.0
MAX_TEXT = 4000


class Feed:
    def __init__(self) -> None:
        self.events: deque[dict[str, Any]] = deque(maxlen=MAX_EVENTS)
        self.seq = 0
        self.running = True
        self.result: dict[str, Any] | None = None
        self.finished_at: float | None = None


class FeedStore:
    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._feeds: dict[str, Feed] = {}
        self._clock = clock
        self._wall = wall

    def _purge(self) -> None:
        now = self._clock()
        for sid in [
            s
            for s, f in self._feeds.items()
            if f.finished_at is not None and now - f.finished_at > KEEP_AFTER_FINISH_S
        ]:
            del self._feeds[sid]

    def start(self, sid: str) -> None:
        self._purge()
        self._feeds[sid] = Feed()

    def add(
        self,
        sid: str,
        kind: str,
        tool: str | None = None,
        detail: str | None = None,
        text: str | None = None,
    ) -> None:
        feed = self._feeds.get(sid)
        if feed is None:
            return
        feed.seq += 1
        feed.events.append(
            {
                "seq": feed.seq,
                "kind": kind,
                "tool": tool,
                "detail": detail,
                "text": text[:MAX_TEXT] if text else text,
                "ts": self._wall().isoformat(),
            }
        )

    def finish(
        self,
        sid: str,
        result: dict[str, Any] | None,
        alias: str | None = None,
    ) -> None:
        """Marca o fim; `alias` (id real da sessão, se mudou) aponta para o mesmo feed."""
        feed = self._feeds.get(sid)
        if feed is None:
            return
        feed.running = False
        feed.result = result
        feed.finished_at = self._clock()
        if alias and alias != sid:
            self._feeds[alias] = feed

    def snapshot(self, sid: str, after_seq: int = 0) -> dict[str, Any]:
        self._purge()
        feed = self._feeds.get(sid)
        if feed is None:
            return {"running": False, "events": [], "result": None}
        return {
            "running": feed.running,
            "events": [dict(e) for e in feed.events if e["seq"] > after_seq],
            "result": dict(feed.result) if feed.result else None,
        }
