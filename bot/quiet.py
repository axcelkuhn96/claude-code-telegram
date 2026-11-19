"""Modo silêncio (/silencio): política de entrega por tipo de mensagem e fila do /resumo.

Puro, sem Telegram nem E/S. O estado (`quiet`, `digest`) vive em bot/state.py.

Tipos de mensagem (`kind`):
- `urgent`  — precisa do dono (perguntas, diálogos do terminal, limite/troca de conta, erros,
              execução interrompida, TOTP/trava): sempre entregue, notificando.
- `final`   — resultado final de uma execução (com rodapé e "📖 Ver no painel").
- `notify`  — `telegram_notificar` vindo de uma sessão (MCP).
- `echo`    — progresso, ecos "🛰 (via claude.ai)", avisos intermediários.

Modos: `off` (como sempre), `final` (só finais + urgentes), `total` (só urgentes; finais e
notificações vão para a fila, que o /resumo entrega e limpa).
"""

from __future__ import annotations

MODES = ("off", "final", "total")
KINDS = ("urgent", "final", "notify", "echo")
DEFAULT_MODE = "off"

MAX_DIGEST = 50
MAX_ITEM_TEXT = 4000

# ações: "send" (entrega normal), "silent" (entrega sem notificar), "queue", "drop"
_MATRIX: dict[str, dict[str, str]] = {
    "off": {"urgent": "send", "final": "send", "notify": "send", "echo": "send"},
    "final": {"urgent": "send", "final": "send", "notify": "silent", "echo": "drop"},
    "total": {"urgent": "send", "final": "queue", "notify": "queue", "echo": "drop"},
}


def valid_mode(v: object) -> str:
    return v if isinstance(v, str) and v in MODES else DEFAULT_MODE


def policy(kind: str, mode: str) -> str:
    """Ação de entrega para `kind` no `mode`. Desconhecido -> trata como urgente/off (entrega)."""
    return _MATRIX.get(mode, _MATRIX[DEFAULT_MODE]).get(kind, "send")


def make_item(
    ts: float,
    project_label: str | None,
    title: str | None,
    kind: str,
    text: str,
    session_id: str | None = None,
) -> dict:
    text = text.strip()
    if len(text) > MAX_ITEM_TEXT:
        text = text[: MAX_ITEM_TEXT - 1] + "…"
    return {
        "ts": float(ts),
        "project_label": project_label or "",
        "title": title or "",
        "kind": kind if kind in KINDS else "final",
        "text": text,
        "session_id": session_id if isinstance(session_id, str) else "",
    }


def enqueue(digest: list, item: dict) -> list:
    """Nova fila com `item` no fim, limitada aos últimos MAX_DIGEST."""
    return (list(digest) + [item])[-MAX_DIGEST:]


def valid_digest(v: object) -> list:
    """Só itens bem formados (o state.json é lido no start; nada de confiar no disco)."""
    if not isinstance(v, list):
        return []
    out = []
    for e in v:
        if not isinstance(e, dict):
            continue
        text, ts = e.get("text"), e.get("ts")
        if not isinstance(text, str) or isinstance(ts, bool):
            continue
        if not isinstance(ts, (int, float)):
            continue
        out.append(
            make_item(
                ts,
                e["project_label"] if isinstance(e.get("project_label"), str) else "",
                e["title"] if isinstance(e.get("title"), str) else "",
                e["kind"] if isinstance(e.get("kind"), str) else "final",
                text,
                e["session_id"] if isinstance(e.get("session_id"), str) else "",
            )
        )
    return out[-MAX_DIGEST:]
