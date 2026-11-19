"""Markdown do Claude -> HTML do Telegram (so tags permitidas) + split seguro (4096)."""

from __future__ import annotations

import html
import re

# placeholders \x00N\x01 protegem trechos ja renderizados (code, links, URLs nuas)
_PH_RE = re.compile(r"\x00(\d+)\x01")
_CODE_RE = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")
_LINK_RE = re.compile(
    r"\[((?:[^\[\]]|\[[^\[\]]*\])+)\]"
    r"\(\s*((?:[^\s()\x00\x01]|\([^\s()\x00\x01]*\))+)\s*\)"
)
_URL_RE = re.compile(r"https?://[^\s\x00\x01]+")
_EMPH_RE = re.compile(
    r"(?P<bi>\*\*\*(?=[^\s*])(?P<bi_t>.+?)(?<=[^\s*])\*\*\*)"
    r"|(?P<b1>\*\*(?=\S)(?P<b1_t>.+?)(?<=\S)\*\*)"
    r"|(?<!\w)(?P<b2>__(?=\S)(?P<b2_t>.+?)(?<=\S)__)(?!\w|\.\w)"
    r"|(?P<s>~~(?=\S)(?P<s_t>.+?)(?<=\S)~~)"
    r"|(?<![\w*])(?P<i1>\*(?=[^\s*])(?P<i1_t>.+?)(?<=[^\s*])\*)(?![\w*])"
    r"|(?<![\w_])(?P<i2>_(?=[^\s_])(?P<i2_t>.+?)(?<=[^\s_])_)(?![\w_]|\.\w)"
)
_TAG_OF = {"bi": "b", "b1": "b", "b2": "b", "s": "s", "i1": "i", "i2": "i"}
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$")
_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_QUOTE_RE = re.compile(r"^\s{0,3}>\s?(.*)$")
_LANG_RE = re.compile(r"[\w+#.-]{1,30}")
_SAFE_SCHEMES = ("http://", "https://")
_URL_TRAIL = "*_.,;:!?)]'\""


def _esc(s: str) -> str:
    return html.escape(s, quote=False)


def _emph(s: str) -> str:
    """Aplica negrito/italico/riscado em texto ja escapado; o miolo e tratado recursivamente."""

    def repl(m: re.Match[str]) -> str:
        key = next(k for k in _TAG_OF if m.group(k) is not None)
        inner = _emph(m.group(f"{key}_t"))
        if key == "bi":
            return f"<b><i>{inner}</i></b>"
        tag = _TAG_OF[key]
        return f"<{tag}>{inner}</{tag}>"

    return _EMPH_RE.sub(repl, s)


def _inline(text: str) -> str:
    holds: list[str] = []

    def hold(rendered: str) -> str:
        holds.append(rendered)
        return f"\x00{len(holds) - 1}\x01"

    def link(m: re.Match[str]) -> str:
        label, url = m.group(1), m.group(2)
        inner = _emph(_esc(label))
        if not url.lower().startswith(_SAFE_SCHEMES):
            return hold(inner)  # esquema inseguro: so o texto
        return hold(f'<a href="{html.escape(url, quote=True)}">{inner}</a>')

    def bare(m: re.Match[str]) -> str:
        url = m.group(0)
        core = url.rstrip(_URL_TRAIL)
        return hold(_esc(core)) + url[len(core) :]

    text = _CODE_RE.sub(lambda m: hold(f"<code>{_esc(m.group(2))}</code>"), text)
    text = _LINK_RE.sub(link, text)
    text = _URL_RE.sub(bare, text)
    out = _emph(_esc(text))

    def restore(s: str) -> str:
        return _PH_RE.sub(lambda m: restore(holds[int(m.group(1))]), s)

    return restore(out)


def _is_fence_close(line: str, marker: str) -> bool:
    s = line.strip()
    return len(s) >= len(marker) and set(s) == {marker[0]}


