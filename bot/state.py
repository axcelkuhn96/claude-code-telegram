from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from bot import quiet as quiet_mod
from bot import slots as slots_mod

log = logging.getLogger(__name__)


@dataclass
class State:
    owner_id: int | None = None
    project: str | None = None  # cwd do projeto ativo
    account: str | None = None  # nome do perfil ativo
    session_id: str | None = None
    unlocked_until: float | None = None  # epoch (s) do fim do destrave TOTP
    totp_last_step: int | None = None  # último passo TOTP aceito (anti-reuso)
    # atalhos fixados: {"1".."4": {"session_id", "project", "account"}} (ver bot/slots.py)
    slots: dict = field(default_factory=dict)
    # execuções em andamento / interrompidas (reinício, kill): [{id, session_id, project, account,
    # prompt, ts, posted}]. Gravadas no launch e removidas ao fim normal; o que sobra no próximo
    # start foi interrompido e vira "⏸ interrompido" com botão "🔁 Reenviar".
    interrupted: list = field(default_factory=list)
    # modo silêncio ("off" | "final" | "total", ver bot/quiet.py) e a fila do /resumo (últimos 50)
    quiet: str = quiet_mod.DEFAULT_MODE
    digest: list = field(default_factory=list)


def _num_or_none(v: object) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


MAX_INTERRUPTED = 20
MAX_PROMPT = 20000


def _valid_interrupted(v: object) -> list:
    """Só entradas bem formadas (o state.json é lido no start; nada de confiar no disco)."""
    if not isinstance(v, list):
        return []
    out = []
    for e in v:
        if not isinstance(e, dict) or not all(
            isinstance(e.get(k), str)
            for k in ("id", "session_id", "project", "account", "prompt")
        ):
            continue
        try:
            if str(uuid.UUID(e["session_id"])) != e["session_id"]:
                continue
        except ValueError:
            continue
        ts = e.get("ts")
        out.append(
            {
                "id": e["id"][:16],
                "session_id": e["session_id"],
                "project": e["project"],
                "account": e["account"],
                "prompt": e["prompt"][:MAX_PROMPT],
                "ts": float(ts)
                if isinstance(ts, (int, float)) and not isinstance(ts, bool)
                else 0.0,
                "posted": e.get("posted") is True,
            }
        )
    return out[-MAX_INTERRUPTED:]


def _str_or_none(v: object) -> str | None:
    return v if isinstance(v, str) else None


def load(path: Path) -> State:
    """Ausente ou corrompido -> State vazio; campo de tipo errado -> None."""
    try:
        data = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return State()
    except (OSError, ValueError):
        log.warning("state ilegível ou corrompido: %s", path)
        return State()
    if not isinstance(data, dict):
        log.warning("state corrompido (não é objeto): %s", path)
        return State()
    owner = data.get("owner_id")
    return State(
        owner_id=owner
        if isinstance(owner, int) and not isinstance(owner, bool)
        else None,
        project=_str_or_none(data.get("project")),
        account=_str_or_none(data.get("account")),
        session_id=_str_or_none(data.get("session_id")),
        unlocked_until=_num_or_none(data.get("unlocked_until")),
        totp_last_step=int(step)
        if (step := _num_or_none(data.get("totp_last_step"))) is not None
        and step == int(step)
        else None,
        slots=slots_mod.validate(data.get("slots")),
        interrupted=_valid_interrupted(data.get("interrupted")),
        quiet=quiet_mod.valid_mode(data.get("quiet")),
        digest=quiet_mod.valid_digest(data.get("digest")),
    )


def save(path: Path, st: State) -> None:
    """Gravação atômica (tmp + rename), modo 0600."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.stat().st_mode & 0o077:
        path.parent.chmod(0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(asdict(st), f)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
