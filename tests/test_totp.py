from __future__ import annotations

import base64
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from bot import totp

# RFC 6238 apêndice B (SHA-1): segredo ASCII "12345678901234567890", códigos de 8 dígitos
RFC_SECRET = base64.b32encode(b"12345678901234567890").decode()
RFC_VECTORS = [
    (59, "94287082"),
    (1111111109, "07081804"),
    (1111111111, "14050471"),
    (1234567890, "89005924"),
    (2000000000, "69279037"),
    (20000000000, "65353130"),
]


@pytest.mark.parametrize("t,expected", RFC_VECTORS)
def test_rfc6238_vetores_8_digitos(t, expected):
    assert totp.code_at(RFC_SECRET, t, digits=8) == expected


@pytest.mark.parametrize("t,expected", RFC_VECTORS)
def test_rfc6238_vetores_6_digitos(t, expected):
    assert totp.code_at(RFC_SECRET, t) == expected[-6:]


def test_segredo_com_espacos_minusculas_e_sem_padding():
    spaced = " ".join(
        RFC_SECRET[i : i + 4] for i in range(0, len(RFC_SECRET), 4)
    ).lower()
    assert totp.code_at(spaced, 59) == totp.code_at(RFC_SECRET, 59)
    assert totp.code_at(RFC_SECRET.rstrip("="), 59) == totp.code_at(RFC_SECRET, 59)


def test_generate_secret_160_bits_e_valido():
    s = totp.generate_secret()
    assert len(s) == 32 and set(s) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")
    assert len(base64.b32decode(s)) == 20
    assert totp.generate_secret() != s
    assert totp.valid_secret(s)


@pytest.mark.parametrize("bad", ["", "abc", "1!@#", None, 5, "AAAA", "A" * 3])
def test_valid_secret_rejeita(bad):
    assert totp.valid_secret(bad) is False


def test_verify_janela_mais_menos_1_passo():
    t = 1_000_000_000.0
    step = totp.step_of(t)
    for delta in (-1, 0, 1):
        code = totp.code_at(RFC_SECRET, t + delta * 30)
        assert totp.verify(RFC_SECRET, code, t) == step + delta
    for delta in (-2, 2):
        assert (
            totp.verify(RFC_SECRET, totp.code_at(RFC_SECRET, t + delta * 30), t) is None
        )


def test_verify_rejeita_reuso_do_passo():
    t = 1_000_000_000.0
    code = totp.code_at(RFC_SECRET, t)
    step = totp.verify(RFC_SECRET, code, t)
    assert step is not None
    assert totp.verify(RFC_SECRET, code, t, last_step=step) is None  # mesmo código
    assert (
        totp.verify(RFC_SECRET, code, t + 20, last_step=step) is None
    )  # ainda na janela
    prev = totp.code_at(RFC_SECRET, t - 30)  # passo anterior ao já usado
    assert totp.verify(RFC_SECRET, prev, t, last_step=step) is None
    nxt = totp.code_at(RFC_SECRET, t + 30)  # passo seguinte: vale
    assert totp.verify(RFC_SECRET, nxt, t, last_step=step) == step + 1


@pytest.mark.parametrize(
    "bad",
    ["", "12345", "1234567", "abcdef", "12 345", "١٢٣٤٥٦", None, 123456, "12345\x00"],
)
def test_verify_rejeita_formato_invalido(bad):
    assert totp.verify(RFC_SECRET, bad, 59) is None


def test_verify_aceita_espacos_ao_redor():
    code = totp.code_at(RFC_SECRET, 59)
    assert totp.verify(RFC_SECRET, f" {code[:3]} {code[3:]} ", 59) is not None


def test_otpauth_uri():
    uri = totp.otpauth_uri("ABCDEFGH")
    assert uri.startswith("otpauth://totp/Claude%20Telegram%3Abot?secret=ABCDEFGH")
    for part in ("issuer=Claude%20Telegram", "algorithm=SHA1", "digits=6", "period=30"):
        assert part in uri


def test_set_env_secret_preserva_linhas_substitui_e_0600(tmp_path):
    env = tmp_path / "estado" / "env"
    totp.set_env_secret(env, "AAAA")  # cria (dir e arquivo)
    assert env.read_text() == "TOTP_SECRET=AAAA\n"
    env.write_text(
        "A=1\n# comentário\nTOTP_SECRET=velho\nexport TOTP_SECRET=outro\nB=2\n"
    )
    totp.set_env_secret(env, "NOVO")
    assert env.read_text().splitlines() == [
        "A=1",
        "# comentário",
        "B=2",
        "TOTP_SECRET=NOVO",
    ]
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert not list(env.parent.glob("*.tmp"))
    assert totp.has_env_secret(env) is True
    env.write_text("A=1\nTOTP_SECRET=\n")
    assert totp.has_env_secret(env) is False
    assert totp.has_env_secret(tmp_path / "nao-existe") is False


def test_script_totp_setup_grava_e_imprime_so_a_uri(tmp_path):
    env = tmp_path / "env"
    env.write_text("TELEGRAM_BOT_TOKEN=nao-imprimir-isto\nOPENAI_API_KEY=nem-isto\n")
    script = Path(__file__).resolve().parent.parent / "scripts" / "totp_setup.py"
    out = subprocess.run(  # noqa: S603 - script do próprio repo, args controlados
        [sys.executable, "-I", str(script), str(env)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.startswith("otpauth://totp/")
    assert (
        "nao-imprimir-isto" not in out.stdout + out.stderr
        and "nem-isto" not in out.stdout + out.stderr
    )
    lines = env.read_text().splitlines()
    assert lines[:2] == [
        "TELEGRAM_BOT_TOKEN=nao-imprimir-isto",
        "OPENAI_API_KEY=nem-isto",
    ]
    secret = lines[2].split("=", 1)[1]
    assert totp.valid_secret(secret) and f"secret={secret}" in out.stdout
    assert stat.S_IMODE(env.stat().st_mode) == 0o600


def test_setup_totp_sh_recusa_dentro_do_claude_code(tmp_path):
    sh = Path(__file__).resolve().parent.parent / "scripts" / "setup-totp.sh"
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "CLAUDECODE": "1"}
    r = subprocess.run(  # noqa: S603 - script do próprio repo
        ["bash", str(sh)],  # noqa: S607
        capture_output=True,
        text=True,
        env=env,
    )
    assert r.returncode == 1 and "FORA do Claude Code" in r.stderr
    assert not (tmp_path / ".config").exists()  # nada foi escrito
