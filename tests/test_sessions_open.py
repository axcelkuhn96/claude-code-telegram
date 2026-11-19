"""list_open/open_ids: sessões vivas pelo registro `<config_dir>/sessions/<pid>.json`.

/proc é falso (tmp_path); nada é sinalizado, nada de `.key` é lido.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bot import sessions
from bot.sessions import OpenSession, list_open, open_ids


@dataclass
class FakeProfile:
    name: str
    config_dir: Path


def sid() -> str:
    return str(uuid.uuid4())


@pytest.fixture(autouse=True)
def no_signals(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("nunca sinalizar processos")

    monkeypatch.setattr(os, "kill", boom)
    monkeypatch.setattr(os, "killpg", boom)


@pytest.fixture
def proc(tmp_path) -> Path:
    p = tmp_path / "proc"
    p.mkdir()
    return p


@pytest.fixture
def accounts(tmp_path):
    return [
        FakeProfile("claude", tmp_path / "a"),
        FakeProfile("work", tmp_path / "b"),
    ]


def fake_proc(proc_root: Path, pid: int, start: str, comm: str = "claude") -> None:
    d = proc_root / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    # campos 4..21 (18) + starttime (22); comm pode ter espaços/parênteses
    (d / "stat").write_text(
        f"{pid} ({comm}) S " + " ".join(["0"] * 18) + f" {start} 0 0\n"
    )


def register(profile, file_pid: int, session_id: str, **over) -> Path:
    data = {
        "pid": file_pid,
        "sessionId": session_id,
        "cwd": "/home/you/Projeto",
        "startedAt": 1795099980400,
        "procStart": "5000",
        "kind": "interactive",
        "version": "2.1.328",
    }
    data.update(over)
    d = profile.config_dir / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{file_pid}.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_sessao_aberta_quando_proc_existe_e_starttime_confere(accounts, proc):
    s = sid()
    register(accounts[0], 111, s)
    fake_proc(proc, 111, "5000")
    got = list_open(accounts, proc_root=proc)
    assert got == [
        OpenSession(
            pid=111,
            session_id=s,
            cwd="/home/you/Projeto",
            account="claude",
            config_dir=accounts[0].config_dir,
            started_at=datetime.fromtimestamp(1795099980.4, tz=UTC),
            kind="interactive",
        )
    ]
    assert open_ids(accounts, proc_root=proc) == {s}


def test_comm_com_espacos_e_parenteses_nao_quebra_o_stat(accounts, proc):
    s = sid()
    register(accounts[0], 111, s)
    fake_proc(proc, 111, "5000", comm="my (weird) proc")
    assert open_ids(accounts, proc_root=proc) == {s}


def test_procstart_inteiro_no_json_tambem_confere(accounts, proc):
    s = sid()
    register(accounts[0], 111, s, procStart=5000)
    fake_proc(proc, 111, "5000")
    assert open_ids(accounts, proc_root=proc) == {s}


def test_pid_reutilizado_starttime_diferente_nao_e_aberta(accounts, proc):
    register(accounts[0], 111, sid())
    fake_proc(proc, 111, "9999")
    assert list_open(accounts, proc_root=proc) == []


def test_processo_inexistente_nao_e_aberta(accounts, proc):
    register(accounts[0], 111, sid())
    assert list_open(accounts, proc_root=proc) == []


@pytest.mark.parametrize("content", ["", "lixo sem parenteses", "1 (x) S 0 0", "1 (x)"])
def test_stat_malformado_nao_e_aberta(accounts, proc, content):
    register(accounts[0], 111, sid())
    d = proc / "111"
    d.mkdir()
    (d / "stat").write_text(content)
    assert list_open(accounts, proc_root=proc) == []


def test_json_malformado_ou_invalido_e_ignorado(accounts, proc):
    d = accounts[0].config_dir / "sessions"
    d.mkdir(parents=True)
    (d / "1.json").write_text("{nao e json")
    (d / "2.json").write_text("[1, 2]")
    (d / "3.json").write_bytes(b"\xff\xfe\x00")
    (d / "4.json").write_text("")
    register(accounts[0], 5, "nao-e-uuid")
    register(accounts[0], 6, sid().upper())
    register(accounts[0], 7, sid(), procStart=None)
    register(accounts[0], 8, sid(), cwd=None)
    register(accounts[0], 9, sid(), pid="nove")
    register(accounts[0], 10, sid(), pid=99)  # pid do JSON != nome do arquivo
    ok = sid()
    register(accounts[0], 11, ok)
    for pid in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 99):
        fake_proc(proc, pid, "5000")
    assert open_ids(accounts, proc_root=proc) == {ok}


def test_arquivos_key_e_nomes_nao_numericos_nunca_sao_lidos(
    accounts, proc, monkeypatch
):
    s_key = sid()
    d = accounts[0].config_dir / "sessions"
    d.mkdir(parents=True)
    payload = json.dumps(
        {"pid": 5, "sessionId": s_key, "cwd": "/x", "startedAt": 1, "procStart": "5000"}
    )
    key = d / ("5." + "a" * 64 + ".key")
    key.write_text(payload)
    (d / "abc.json").write_text(payload)
    (d / "5.json.bak").write_text(payload)
    fake_proc(proc, 5, "5000")

    opened: list[str] = []
    real_open = Path.open
    real_read_text = Path.read_text

    def spy_open(self, *a, **k):
        opened.append(self.name)
        return real_open(self, *a, **k)

    def spy_read_text(self, *a, **k):
        opened.append(self.name)
        return real_read_text(self, *a, **k)

    monkeypatch.setattr(Path, "open", spy_open)
    monkeypatch.setattr(Path, "read_text", spy_read_text)
    assert list_open(accounts, proc_root=proc) == []
    assert not any(n.endswith(".key") for n in opened)


def test_conta_e_a_dona_do_json_e_dedup_pelo_mais_recente(accounts, proc):
    s = sid()
    register(accounts[0], 111, s, startedAt=1000, cwd="/velho")
    register(accounts[1], 222, s, startedAt=2000, cwd="/novo")
    fake_proc(proc, 111, "5000")
    fake_proc(proc, 222, "5000")
    got = list_open(accounts, proc_root=proc)
    assert len(got) == 1
    assert got[0].pid == 222
    assert got[0].account == "work"
    assert got[0].config_dir == accounts[1].config_dir
    assert got[0].cwd == "/novo"


def test_dedup_ignora_a_mais_nova_se_o_processo_morreu(accounts, proc):
    s = sid()
    register(accounts[0], 111, s, startedAt=1000)
    register(accounts[1], 222, s, startedAt=2000)  # morta: sem /proc/222
    fake_proc(proc, 111, "5000")
    got = list_open(accounts, proc_root=proc)
    assert [g.pid for g in got] == [111]
    assert got[0].account == "claude"


def test_varias_sessoes_ordenadas_da_mais_recente_para_a_antiga(accounts, proc):
    ids = [sid() for _ in range(3)]
    for i, s in enumerate(ids):
        register(accounts[i % 2], 100 + i, s, startedAt=1000 * (i + 1))
        fake_proc(proc, 100 + i, "5000")
    assert [g.session_id for g in list_open(accounts, proc_root=proc)] == [
        ids[2],
        ids[1],
        ids[0],
    ]


def test_kind_ausente_vira_vazio_e_startedat_invalido_cai_no_mtime(accounts, proc):
    s = sid()
    p = register(accounts[0], 111, s, startedAt="ontem")
    data = json.loads(p.read_text())
    del data["kind"]
    p.write_text(json.dumps(data))
    os.utime(p, (1_700_000_000, 1_700_000_000))
    fake_proc(proc, 111, "5000")
    got = list_open(accounts, proc_root=proc)
    assert got[0].kind == ""
    assert got[0].started_at == datetime.fromtimestamp(1_700_000_000, tz=UTC)


def test_pasta_sessions_ausente_e_proc_root_padrao(accounts, tmp_path):
    assert list_open(accounts, proc_root=tmp_path / "nao-existe") == []
    assert list_open([FakeProfile("x", tmp_path / "nada")]) == []
    assert sessions.list_open.__defaults__ == (Path("/proc"),)


def test_json_gigante_e_ignorado(accounts, proc):
    d = accounts[0].config_dir / "sessions"
    d.mkdir(parents=True)
    (d / "7.json").write_text(
        json.dumps(
            {
                "pid": 7,
                "sessionId": sid(),
                "cwd": "/x",
                "procStart": "5000",
                "junk": "j" * 200_000,
            }
        )
    )
    fake_proc(proc, 7, "5000")
    assert list_open(accounts, proc_root=proc) == []
