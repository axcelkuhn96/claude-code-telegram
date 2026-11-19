"""Parse do stream-json do `claude -p --output-format stream-json --verbose`."""

from __future__ import annotations

import json
from dataclasses import dataclass

DETAIL_MAX = 60
# ordem de prioridade do "alvo curto" de um tool_use
DETAIL_KEYS = ("file_path", "command", "pattern", "url", "path")


@dataclass
class Event:
    kind: str  # "init" | "tool" | "text" | "result" | "other"
    session_id: str | None = None
    tool: str | None = None
    detail: str | None = None
    text: str | None = None
    is_error: bool = False
    cost_usd: float | None = None
    duration_ms: int | None = None


def _detail(tool_input: object) -> str | None:
    if not isinstance(tool_input, dict):
        return None
    for key in DETAIL_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            one_line = " ".join(value.split())
            if len(one_line) > DETAIL_MAX:
                one_line = one_line[: DETAIL_MAX - 1] + "…"
            return one_line
    return None


def _assistant_events(d: dict) -> list[Event]:
    message = d.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return [Event("other")]
    sid = _sid(d)
    events: list[Event] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use":
            name = block.get("name")
            events.append(
                Event(
                    "tool",
                    session_id=sid,
                    tool=name if isinstance(name, str) else None,
                    detail=_detail(block.get("input")),
                )
            )
        elif block.get("type") == "text" and isinstance(block.get("text"), str):
            events.append(Event("text", session_id=sid, text=block["text"]))
    return events


def _number(value: object, kind: type) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return kind(value)
    except (OverflowError, ValueError):  # ex.: int(inf)
        return None


def _sid(d: dict) -> str | None:
    sid = d.get("session_id")
    return sid if isinstance(sid, str) else None


def parse_line(line: str) -> list[Event]:
    """Uma linha do stream -> eventos. Nunca levanta: JSON inválido/não-objeto -> []."""
    try:
        d = json.loads(line)
    except (ValueError, TypeError, RecursionError):
        return []
    if not isinstance(d, dict):
        return []
    kind = d.get("type")
    if kind == "system" and d.get("subtype") == "init":
        return [Event("init", session_id=_sid(d))]
    if kind == "assistant":
        return _assistant_events(d)
    if kind == "result":
        text = d.get("result")
        return [
            Event(
                "result",
                session_id=_sid(d),
                text=text if isinstance(text, str) else None,
                is_error=bool(d.get("is_error")),
                cost_usd=_number(d.get("total_cost_usd"), float),
                duration_ms=_number(d.get("duration_ms"), int),
            )
        ]
    return [Event("other")]
