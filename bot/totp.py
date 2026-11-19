"""TOTP (RFC 6238, SHA-1, 6 dígitos, 30 s) só com a stdlib, e utilidades do setup."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import re
import secrets
import struct
from pathlib import Path
from urllib.parse import quote

STEP_S = 30
DIGITS = 6
WINDOW = 1  # aceita o passo atual e ±1
ENV_KEY = "TOTP_SECRET"


def generate_secret() -> str:
    """160 bits em base32 (32 caracteres, sem padding)."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret: str) -> bytes:
    s = re.sub(r"[\s-]", "", secret).upper()
    return base64.b32decode(s + "=" * (-len(s) % 8))


def valid_secret(secret: object) -> bool:
    """Base32 decodificável e com pelo menos 80 bits."""
    if not isinstance(secret, str):
        return False
    try:
        return len(_key(secret)) >= 10
    except (binascii.Error, ValueError):
        return False


def hotp(key: bytes, counter: int, digits: int = DIGITS) -> str:
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    value = struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**digits).zfill(digits)


def step_of(unix_time: float) -> int:
    return int(unix_time // STEP_S)


def code_at(secret: str, unix_time: float, digits: int = DIGITS) -> str:
    return hotp(_key(secret), step_of(unix_time), digits)


def verify(
    secret: str,
    code: object,
    unix_time: float,
    last_step: int | None = None,
    window: int = WINDOW,
) -> int | None:
    """Passo (step) do código aceito, ou None. Aceita o passo atual ±`window`; um código
    cujo passo já foi usado (`<= last_step`) é rejeitado (anti-reuso)."""
    if not isinstance(code, str):
        return None
    code = code.strip().replace(" ", "")
    if len(code) != DIGITS or not (code.isascii() and code.isdigit()):
        return None
    key = _key(secret)
    cur = step_of(unix_time)
    matched = None
    for step in range(cur - window, cur + window + 1):  # sem curto-circuito
        if hmac.compare_digest(hotp(key, step), code):
            matched = step
    if matched is None or (last_step is not None and matched <= last_step):
        return None
    return matched


def otpauth_uri(
    secret: str, issuer: str = "Claude Telegram", account: str = "bot"
) -> str:
    label = quote(f"{issuer}:{account}")
    return (
        f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}"
        f"&algorithm=SHA1&digits={DIGITS}&period={STEP_S}"
    )


def set_env_secret(env_path: Path, secret: str) -> None:
    """Grava/substitui `TOTP_SECRET=` no arquivo de env (0600, atômico), mantendo as outras linhas."""
    env_path = Path(env_path)
    try:
        lines = env_path.read_text().splitlines()
    except FileNotFoundError:
        lines = []
    keep = [ln for ln in lines if not re.match(rf"\s*(export\s+)?{ENV_KEY}\s*=", ln)]
    keep.append(f"{ENV_KEY}={secret}")
    env_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = env_path.with_name(env_path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(keep) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, env_path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def has_env_secret(env_path: Path) -> bool:
    try:
        text = Path(env_path).read_text()
    except OSError:
        return False
    return any(
        re.match(rf"\s*(export\s+)?{ENV_KEY}\s*=\s*\S", ln) for ln in text.splitlines()
    )
