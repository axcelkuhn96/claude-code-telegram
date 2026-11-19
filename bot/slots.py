"""Atalhos (slots 1-4) de sessões fixadas no teclado do Telegram.

Funções puras: nunca mutam a entrada. slots = {"1".."4": {"session_id", "project", "account"}}.
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

SLOT_KEYS = ("1", "2", "3", "4")
_PREFIX = re.compile(r"^\s*([1-4])\s*[:：]\s*(.+)", re.DOTALL)
_FIELDS = ("session_id", "project", "account")
_RUNNING = " ⏳"


def _is_uuid(s: object) -> bool:
    try:
        return isinstance(s, str) and str(uuid.UUID(s)) == s
    except ValueError:
        return False


def parse_prefix(text: str) -> tuple[int | None, str]:
    m = _PREFIX.match(text)
    if not m or not m.group(2).strip():
        return None, text
    return int(m.group(1)), m.group(2)


def pin(slots: dict, n: int | None, entry: dict) -> tuple[dict, str]:
    if n is not None and str(n) not in SLOT_KEYS:
        raise ValueError(f"slot inválido: {n!r}")
    new = {k: dict(v) for k, v in slots.items()}
    sid = entry["session_id"]
    current = next((k for k, v in new.items() if v.get("session_id") == sid), None)
    if n is None:
        if current is not None:
            return new, "already"
        free = next((k for k in SLOT_KEYS if k not in new), None)
        if free is None:
            return new, "full"
        new[free] = dict(entry)
        return new, "pinned"
    key = str(n)
    if current == key:
        return new, "already"
    if current is not None:
        del new[current]
    outcome = "replaced" if key in new else "pinned"
    new[key] = dict(entry)
    return new, outcome


def unpin(slots: dict, n: int) -> dict:
    return {k: dict(v) for k, v in slots.items() if k != str(n)}


def _basename(project: str) -> str:
    p = project.rstrip("/")
    if p and p == os.path.expanduser("~").rstrip("/"):
        return "~"
    return (Path(p).name if p else "/")[:10]


def label(n: int, entry: dict, running: bool) -> str:
    return f"{n}·{_basename(entry.get('project') or '')}" + (
        _RUNNING if running else ""
    )


def keyboard_row(slots: dict, running_ids: set[str]) -> list[str]:
    return [
        label(int(k), slots[k], slots[k].get("session_id") in running_ids)
        for k in SLOT_KEYS
        if k in slots
    ]


def slot_for_button(text: str, slots: dict) -> int | None:
    t = text.removesuffix(_RUNNING)
    if len(t) > 2 and t[1] == "·" and t[0] in slots:
        return int(t[0])
    return None


def validate(slots: object) -> dict:
    if not isinstance(slots, dict):
        return {}
    out: dict[str, dict] = {}
    seen: set[str] = set()
    for k in SLOT_KEYS:
        v = slots.get(k)
        if not isinstance(v, dict) or not all(
            isinstance(v.get(f), str) for f in _FIELDS
        ):
            continue
        if not _is_uuid(v["session_id"]) or v["session_id"] in seen:
            continue
        seen.add(v["session_id"])
        out[k] = {f: v[f] for f in _FIELDS}
    return out
