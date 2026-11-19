"""Quebra texto longo em partes <= limit (Telegram: 4096), preservando cercas ```."""

from __future__ import annotations

import re

MIN_LIMIT = 16
# abertura/fechamento de cerca CommonMark (``` ou ~~~, 3+); recuo livre (itens de lista)
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")


def _units(s: str) -> int:
    """Tamanho em UTF-16 code units — como o Telegram conta o limite de 4096."""
    return len(s.encode("utf-16-le")) // 2


def _take(s: str, room: int) -> int:
    """Quantos chars de s cabem em `room` unidades UTF-16 (sem partir par substituto)."""
    used = 0
    for i, ch in enumerate(s):
        w = 2 if ord(ch) > 0xFFFF else 1
        if used + w > room:
            return i
        used += w
    return len(s)


def _fence_marker(line: str) -> tuple[str, str] | None:
    m = _FENCE_RE.match(line.rstrip("\n"))
    if m is None:
        return None
    marker, rest = m.groups()
    if marker[0] == "`" and "`" in rest:  # ```x``` numa linha não é cerca
        return None
    return marker, rest


def split(text: str, limit: int = 4096) -> list[str]:
    """Quebra em fim de linha; linha > limit sofre quebra dura.

    `limit` é em UTF-16 code units (emoji fora do BMP = 2). Se uma cerca (``` ou
    ~~~) é partida entre duas partes, a primeira é fechada e a seguinte reabre
    com a mesma linha de abertura (ex.: ```python). Texto vazio -> [].
    """
    if limit < MIN_LIMIT:
        raise ValueError(f"limit deve ser >= {MIN_LIMIT}")
    chunks: list[str] = []
    cur = ""
    pre = 0  # tamanho (chars) do prefixo de reabertura dentro de cur
    opener: str | None = (
        None  # linha de abertura da cerca aberta (estado ao fim de cur)
    )
    marker = ""  # marcador da cerca aberta

    def close() -> str:
        return "\n" + marker if opener is not None else ""

    def flush() -> None:
        nonlocal cur, pre
        chunks.append(cur.rstrip("\n") + close())
        cur = opener + "\n" if opener is not None else ""
        pre = len(cur)

    for line in text.splitlines(keepends=True):
        is_whole_line = True
        while line:
            new_opener, new_marker = opener, marker
            if is_whole_line:
                fence = _fence_marker(line)
                if fence is not None:
                    if opener is None:
                        new_opener, new_marker = line.strip(), fence[0]
                    elif (
                        fence[0][0] == marker[0]
                        and len(fence[0]) >= len(marker)
                        and not fence[1].strip()
                    ):
                        new_opener, new_marker = None, ""
            reserve = len(new_marker) + 1 if new_opener is not None else 0
            if _units(cur) + _units(line) + reserve <= limit:
                cur += line
                opener, marker = new_opener, new_marker
                break
            if cur[pre:].strip():
                flush()
                continue
            # cur vazio (ou só prefixo): a própria linha excede -> quebra dura
            room = limit - _units(cur) - len(close())
            if room < 2:  # prefixo de reabertura não cabe: segue sem ele
                cur, pre = "", 0
                room = limit - len(close())
            n = _take(line, room)
            cur += line[:n]
            flush()
            line = line[n:]
            is_whole_line = False
    if cur[pre:].strip():
        chunks.append(cur.rstrip("\n"))
    return chunks
