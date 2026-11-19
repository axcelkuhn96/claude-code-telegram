"""Fake do `claude` para testar o runner. Uso: python fake_claude.py <modo> [args...].

Modos: ok, fixture <arquivo>, fail, sleep, ignoreterm, big, echoenv <VAR>.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "ok"
    init = {"type": "system", "subtype": "init", "session_id": "fake-sid"}
    if mode == "ok":
        emit(init)
        emit(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}
                    ]
                },
            }
        )
        emit(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "OK",
                "session_id": "fake-sid",
                "total_cost_usd": 0.01,
                "duration_ms": 5,
            }
        )
        return 0
    if mode == "fixture":
        with open(sys.argv[2], encoding="utf-8") as f:
            sys.stdout.write(f.read())
        return 0
    if mode == "fail":
        for i in range(20):
            sys.stderr.write(f"erro linha {i}\n")
        sys.stderr.flush()
        return 3
    if mode == "sleep":
        emit(init)
        time.sleep(60)
        return 0
    if mode == "ignoreterm":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        emit(init)
        time.sleep(60)
        return 0
    if mode == "big":
        emit(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "x" * 200_000}]},
            }
        )
        emit({"type": "result", "is_error": False, "result": "fim"})
        return 0
    if mode == "bigsleep":  # linha enorme e depois fica vivo (leitor estoura o limite)
        emit(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "x" * 5000}]},
            }
        )
        time.sleep(60)
        return 0
    if mode == "grandchild":  # neto no mesmo grupo que ignora SIGTERM; emite o pid dele
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import signal,sys,time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "print('r', flush=True)\n"
                "time.sleep(60)\n",
            ],
            stdout=subprocess.PIPE,
        )
        child.stdout.readline()  # handler instalado
        emit({"type": "system", "subtype": "init", "session_id": str(child.pid)})
        time.sleep(60)
        return 0
    if mode == "echoenv":
        emit(
            {
                "type": "result",
                "is_error": False,
                "result": f"{os.environ.get(sys.argv[2], '')}|{os.getcwd()}",
            }
        )
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
