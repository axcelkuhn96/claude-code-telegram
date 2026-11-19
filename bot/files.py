"""Arquivos: o que o bot pode enviar (/send_file) e onde guarda o que recebe (inbox)."""

from __future__ import annotations

import os
import re
import stat
from datetime import datetime
from pathlib import Path

MAX_SEND_BYTES = 50 * 1024 * 1024  # limite do sendDocument
MAX_RECV_BYTES = 20 * 1024 * 1024  # limite de download da Bot API
CAPTION_MAX = 1024
# diretórios (relativos à home) que nunca saem do notebook
DENY_HOME_DIRS = (
    ".ssh",
    ".gnupg",
    ".cloudflared",
    ".aws",
    ".kube",
    ".docker",
    ".config/gh",
    ".config/claude-telegram",
)
DENY_HOME_FILES = (".netrc", ".git-credentials", ".npmrc", ".pypirc", ".pgpass")
# nomes proibidos em qualquer lugar sob a home
DENY_NAMES = (".credentials.json", ".claude.json")


class FileDenied(Exception):
    """Pedido de envio recusado; a mensagem é segura para devolver ao chamador."""


def _under(path: Path, base: Path) -> bool:
    return path == base or base in path.parents


def _denied(path: Path, home: Path, state_dir: Path) -> bool:
    if path.name in DENY_NAMES or path.name.startswith(".env"):
        return True
    if any(part.startswith(".env") for part in path.parts):  # **/.env*/...
        return True
    if path in {home / f for f in DENY_HOME_FILES}:
        return True
    bases = [home / d for d in DENY_HOME_DIRS] + [state_dir]
    if any(_under(path, b) for b in bases):
        return True
    try:  # transcritos das sessões: ~/.claude*/projects/**
        rel = path.relative_to(home).parts
    except ValueError:
        return False
    return len(rel) > 1 and rel[0].startswith(".claude") and rel[1] == "projects"


def check_sendable(raw: object, home: Path, state_dir: Path) -> Path:
    """Valida o caminho pedido por uma sessão: absoluto, dentro da home (após resolver links),
    arquivo regular ≤ 50 MB e fora da lista de segredos. Devolve o caminho resolvido."""
    if not isinstance(raw, str) or not raw.strip() or "\0" in raw:
        raise FileDenied("caminho inválido")
    if not raw.startswith("/"):
        raise FileDenied("o caminho precisa ser absoluto")
    home_r, state_r = home.resolve(), state_dir.resolve()
    given = Path(os.path.normpath(raw))
    try:
        real = Path(raw).resolve()
    except (OSError, RuntimeError):
        raise FileDenied("caminho inválido") from None
    if not _under(real, home_r):
        raise FileDenied("fora da home")
    # confere o caminho como foi pedido E o destino real (link para um segredo não passa)
    if _denied(real, home_r, state_r) or (
        _under(given, home) and _denied(given, home, state_dir)
    ):
        raise FileDenied("caminho não permitido")
    try:
        if not real.is_file():
            raise FileDenied("arquivo inexistente ou não é regular")
        if real.stat().st_size > MAX_SEND_BYTES:
            raise FileDenied("arquivo grande demais (máx. 50 MB)")
    except OSError:
        raise FileDenied("arquivo inacessível") from None
    return real


def read_sendable(raw: object, home: Path, state_dir: Path) -> tuple[bytes, str]:
    """Lê o arquivo a enviar SEM janela de corrida entre checar e ler: valida o caminho, abre com
    O_NOFOLLOW, e revalida no fd aberto (regular, ≤ 50 MB e o destino real de /proc/self/fd/N
    continua sob a home e fora da deny-list). Lê do fd. Devolve (conteúdo, nome)."""
    real = check_sendable(raw, home, state_dir)
    try:
        # O_NONBLOCK: um FIFO trocado na hora não pode travar o open (o fstat o recusa)
        fd = os.open(real, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError:
        raise FileDenied("arquivo inacessível") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise FileDenied("arquivo inexistente ou não é regular")
        if st.st_size > MAX_SEND_BYTES:
            raise FileDenied("arquivo grande demais (máx. 50 MB)")
        try:
            opened = Path(os.readlink(f"/proc/self/fd/{fd}"))
        except OSError:
            opened = real  # sem /proc: já validado acima
        home_r, state_r = home.resolve(), state_dir.resolve()
        if not _under(opened, home_r) or _denied(opened, home_r, state_r):
            raise FileDenied("caminho não permitido")
        with os.fdopen(fd, "rb", closefd=False) as f:
            data = f.read(MAX_SEND_BYTES + 1)
    except OSError:
        raise FileDenied("arquivo inacessível") from None
    finally:
        os.close(fd)
    if len(data) > MAX_SEND_BYTES:
        raise FileDenied("arquivo grande demais (máx. 50 MB)")
    return data, real.name


def sanitize_name(name: str | None) -> str:
    """Só o nome-base, [A-Za-z0-9._-], sem ponto inicial, ≤ 100 chars."""
    base = re.sub(
        r"[^A-Za-z0-9._-]", "_", os.path.basename((name or "").replace("\\", "/"))
    )
    base = base.lstrip(".")[-100:]
    return base or "arquivo"


def _mkdir_private(path: Path) -> None:
    path.mkdir(mode=0o700, exist_ok=True)
    if path.stat().st_mode & 0o077:
        path.chmod(0o700)


def save_inbox(home: Path, now: datetime, name: str | None, data: bytes) -> Path:
    """`~/.cache/claude-telegram/inbox/<AAAA-MM-DD>/<nome>`: dirs 0700, arquivo 0600; nome
    repetido ganha sufixo -1, -2…"""
    cache = home / ".cache"
    cache.mkdir(mode=0o700, exist_ok=True)
    folder = cache / "claude-telegram" / "inbox" / now.strftime("%Y-%m-%d")
    for part in (
        cache / "claude-telegram",
        cache / "claude-telegram" / "inbox",
        folder,
    ):
        _mkdir_private(part)
    safe = sanitize_name(name)
    stem, dot, ext = safe.rpartition(".")
    if not dot:
        stem, ext = safe, ""
    for n in range(1000):
        target = folder / (safe if n == 0 else f"{stem}-{n}{dot}{ext}")
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return target
    raise OSError("inbox cheio de nomes repetidos")
