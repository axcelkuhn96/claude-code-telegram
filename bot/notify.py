"""sd_notify mínimo (systemd Type=notify / watchdog) sem dependência."""

from __future__ import annotations

import logging
import os
import socket

log = logging.getLogger(__name__)


def sd_notify(msg: str) -> bool:
    """Envia `msg` ao NOTIFY_SOCKET (caminho ou abstract `@nome`). No-op sem socket."""
    path = os.environ.get("NOTIFY_SOCKET")
    if not path:
        return False
    if path.startswith("@"):
        path = "\0" + path[1:]
    try:
        with socket.socket(
            socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC
        ) as s:
            s.connect(path)
            s.sendall(msg.encode())
    except OSError as e:
        log.warning("sd_notify falhou (%s)", type(e).__name__)
        return False
    return True
