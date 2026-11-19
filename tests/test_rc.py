"""Remote Control (bot/rc.py): nome de unit, confiança, start/status/stop com runner falso."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from bot import rc

FIXTURE = Path(__file__).parent / "fixtures" / "rc_journal.txt"
CFG = Path("/cfg/.claude-personal")


class FakeRunner:
    """Captura argv; `script` mapeia um prefixo de argv (tupla) -> (rc, stdout)."""

    def __init__(self, script=None):
        self.calls: list[list[str]] = []
        self.script = script or {}

    def __call__(self, argv, timeout=rc.CMD_TIMEOUT_S):
        self.calls.append(list(argv))
        for prefix, out in self.script.items():
            if tuple(argv[: len(prefix)]) == prefix:
                return out
        return (0, "")


# ---------- accounts / unit_name ----------


def test_accounts_default_lista_e_vazio():
    assert rc.accounts({}) == ("personal",)
    assert rc.accounts({"RC_ACCOUNTS": " personal , work ,"}) == (
        "personal",
        "work",
    )
    assert rc.accounts({"RC_ACCOUNTS": ""}) == ()


def test_unit_name_formato_e_estabilidade():
    u = rc.unit_name("personal", "/home/you/cbs-lab")
    assert re.fullmatch(r"claude-rc-personal-cbs-lab-[0-9a-f]{6}", u)
    assert u == rc.unit_name("personal", "/home/you/cbs-lab")
    assert u != rc.unit_name("personal", "/outro/cbs-lab")
    assert u != rc.unit_name("work", "/home/you/cbs-lab")


def test_unit_name_slug_limpo_e_curto():
    u = rc.unit_name("personal", "/x/Meu Projeto_Ç!! " + "a" * 80)
    assert re.fullmatch(r"claude-rc-personal-[a-z0-9-]+-[0-9a-f]{6}", u)
    slug = u[len("claude-rc-personal-") : -7]
    assert 0 < len(slug) <= 40
    assert not slug.startswith("-") and not slug.endswith("-") and "--" not in slug


def test_unit_name_raiz_tem_slug():
    assert re.fullmatch(
        r"claude-rc-personal-[a-z0-9-]+-[0-9a-f]{6}", rc.unit_name("personal", "/")
    )


# ---------- is_trusted ----------


def _write_cfg(tmp_path, projects):
    d = tmp_path / "cfg"
    d.mkdir(exist_ok=True)
    (d / ".claude.json").write_text(json.dumps({"projects": projects}))
    return d


def test_is_trusted_exato_e_ausente(tmp_path):
    d = _write_cfg(
        tmp_path,
        {
            "/p/ok": {"hasTrustDialogAccepted": True},
            "/p/no": {"hasTrustDialogAccepted": False},
            "/p/vazio": {},
        },
    )
    assert rc.is_trusted(d, "/p/ok") is True
    assert rc.is_trusted(d, "/p/no") is False
    assert rc.is_trusted(d, "/p/vazio") is False
    assert rc.is_trusted(d, "/p/ausente") is False


def test_is_trusted_herda_do_pai(tmp_path):
    d = _write_cfg(tmp_path, {"/p": {"hasTrustDialogAccepted": True}})
    assert rc.is_trusted(d, "/p/sub/dir") is True
    assert rc.is_trusted(d, "/q/sub") is False


def test_is_trusted_arquivo_ruim_ou_inexistente(tmp_path):
    assert rc.is_trusted(tmp_path / "nada", "/p") is False
    d = tmp_path / "cfg"
    d.mkdir()
    (d / ".claude.json").write_text("{não é json")
    assert rc.is_trusted(d, "/p") is False
    (d / ".claude.json").write_text("[1,2]")
    assert rc.is_trusted(d, "/p") is False


def test_is_trusted_nao_escreve(tmp_path):
    d = _write_cfg(tmp_path, {})
    before = (d / ".claude.json").read_bytes()
    rc.is_trusted(d, "/p")
    assert (d / ".claude.json").read_bytes() == before
    assert sorted(p.name for p in d.iterdir()) == [".claude.json"]


# ---------- start ----------


def _trusted_cfg(tmp_path, project):
    return _write_cfg(tmp_path, {project: {"hasTrustDialogAccepted": True}})


def test_start_argv_e_sem_segredo(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok-secreto-123")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secreto-456")
    monkeypatch.setenv("TOTP_SECRET", "totp-secreto-789")
    project = "/p/cbs-lab"
    cfg = _trusted_cfg(tmp_path, project)
    unit = rc.unit_name("personal", project)
    fr = FakeRunner({("systemctl", "--user", "is-active"): (3, "inactive\n")})
    res = rc.start(
        "personal",
        cfg,
        project,
        "/bin/claude",
        runner=fr,
        hostname="lap",
        path_env="/bin:/usr/bin",
    )
    assert res.status == "started" and res.unit == unit
    argv = next(c for c in fr.calls if c[0] == "systemd-run")
    assert argv[:2] == ["systemd-run", "--user"]
    assert (
        f"--unit={unit}" in argv
        or ["--unit", unit] == argv[argv.index("--unit") : argv.index("--unit") + 2]
    )
    for needed in (
        "--collect",
        f"--working-directory={project}",
        f"--setenv=CLAUDE_CONFIG_DIR={cfg}",
        "--setenv=PATH=/bin:/usr/bin",
    ):
        assert needed in argv
    assert (
        "Restart=on-failure" in argv
        and "RestartSec=10" in argv
        and "StandardInput=null" in argv
    )
    i = argv.index("/bin/claude")
    assert argv[i + 1 :] == [
        "remote-control",
        "--name",
        "lap-cbs-lab",
        "--permission-mode",
        "bypassPermissions",
    ]
    joined = " ".join(argv)
    for secret in (
        "tok-secreto-123",
        "sk-secreto-456",
        "totp-secreto-789",
        "TELEGRAM_BOT_TOKEN",
        "OPENAI_API_KEY",
        "TOTP_SECRET",
    ):
        assert secret not in joined


def test_start_idempotente_se_ativa(tmp_path):
    project = "/p/cbs-lab"
    fr = FakeRunner({("systemctl", "--user", "is-active"): (0, "active\n")})
    res = rc.start(
        "personal", _trusted_cfg(tmp_path, project), project, "/bin/claude", runner=fr
    )
    assert res.status == "already"
    assert not any(c[0] == "systemd-run" for c in fr.calls)


def test_start_nao_confiado_nao_lanca(tmp_path):
    fr = FakeRunner({("systemctl", "--user", "is-active"): (3, "inactive\n")})
    res = rc.start(
        "personal", _write_cfg(tmp_path, {}), "/p/x", "/bin/claude", runner=fr
    )
    assert res.status == "untrusted"
    assert not any(c[0] == "systemd-run" for c in fr.calls)


def test_start_erro_do_systemd_run(tmp_path):
    project = "/p/cbs-lab"
    fr = FakeRunner(
        {
            ("systemctl", "--user", "is-active"): (3, "inactive\n"),
            ("systemd-run",): (1, "Failed to connect to bus"),
        }
    )
    res = rc.start(
        "personal", _trusted_cfg(tmp_path, project), project, "/bin/claude", runner=fr
    )
    assert res.status == "error"


def test_start_corrida_unit_ja_existe_vira_already(tmp_path):
    project = "/p/cbs-lab"
    answers = iter([(3, "inactive\n"), (0, "active\n")])

    def runner(argv, timeout=rc.CMD_TIMEOUT_S):
        if argv[0] == "systemd-run":
            return (1, "Unit already exists")
        return next(answers)

    res = rc.start(
        "personal",
        _trusted_cfg(tmp_path, project),
        project,
        "/bin/claude",
        runner=runner,
    )
    assert res.status == "already"


# ---------- parsing / status ----------


def test_strip_ansi_e_extracao_do_fixture():
    raw = FIXTURE.read_text()
    assert "\x1b" in raw
    clean = rc.strip_ansi(raw)
    assert "\x1b" not in clean and "Connected" in clean
    info = rc.parse_journal(raw)
    assert info["env_url"] == "https://claude.ai/code?environment=env_0FAKEFAKEFAKE1"
    assert info["session_urls"] == [
        "https://claude.ai/code/session_01FAKEFAKEFAKE0001?from=cli",
        "https://claude.ai/code/session_01FAKEFAKEFAKE0002?from=cli",
    ]
    assert info["capacity"] == "2/32"  # a última linha vence


def test_parse_journal_vazio():
    assert rc.parse_journal("") == {
        "env_url": None,
        "session_urls": [],
        "capacity": None,
    }


def test_parse_journal_so_aceita_claude_ai():
    info = rc.parse_journal("x https://evil.example/code?environment=e1 y")
    assert info["env_url"] is None


def test_status_lista_units(tmp_path):
    unit = rc.unit_name("personal", "/p/cbs-lab")
    listing = json.dumps(
        [
            {
                "unit": f"{unit}.service",
                "load": "loaded",
                "active": "active",
                "sub": "running",
                "description": "claude-rc:personal:/p/cbs-lab",
            },
            {
                "unit": "claude-rc-personal-zzz-abcdef.service",
                "load": "loaded",
                "active": "activating",
                "sub": "auto-restart",
                "description": "claude-rc:personal:/p/zzz",
            },
            {
                "unit": "claude-rc-personal-sem-desc-123456.service",
                "load": "loaded",
                "active": "active",
                "sub": "running",
                "description": "algo",
            },
        ]
    )
    fr = FakeRunner(
        {
            ("systemctl", "--user", "list-units"): (0, listing),
            ("journalctl", "--user", "-u", f"{unit}.service"): (0, FIXTURE.read_text()),
        }
    )
    out = rc.status(runner=fr)
    by_unit = {s["unit"]: s for s in out}
    s = by_unit[f"{unit}.service"]
    assert (s["account"], s["project"], s["active"]) == ("personal", "/p/cbs-lab", True)
    assert s["env_url"].endswith("env_0FAKEFAKEFAKE1") and len(s["session_urls"]) == 2
    z = by_unit["claude-rc-personal-zzz-abcdef.service"]
    assert z["active"] is False
    assert by_unit["claude-rc-personal-sem-desc-123456.service"]["project"] is None


def test_status_falha_do_systemctl_vira_lista_vazia():
    assert rc.status(runner=FakeRunner({("systemctl",): (1, "boom")})) == []
    assert rc.status(runner=FakeRunner({("systemctl",): (0, "não é json")})) == []


def test_status_ignora_units_de_outros_nomes():
    listing = json.dumps(
        [
            {
                "unit": "ssh.service",
                "load": "loaded",
                "active": "active",
                "sub": "running",
                "description": "",
            }
        ]
    )
    assert rc.status(runner=FakeRunner({("systemctl",): (0, listing)})) == []


# ---------- stop ----------


def test_stop_allow_list():
    fr = FakeRunner()
    assert rc.stop("claude-rc-personal-cbs-lab-abc123", runner=fr) is True
    assert fr.calls == [
        ["systemctl", "--user", "stop", "claude-rc-personal-cbs-lab-abc123"]
    ]
    assert rc.stop("claude-rc-personal-cbs-lab-abc123.service", runner=fr) is True
    for bad in (
        "ssh.service",
        "claude-telegram",
        "claude-rc-x; rm -rf",
        "../claude-rc-x",
        "claude-rc-",
        "",
        "claude-rc-a b",
    ):
        fr2 = FakeRunner()
        assert rc.stop(bad, runner=fr2) is False
        assert fr2.calls == []


def test_stop_falha_do_systemctl():
    assert (
        rc.stop(
            "claude-rc-personal-x-abc123", runner=FakeRunner({("systemctl",): (1, "")})
        )
        is False
    )


def test_run_cmd_real_sem_shell_e_timeout(monkeypatch):
    monkeypatch.undo()  # desfaz o stub autouse do conftest
    code, out = rc.run_cmd(["echo", "oi"], 5)
    assert (code, out.strip()) == (0, "oi")
    code, _ = rc.run_cmd(["sleep", "5"], 0.2)
    assert code != 0
    code, _ = rc.run_cmd(["/nao/existe/xyz"], 1)
    assert code != 0


@pytest.mark.parametrize("project", ["/p/a b", "/p/ç"])
def test_start_caminho_estranho_e_um_unico_argumento(tmp_path, project):
    cfg = _trusted_cfg(tmp_path, project)
    fr = FakeRunner({("systemctl", "--user", "is-active"): (3, "inactive\n")})
    rc.start("personal", cfg, project, "/bin/claude", runner=fr)
    argv = next(c for c in fr.calls if c[0] == "systemd-run")
    assert f"--working-directory={project}" in argv
