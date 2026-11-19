from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass, field
from pathlib import Path

# fora do range efêmero do Linux (32768-60999) e longe de portas de dev comuns;
# manter igual a DEFAULT_PORT em mcp_server/telegram_mcp.py
DEFAULT_PORT = 28765

REQUIRED = {
    "TELEGRAM_BOT_TOKEN": "bot_token",
    "OPENAI_API_KEY": "openai_key",
    "ALLOWED_PHONE": "allowed_phone",
}


# chaves que o bot lê do ambiente; as que faltarem no processo vêm de <state_dir>/env
KNOWN_KEYS = (
    *REQUIRED,
    "TOTP_SECRET",
    "TOTP_UNLOCK_HOURS",
    "WEBAPP_URL",
    "WEBAPP_PORT",
    "BOT_LOCAL_PORT",
    "RC_ACCOUNTS",
)


@dataclass
class Config:
    bot_token: str = field(repr=False)
    openai_key: str = field(repr=False)
    allowed_phone: str
    home: Path
    state_dir: Path
    claude_bin: Path
    local_port: int = DEFAULT_PORT


def _read_env_file(path: Path) -> dict[str, str]:
    try:
        text = path.read_text()
    except OSError:
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[key] = value
    return out


def _port(raw: str | None) -> int:
    if not raw:
        return DEFAULT_PORT
    try:
        port = int(raw)
    except ValueError:
        raise ValueError("BOT_LOCAL_PORT inválida") from None
    if not 1 <= port <= 65535:
        raise ValueError("BOT_LOCAL_PORT inválida")
    return port


def _state_dir(env: Mapping[str, str], home: Path) -> Path:
    override = env.get("CLAUDE_TELEGRAM_STATE_DIR")
    return Path(override) if override else home / ".config" / "claude-telegram"


def apply_env_file(
    environ: MutableMapping[str, str] | None = None, home: Path | None = None
) -> list[str]:
    """Copia para o ambiente do processo as chaves conhecidas que só existem em <state_dir>/env
    (o processo vence). Sem isto, `python -m bot.main` manual rodaria sem TOTP_SECRET, WEBAPP_URL…
    (o systemd já injeta tudo via EnvironmentFile). Devolve os NOMES aplicados (nunca valores)."""
    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else Path(home)
    file_env = _read_env_file(_state_dir(environ, home) / "env")
    applied = []
    for key in KNOWN_KEYS:
        if key not in environ and file_env.get(key):
            environ[key] = file_env[key]
            applied.append(key)
    return applied


def load(env: Mapping[str, str] | None = None, home: Path | None = None) -> Config:
    """Env primeiro; o que faltar vem de <state_dir>/env. Erro cita só o NOME da var."""
    env = os.environ if env is None else env
    home = Path.home() if home is None else Path(home)
    state_dir = _state_dir(env, home)

    missing = [k for k in (*REQUIRED, "BOT_LOCAL_PORT") if not env.get(k)]
    file_env = _read_env_file(state_dir / "env") if missing else {}
    values = {k: env.get(k) or file_env.get(k) for k in REQUIRED}
    still_missing = [k for k, v in values.items() if not v]
    if still_missing:
        raise ValueError("variável obrigatória ausente: " + ", ".join(still_missing))

    return Config(
        bot_token=values["TELEGRAM_BOT_TOKEN"],
        openai_key=values["OPENAI_API_KEY"],
        allowed_phone=values["ALLOWED_PHONE"],
        home=home,
        state_dir=state_dir,
        claude_bin=home / ".local" / "bin" / "claude",
        local_port=_port(env.get("BOT_LOCAL_PORT") or file_env.get("BOT_LOCAL_PORT")),
    )
