"""Energia/sono para o /status: só leitura (sysfs, loginctl, gsettings, busctl)."""

from __future__ import annotations

import asyncio
import getpass
import re
from pathlib import Path

SYSFS_POWER = Path("/sys/class/power_supply")
CMD_TIMEOUT_S = 3.0


def parse_linger(out: str | None) -> str:
    m = re.fullmatch(r"Linger=(yes|no)", (out or "").strip())
    return {"yes": "sim", "no": "não"}[m.group(1)] if m else "?"


def parse_gsettings(out: str | None) -> str:
    m = re.fullmatch(r"'([^']*)'", (out or "").strip())
    return m.group(1) if m else "?"


def parse_busctl(out: str | None) -> str:
    m = re.fullmatch(r's "([^"]*)"', (out or "").strip())
    return m.group(1) if m else "?"


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def read_power_raw(base: Path = SYSFS_POWER) -> tuple[bool | None, int | None]:
    """(AC ligado?, % da bateria); None onde não der para ler. Para a API do Mini App."""
    ac = {"1": True, "0": False}.get(_read(base / "ACAD" / "online") or "")
    cap = _read(base / "BAT1" / "capacity")
    return ac, int(cap) if cap and cap.isdigit() else None


def read_ac_battery(base: Path = SYSFS_POWER) -> str:
    ac = {"1": "ligado", "0": "desligado"}.get(
        _read(base / "ACAD" / "online") or "", "?"
    )
    cap = _read(base / "BAT1" / "capacity")
    bat = f"{cap}%" if cap and cap.isdigit() else "?"
    return f"AC {ac} · bateria {bat}"


async def _run(*argv: str) -> str | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), CMD_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return None
    return out.decode(errors="replace") if proc.returncode == 0 else None


async def power_status(base: Path = SYSFS_POWER) -> str:
    linger, sleep, lid = await asyncio.gather(
        _run("loginctl", "show-user", getpass.getuser(), "-p", "Linger"),
        _run(
            "gsettings",
            "get",
            "org.gnome.settings-daemon.plugins.power",
            "sleep-inactive-ac-type",
        ),
        _run(
            "busctl",
            "get-property",
            "org.freedesktop.login1",
            "/org/freedesktop/login1",
            "org.freedesktop.login1.Manager",
            "HandleLidSwitch",
        ),
    )
    return "\n".join(
        [
            read_ac_battery(base),
            f"Linger: {parse_linger(linger)}",
            f"Sono no AC (GNOME): {parse_gsettings(sleep)}",
            f"Tampa (HandleLidSwitch): {parse_busctl(lid)}",
        ]
    )
