"""Projetos (workspaces do Horizon), sessões do Claude Code e cópia entre contas.

Só LEITURA de ~/.claude*/ e ~/.horizon, exceto `copy_session_to` (spec 9b), que
copia um .jsonl para a conta de destino sem tocar em mais nada.
`Profile` (bot/usage.py) é usado por duck typing: só `.name` e `.config_dir`.

FATO (medido neste notebook): `~/.claude-work/projects`, `~/.claude-personal/projects`
e `~/.claude-client/projects` são symlinks para `~/.claude/projects` — todas as
contas compartilham as mesmas sessões. Por isso `list_recent` deduplica pelo
caminho resolvido e `copy_session_to` entre contas assim devolve "kept" sem copiar:
a troca de conta não precisa de cópia aqui.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import uuid
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TextIO

import yaml

log = logging.getLogger(__name__)

PROMPT_LEN = 50


def encode_cwd(cwd: str) -> str:
    """cwd -> nome da pasta em `<config_dir>/projects/`.

    Medido: `/` e `.` viram `-` (`.../.claude/worktrees/x` -> `...--claude-worktrees-x`;
    `/tmp/tmp.AbC` -> `-tmp-tmp-AbC`). `_` não aparece em nenhuma pasta real daqui;
    adotada a regra do Claude Code (tudo fora de [A-Za-z0-9] vira `-`) — inferida.
    shortcut: Claude Code trunca nomes > 200 chars com hash; não tratado.
    """
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def load_projects(horizon_config: Path, home: Path | None = None) -> list[str]:
    """cwds de `workspaces[]` (ordem do arquivo, únicos, `~` expandido)."""
    try:
        data = yaml.safe_load(Path(horizon_config).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        log.warning(
            "config do Horizon ilegível (%s): %s", horizon_config, type(e).__name__
        )
        return []
    workspaces = data.get("workspaces") if isinstance(data, dict) else None
    if not isinstance(workspaces, list):
        return []
    base = home if home is not None else Path.home()
    out: list[str] = []
    for ws in workspaces:
        cwd = ws.get("cwd") if isinstance(ws, dict) else None
        if not isinstance(cwd, str) or not cwd.strip():
            continue
        cwd = cwd.strip()
        if cwd == "~" or cwd.startswith("~/"):
            cwd = str(base / cwd[2:])
        if cwd not in out:
            out.append(cwd)
    return out


@dataclass
class SessionInfo:
    session_id: str
    # None quando várias contas compartilham a pasta e o índice do Horizon não diz qual.
    account: str | None
    config_dir: Path
    cwd: str
    mtime: datetime
    first_prompt: str
    # Último texto do usuário (limpo, ≤60); "" se não estiver nos últimos ~256 KB.
    last_prompt: str = ""
    # custom-title (nome dado pelo usuário) senão ai-title; limpo, ≤60; "" se não achar.
    title: str = ""

    @property
    def last_activity(self) -> datetime:
        """Última atividade = mtime do .jsonl."""
        return self.mtime


def _is_uuid(s: str) -> bool:
    try:
        return str(uuid.UUID(s)) == s
    except ValueError:
        return False


def _texts(content: object) -> Iterator[str]:
    if isinstance(content, str):
        yield content
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    yield text


_SYSTEM_REMINDER = re.compile(
    r"<system-reminder\b[^>]*>.*?(?:</system-reminder>|\Z)", re.DOTALL
)
_TEAMMATE = re.compile(
    r"<teammate-message\b[^>]*>(.*?)(?:</teammate-message>|\Z)", re.DOTALL
)
_PASTED = re.compile(r"<pasted_content\b[^>]*>.*?(?:</pasted_content>|\Z)", re.DOTALL)
_CMD_MESSAGE = re.compile(r"<command-message>.*?</command-message>", re.DOTALL)
_CMD_NAME = re.compile(r"<command-name>(.*?)</command-name>", re.DOTALL)
_CMD_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)
_IMAGE = re.compile(r"\[Image(?: #\d+|:[^\]]*)\]")
_RELAY_LEAD = re.compile(r"^\s*Another Claude session sent a message:", re.IGNORECASE)
CLEAN_INPUT_MAX = 200_000  # shortcut: texto além disso é ignorado ao limpar
TAIL_STAGES = (256 * 1024, 1024 * 1024, 4 * 1024 * 1024, 8 * 1024 * 1024)  # cumulativo


def clean_prompt(text: str) -> str:
    """Tira embrulhos/ruído de um texto de usuário p/ exibir em listas.

    `<system-reminder>` some; `<teammate-message …>x</…>` vira `x` (e o prefixo
    "Another Claude session sent a message:" cai); `<pasted_content …>` ->
    "[colado]"; `<command-name>/x</command-name>` -> "/x" (+ args); `[Image #N]` e
    `[Image: source…]` -> "🖼"; espaços colapsados. Qualquer outro texto que
    comece com `<` (task-notification, local-command-*) é ruído -> "".
    """
    if not isinstance(text, str):
        return ""
    text = text[:CLEAN_INPUT_MAX]
    text = _SYSTEM_REMINDER.sub(" ", text)
    text = _TEAMMATE.sub(lambda m: f" {m.group(1)} ", text)
    text = _PASTED.sub(" [colado] ", text)
    text = _CMD_MESSAGE.sub(" ", text)
    text = _CMD_NAME.sub(lambda m: f" {m.group(1)} ", text)
    text = _CMD_ARGS.sub(lambda m: f" {m.group(1)} ", text)
    text = _IMAGE.sub(" 🖼 ", text)
    text = " ".join(text.split())
    text = _RELAY_LEAD.sub("", text).strip()
    return "" if text.startswith("<") else text


def _prompt_of(d: object) -> str:
    """Texto limpo do prompt de um registro do .jsonl; "" se não for prompt de usuário."""
    if not isinstance(d, dict) or d.get("type") != "user":
        return ""
    if d.get("isMeta") or d.get("isSidechain") or d.get("isCompactSummary"):
        return ""
    msg = d.get("message")
    parts = [
        clean_prompt(t)
        for t in _texts(msg.get("content") if isinstance(msg, dict) else None)
    ]
    parts = [p for p in parts if p]
    real = [p for p in parts if p != "🖼"]
    if real:
        return " ".join(real)
    return parts[0] if parts else ""


def first_prompt(path: Path) -> str:
    """1ª mensagem de usuário com texto real, limpa (~50 chars); "" se não achar."""
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for line in _bounded_lines(f):
                try:
                    text = _prompt_of(json.loads(line))
                except ValueError:
                    continue
                if text:
                    return text if len(text) <= PROMPT_LEN else text[:PROMPT_LEN] + "…"
    except OSError:
        return ""
    return ""


TITLE_LEN = 60
TAIL_CHUNK = 256 * 1024


def _title_of(value: object) -> str:
    """Título limpo (espaços colapsados, ≤60); "" se não for string útil."""
    if not isinstance(value, str):
        return ""
    return _clip(" ".join(value[:CLEAN_INPUT_MAX].split()), TITLE_LEN)


def tail_meta(path: Path, limit: int = 60) -> tuple[str, str]:
    """(título, último prompt) numa única leitura de trás pra frente do .jsonl.

    Título = último `custom-title` (nome dado pelo usuário), senão o último
    `ai-title`, senão "". Prompt = o registro mais recente entre uma linha
    `last-prompt` (barata; limpa por `clean_prompt`) e uma mensagem de usuário.

    Janelas cumulativas de TAIL_STAGES (256 KB, 1 MB, 4 MB, 8 MB): cada estágio lê
    só o trecho novo e carrega a linha cortada na fronteira pro próximo, então a
    memória fica limitada ao maior bloco. Para quando já tem prompt E título.
    Arquivo inteiro coberto -> o que achou. Teto atingido sem prompt (run enorme
    só de ferramentas) -> prompt cai no `first_prompt`; sem título -> "".
    shortcut: um `ai-title` mais novo que o prompt encerra a busca sem procurar
    um `custom-title` anterior a ele (o Claude Code reescreve os títulos no fim).
    """
    title = prompt = ""
    try:
        with Path(path).open("rb") as f:
            size = f.seek(0, os.SEEK_END)
            pos, carry = size, b""
            ai_seen = False
            for bound in _tail_bounds():
                start = max(0, size - bound)
                if start >= pos:
                    continue
                f.seek(start)
                block = f.read(pos - start)
                pos = start
                parts = (block + carry).split(b"\n")
                # com start > 0 a 1ª linha pode estar cortada: vira carry do próximo estágio
                carry, lines = (parts[0], parts[1:]) if start > 0 else (b"", parts)
                for line in reversed(lines):
                    d = _loads(line)
                    if not isinstance(d, dict):
                        continue
                    kind = d.get("type")
                    if kind == "custom-title":
                        t = _title_of(d.get("customTitle"))
                        if t:
                            title, ai_seen = t, True
                    elif kind == "ai-title":
                        if not title:
                            title = _title_of(d.get("aiTitle"))
                            ai_seen = ai_seen or bool(title)
                    elif not prompt:
                        if kind == "last-prompt":
                            text = clean_prompt(d.get("lastPrompt"))
                        else:
                            text = _prompt_of(d)
                        if text:
                            prompt = _clip(text, limit)
                    if prompt and ai_seen:
                        return title, prompt
                if start == 0:
                    return title, prompt
    except OSError:
        return title, prompt
    return title, prompt or _clip(first_prompt(path), limit)


def _tail_bounds() -> Iterator[int]:
    """Tamanhos cumulativos de TAIL_STAGES, fatiados em passos ≤ TAIL_CHUNK
    (cada leitura carrega no máximo um passo: memória limitada)."""
    last = 0
    for bound in TAIL_STAGES:
        while last < bound:
            last = min(bound, last + TAIL_CHUNK)
            yield last


def _loads(line: bytes) -> object:
    try:
        return json.loads(line.decode("utf-8", errors="replace"))
    except ValueError:
        return None


def last_prompt(path: Path, limit: int = 60) -> str:
    """Último texto de usuário (limpo, ≤`limit`); ver `tail_meta`."""
    return tail_meta(path, limit)[1]


def _index_profiles(accounts_index: Path) -> dict[str, str]:
    try:
        data = json.loads(Path(accounts_index).read_text(encoding="utf-8"))
        sessions = data["sessions"]
        found = {
            k: v.get("profile") for k, v in sessions.items() if isinstance(v, dict)
        }
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}
    return {k: v for k, v in found.items() if isinstance(v, str)}


def list_recent(
    cwd: str, profiles: list, accounts_index: Path, limit: int = 8
) -> list[SessionInfo]:
    """Sessões mais recentes de `cwd` em todas as contas, dedup por session_id.

    Dedup pelo arquivo real (caminho resolvido): contas com `projects/` em symlink
    para a mesma pasta enxergam o mesmo `.jsonl` uma vez só. Entre arquivos
    distintos da mesma sessão vence o maior mtime; empate -> arquivo da conta do
    índice do Horizon, senão o da primeira conta na ordem dos perfis.
    Conta: dono único do arquivo; se vários o compartilham, a do índice do
    Horizon (`claude-session-accounts.json`), senão `None`.
    """
    enc = encode_cwd(cwd)
    preferred = _index_profiles(accounts_index)
    # session_id -> arquivo real -> [mtime, caminho p/ ler, perfis que enxergam]
    found: dict[str, dict[Path, list]] = {}
    for prof in profiles:
        d = Path(prof.config_dir) / "projects" / enc
        try:
            files = list(d.glob("*.jsonl"))
        except OSError:
            continue
        for p in files:
            if not _is_uuid(p.stem):
                continue
            try:
                real = p.resolve()
                mtime = real.stat().st_mtime
            except OSError:
                continue
            entry = found.setdefault(p.stem, {}).setdefault(real, [mtime, p, []])
            entry[2].append(prof)

    best: dict[str, list] = {}
    for sid, files_by_real in found.items():
        win = None
        win_key = None
        for entry in files_by_real.values():
            key = (entry[0], preferred.get(sid) in {pr.name for pr in entry[2]})
            if win_key is None or key > win_key:
                win, win_key = entry, key
        best[sid] = win

    ranked = sorted(best.items(), key=lambda kv: kv[1][0], reverse=True)[:limit]
    out: list[SessionInfo] = []
    for sid, (mtime, p, owners) in ranked:
        title, last = tail_meta(p)
        pref = preferred.get(sid)
        if len(owners) == 1:
            account, owner = owners[0].name, owners[0]
        else:
            account = pref
            owner = next((pr for pr in owners if pr.name == pref), owners[0])
        out.append(
            SessionInfo(
                session_id=sid,
                account=account,
                config_dir=Path(owner.config_dir),
                cwd=cwd,
                mtime=datetime.fromtimestamp(mtime, tz=UTC),
                first_prompt=first_prompt(p),
                last_prompt=last,
                title=title,
            )
        )
    return out


def copy_session_to(session_id: str, cwd: str, src_dir: Path, dst_dir: Path) -> str:
    """Copia `<src>/projects/<enc>/<id>.jsonl` p/ a conta destino (spec 9b).

    Retorna "copied" (destino ausente), "updated" (destino mais antigo) ou "kept"
    (destino igual/mais novo, ou origem e destino na MESMA pasta `projects/`
    — symlinks entre contas —, caso em que não há o que copiar).
    `dst_dir` precisa existir (é o config dir da conta). Nunca apaga/move a
    origem; escrita atômica (tmp + replace).
    """
    if not isinstance(session_id, str) or not _is_uuid(session_id):
        raise ValueError("session_id inválido")
    if not isinstance(cwd, str) or not cwd.strip():
        raise ValueError("cwd vazio")
    if not Path(dst_dir).is_dir():
        raise FileNotFoundError(f"conta de destino inexistente: {dst_dir}")
    enc = encode_cwd(cwd)
    name = f"{session_id}.jsonl"
    src_root = (Path(src_dir) / "projects").resolve()
    dst_root = (Path(dst_dir) / "projects").resolve()
    src = (src_root / enc / name).resolve()
    if not src.is_relative_to(src_root / enc):
        raise ValueError("caminho de origem fora de projects/")
    if not (dst_root / enc).resolve().is_relative_to(dst_root):
        raise ValueError("caminho de destino fora de projects/")
    if not src.is_file():
        raise FileNotFoundError(str(src))
    dst = dst_root / enc / name
    if src_root == dst_root:
        return "kept"
    if dst.exists():
        if dst.stat().st_mtime >= src.stat().st_mtime:
            return "kept"
        result = "updated"
    else:
        result = "copied"
    dst_root.mkdir(mode=0o700, exist_ok=True)
    dst.parent.mkdir(mode=0o700, exist_ok=True)
    tmp = dst.with_name(f".{name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)
    return result


# ---------- sessões abertas (registro vivo do Claude Code) ----------

REGISTRY_MAX = 64 * 1024


@dataclass
class OpenSession:
    pid: int
    session_id: str
    cwd: str
    account: str
    config_dir: Path
    started_at: datetime
    kind: str
    # custom-title/ai-title do .jsonl da sessão; "" se não achar.
    title: str = ""
    # mtime do .jsonl (None se o arquivo não existe).
    last_activity: datetime | None = None


def _proc_starttime(proc_root: Path, pid: int) -> str | None:
    """Campo 22 (starttime) de `<proc_root>/<pid>/stat`; None se ausente/ilegível."""
    try:
        raw = (Path(proc_root) / str(pid) / "stat").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return None
    # `comm` (campo 2) vem entre parênteses e pode ter espaços/parênteses: corta no último ")".
    rest = raw[raw.rfind(")") + 1 :].split() if ")" in raw else []
    return rest[19] if len(rest) > 19 else None  # rest[0] = campo 3 (state)


def _read_registry(path: Path) -> dict | None:
    try:
        with path.open("rb") as f:
            blob = f.read(REGISTRY_MAX + 1)
        if len(blob) > REGISTRY_MAX:
            return None
        data = json.loads(blob.decode("utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def list_open(profiles: list, proc_root: Path = Path("/proc")) -> list[OpenSession]:
    """Sessões do Claude Code vivas agora, da mais recente para a mais antiga.

    Lê `<config_dir>/sessions/<pid>.json` (só `*.json`; os `*.key` ao lado nunca
    são abertos). Aberta = `<proc_root>/<pid>` existe E o starttime do stat ==
    `procStart` do registro (protege contra reuso de pid). Não sinaliza nada.
    Conta = perfil cuja pasta guarda o json. Dedup por session_id: `startedAt`
    mais novo entre os vivos.
    """
    best: dict[str, OpenSession] = {}
    for prof in profiles:
        config_dir = Path(prof.config_dir)
        try:
            files = list((config_dir / "sessions").glob("*.json"))
        except OSError:
            continue
        for p in files:
            if not p.stem.isdigit():
                continue
            data = _read_registry(p)
            if data is None:
                continue
            pid, session_id, cwd = (
                data.get("pid"),
                data.get("sessionId"),
                data.get("cwd"),
            )
            if (
                not isinstance(pid, int)
                or isinstance(pid, bool)
                or pid != int(p.stem)
                or not isinstance(session_id, str)
                or not _is_uuid(session_id)
                or not isinstance(cwd, str)
            ):
                continue
            proc_start = data.get("procStart")
            if not isinstance(proc_start, (str, int)) or isinstance(proc_start, bool):
                continue
            if _proc_starttime(proc_root, pid) != str(proc_start):
                continue
            started = data.get("startedAt")
            try:
                if isinstance(started, bool) or not isinstance(started, (int, float)):
                    raise TypeError
                started_at = datetime.fromtimestamp(started / 1000, tz=UTC)
            except (TypeError, ValueError, OverflowError, OSError):
                try:
                    started_at = datetime.fromtimestamp(p.stat().st_mtime, tz=UTC)
                except OSError:
                    continue
            kind = data.get("kind")
            found = OpenSession(
                pid=pid,
                session_id=session_id,
                cwd=cwd,
                account=prof.name,
                config_dir=config_dir,
                started_at=started_at,
                kind=kind if isinstance(kind, str) else "",
            )
            cur = best.get(session_id)
            if cur is None or found.started_at > cur.started_at:
                best[session_id] = found
    for o in best.values():  # só os vencedores do dedup: 1 leitura de rabo cada
        jsonl = (
            Path(o.config_dir)
            / "projects"
            / encode_cwd(o.cwd)
            / f"{o.session_id}.jsonl"
        )
        try:
            o.last_activity = datetime.fromtimestamp(jsonl.stat().st_mtime, tz=UTC)
        except OSError:
            continue
        o.title = tail_meta(jsonl)[0]
    return sorted(best.values(), key=lambda o: o.started_at, reverse=True)


def open_ids(profiles: list, proc_root: Path = Path("/proc")) -> set[str]:
    """session_ids das sessões abertas (ver `list_open`)."""
    return {o.session_id for o in list_open(profiles, proc_root)}


# ---------- histórico de uma sessão (onda 4, C1) ----------

TURN_TEXT_LEN = 4000
TOOL_SUMMARY_LEN = 200
MAX_PAGE = 500
MAX_LINE = 8_000_000  # chars por linha do .jsonl; acima disso a linha é ignorada


@dataclass
class Turn:
    role: Literal["user", "assistant", "tool"]
    text: str
    tool: str | None = None
    ts: datetime | None = None


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _result_summary(content: object) -> str:
    """Resumo de um tool_result (str ou lista de blocos) em ≤200 chars."""
    if isinstance(content, str):
        raw = content
    elif isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                if isinstance(block.get("text"), str):
                    parts.append(block["text"])
            elif isinstance(block, dict) and block.get("type") == "image":
                parts.append("[imagem]")
        raw = " ".join(parts)
    else:
        raw = ""
    text = " ".join(raw.split())
    return _clip(text, TOOL_SUMMARY_LEN) if text else "[sem saída]"


def _bounded_lines(fh: TextIO) -> Iterator[str]:
    """Linhas do arquivo; as maiores que MAX_LINE são descartadas sem carregar.

    shortcut: uma linha gigante (tool_result de dezenas de MB, prompt colado
    enorme) some do histórico em vez de entrar inteira na memória; o descarte é
    determinístico, então os índices de turno seguem estáveis.
    """
    while True:
        line = fh.readline(MAX_LINE + 1)
        if not line:
            return
        if len(line) > MAX_LINE and not line.endswith("\n"):
            while True:  # consome o resto da linha em pedaços pequenos
                rest = fh.readline(65536)
                if not rest or rest.endswith("\n"):
                    break
            continue
        yield line


def _iter_turns(path: Path) -> Iterator[Turn]:
    """Turnos do .jsonl em ordem, lendo linha a linha (sessões grandes).

    Sem estado compartilhado entre chamadas (seguro para `asyncio.to_thread`).

    Cada linha de assistant traz UM bloco (text | thinking | tool_use); só `text`
    vira turno. tool_use só registra id -> nome; o turno "tool" nasce do tool_result.
    Ignora meta, sidechain, resumo de compactação, system/hook/attachment e
    mensagens que começam com `<` (comandos, system-reminder) ou `[Image`.
    """
    tool_names: dict[str, str] = {}
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in _bounded_lines(fh):
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(d, dict) or d.get("isSidechain"):
                    continue
                kind = d.get("type")
                if kind not in ("user", "assistant"):
                    continue
                if d.get("isMeta") or d.get("isCompactSummary"):
                    continue
                msg = d.get("message")
                content = msg.get("content") if isinstance(msg, dict) else None
                when = _parse_ts(d.get("timestamp"))
                if kind == "assistant":
                    if not isinstance(content, list):
                        continue
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")
                        if btype == "tool_use":
                            tid, name = block.get("id"), block.get("name")
                            if isinstance(tid, str) and isinstance(name, str):
                                tool_names[tid] = name
                        elif btype == "text" and isinstance(block.get("text"), str):
                            text = block["text"].strip()
                            if text:
                                yield Turn(
                                    "assistant", _clip(text, TURN_TEXT_LEN), None, when
                                )
                    continue
                # kind == "user"
                if isinstance(content, list):
                    for block in content:
                        if (
                            isinstance(block, dict)
                            and block.get("type") == "tool_result"
                        ):
                            tid = block.get("tool_use_id")
                            name = tool_names.get(tid) if isinstance(tid, str) else None
                            yield Turn(
                                "tool",
                                _result_summary(block.get("content")),
                                name,
                                when,
                            )
                texts = [
                    t
                    for raw in _texts(content)
                    if (t := raw.strip()) and not t.startswith(("<", "[Image"))
                ]
                if texts:
                    yield Turn(
                        "user", _clip("\n\n".join(texts), TURN_TEXT_LEN), None, when
                    )
    except OSError as e:
        log.warning("sessão ilegível (%s): %s", path.name, type(e).__name__)


def read_history(
    path: Path, limit: int = 50, before: int | None = None
) -> tuple[list[Turn], int | None]:
    """Página de turnos (ordem cronológica) e cursor da página anterior.

    `before=None` -> os `limit` últimos; `before=k` -> os `limit` turnos que
    antecedem o índice k (índice = posição do turno na sessão, estável com o
    arquivo crescendo). Cursor `None` = não há mais nada antes.
    Sem `before` o arquivo é percorrido inteiro (o índice depende disso); é E/S
    síncrona — chamar com `asyncio.to_thread` a partir do event loop.
    """
    limit = max(1, min(limit, MAX_PAGE))
    if before is not None and before <= 0:
        return [], None
    window: deque[Turn] = deque(maxlen=limit)
    end = 0
    for idx, turn in enumerate(_iter_turns(Path(path))):
        if before is not None and idx >= before:
            break
        window.append(turn)
        end = idx + 1
    start = end - len(window)
    return list(window), (start if start > 0 else None)


def last_exchange(path: Path) -> tuple[str | None, str | None]:
    """(último prompt do usuário, última resposta de texto do assistente)."""
    prompt = reply = None
    for turn in _iter_turns(Path(path)):
        if turn.role == "user":
            prompt = turn.text
        elif turn.role == "assistant":
            reply = turn.text
    return prompt, reply


def session_file(cwd: str, session_id: str, profiles: list) -> Path | None:
    """`.jsonl` mais recente da sessão entre as contas (caminho resolvido).

    None se o id não for UUID canônico, cwd vazio, não existir ou o arquivo
    resolver para fora de `<config_dir>/projects`.
    """
    if not isinstance(session_id, str) or not _is_uuid(session_id):
        return None
    if not isinstance(cwd, str) or not cwd.strip():
        return None
    enc = encode_cwd(cwd)
    best: tuple[float, Path] | None = None
    for prof in profiles:
        root = (Path(prof.config_dir) / "projects").resolve()
        try:
            real = (root / enc / f"{session_id}.jsonl").resolve()
            if not real.is_relative_to(root) or not real.is_file():
                continue
            mtime = real.stat().st_mtime
        except OSError:
            continue
        if best is None or mtime > best[0]:
            best = (mtime, real)
    return best[1] if best else None
