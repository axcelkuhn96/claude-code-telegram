"""Gera o segredo TOTP, grava TOTP_SECRET no env (0600) e imprime só a URI otpauth://.

Uso (chamado por scripts/setup-totp.sh): python scripts/totp_setup.py <caminho-do-env>
Não imprime as outras linhas do env.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import totp  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("uso: totp_setup.py <caminho-do-env>", file=sys.stderr)
        return 2
    secret = totp.generate_secret()
    totp.set_env_secret(Path(argv[1]), secret)
    print(totp.otpauth_uri(secret))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