def md_to_tg_html(text: str) -> str:
    """Converte Markdown (estilo Claude) em HTML aceito pelo Bot API do Telegram."""
    lines = (
        text.replace("\r\n", "\n").replace("\x00", "").replace("\x01", "").split("\n")
    )
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        fence = _FENCE_RE.match(line)
        if fence and not (fence.group(1)[0] == "`" and "`" in fence.group(2)):
            marker, rest = fence.groups()
            words = rest.split()
            lang = words[0] if words and _LANG_RE.fullmatch(words[0]) else ""
            i += 1
            body: list[str] = []
            while i < len(lines) and not _is_fence_close(lines[i], marker):
                body.append(lines[i])
                i += 1
            i += 1  # consome a cerca de fechamento (ou o fim do texto)
            code = _esc("\n".join(body))
            if lang:
                out.append(f'<pre><code class="language-{lang}">{code}</code></pre>')
            else:
                out.append(f"<pre>{code}</pre>")
            continue
        if line.lstrip().startswith("|"):
            j = i
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                j += 1
            if j - i >= 2:
                out.append(f"<pre>{_esc(chr(10).join(lines[i:j]))}</pre>")
                i = j
                continue
        if _QUOTE_RE.match(line):
            quoted: list[str] = []
            while i < len(lines) and (q := _QUOTE_RE.match(lines[i])):
                quoted.append(_inline(q.group(1)))
                i += 1
            out.append(f"<blockquote>{chr(10).join(quoted)}</blockquote>")
            continue
        if h := _HEADING_RE.match(line):
            inner = _inline(h.group(1)).replace("<b>", "").replace("</b>", "")
            out.append(f"<b>{inner}</b>")
        elif b := _BULLET_RE.match(line):
            out.append(f"{b.group(1)}• {_inline(b.group(2))}")
        else:
            out.append(_inline(line))
        i += 1
    return "\n".join(out)


# ---------- split ----------

MIN_LIMIT = 64
_ATOM_RE = re.compile(r"<[^>]+>|&(?:#\d+|#x[0-9a-fA-F]+|\w+);|[\s\S]")
_TAG_NAME_RE = re.compile(r"</?([a-zA-Z]+)")


def _units(s: str) -> int:
    """Tamanho em UTF-16 code units — como o Telegram conta o limite de 4096."""
    return len(s.encode("utf-16-le")) // 2


def _closers(stack: list[tuple[str, str]]) -> str:
    return "".join(f"</{name}>" for name, _ in reversed(stack))


def _openers(stack: list[tuple[str, str]]) -> str:
    return "".join(opening for _, opening in stack)


def _advance(stack: list[tuple[str, str]], atom: str) -> list[tuple[str, str]]:
    if not atom.startswith("<"):
        return stack
    name = _TAG_NAME_RE.match(atom)
    if name is None:
        return stack
    if atom.startswith("</"):
        return stack[:-1]
    return [*stack, (name.group(1).lower(), atom)]


def split_html(html_text: str, limit: int = 4096) -> list[str]:
    """Parte HTML em pedacos <= limit (UTF-16, contando o HTML inteiro: conservador).

    Corta em fim de linha quando possivel; tags abertas sao fechadas no fim de cada parte
    e reabertas no comeco da proxima (inclusive <pre><code>).
    """
    if limit < MIN_LIMIT:
        raise ValueError(f"limit minimo e {MIN_LIMIT}")
    if not html_text:
        return []
    if _units(html_text) <= limit:
        return [html_text]

    atoms = _ATOM_RE.findall(html_text)
    parts: list[str] = []
    start = 0
    start_stack: list[tuple[str, str]] = []
    stack = start_stack
    size = _units(_openers(start_stack))
    last_nl: tuple[int, list[tuple[str, str]]] | None = (
        None  # (indice apos "\n", pilha ali)
    )
    i = 0
    while i < len(atoms):
        atom = atoms[i]
        new_stack = _advance(stack, atom)
        cost = size + _units(atom) + _units(_closers(new_stack))
        if cost > limit and i > start:
            if last_nl is not None:
                cut, cut_stack = last_nl
                body = "".join(atoms[start : cut - 1])  # descarta o "\n" do corte
            else:
                cut, cut_stack = i, stack
                body = "".join(atoms[start:cut])
            parts.append(_openers(start_stack) + body + _closers(cut_stack))
            start, start_stack, stack = cut, cut_stack, cut_stack
            size = _units(_openers(start_stack))
            last_nl = None
            i = cut
            continue
        size += _units(atom)
        stack = new_stack
        if atom == "\n" and i > start:
            last_nl = (i + 1, stack)
        i += 1
    parts.append(_openers(start_stack) + "".join(atoms[start:]) + _closers(stack))
    return parts
