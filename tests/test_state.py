import json
import logging
import os
import stat

from bot import state
from bot.state import State


def test_save_creates_state_dir_0700(tmp_path):
    p = tmp_path / "sd" / "state.json"
    state.save(p, State(owner_id=1))
    assert stat.S_IMODE(p.parent.stat().st_mode) == 0o700


def test_save_tightens_loose_existing_dir(tmp_path):
    d = tmp_path / "sd"
    d.mkdir()
    d.chmod(0o755)
    state.save(d / "state.json", State(owner_id=1))
    assert stat.S_IMODE(d.stat().st_mode) == 0o700


def test_save_fsyncs_before_replace(tmp_path, monkeypatch):
    calls = []
    real_fsync, real_replace = os.fsync, os.replace

    def fake_fsync(fd):
        calls.append("fsync")
        real_fsync(fd)

    def fake_replace(a, b):
        calls.append("replace")
        real_replace(a, b)

    monkeypatch.setattr(os, "fsync", fake_fsync)
    monkeypatch.setattr(os, "replace", fake_replace)
    state.save(tmp_path / "state.json", State(owner_id=1))
    assert calls == ["fsync", "replace"]


def test_load_corrupted_logs_warning(tmp_path, caplog):
    p = tmp_path / "state.json"
    p.write_text("{not json")
    with caplog.at_level(logging.WARNING, logger="bot.state"):
        state.load(p)
    assert any("state.json" in r.getMessage() for r in caplog.records)


def test_load_missing_does_not_warn(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="bot.state"):
        state.load(tmp_path / "state.json")
    assert caplog.records == []


def test_load_missing_returns_empty(tmp_path):
    assert state.load(tmp_path / "state.json") == State()


def test_load_corrupted_returns_empty(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{not json")
    assert state.load(p) == State()


def test_load_non_dict_returns_empty(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("[1, 2]")
    assert state.load(p) == State()


def test_load_invalid_types_become_none_valid_kept(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(
        json.dumps(
            {"owner_id": "42", "project": "/home/x/p", "account": 7, "session_id": None}
        )
    )
    st = state.load(p)
    assert st == State(
        owner_id=None, project="/home/x/p", account=None, session_id=None
    )


def test_load_bool_is_not_owner_id(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"owner_id": True}))
    assert state.load(p).owner_id is None


def test_roundtrip(tmp_path):
    p = tmp_path / "state.json"
    st = State(owner_id=42, project="/home/x/p", account="work", session_id="abc")
    state.save(p, st)
    assert state.load(p) == st


def test_save_mode_0600_and_no_tmp_left(tmp_path):
    p = tmp_path / "state.json"
    state.save(p, State(owner_id=1))
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert [f.name for f in tmp_path.iterdir()] == ["state.json"]


def test_save_creates_parent_dir(tmp_path):
    p = tmp_path / "a" / "b" / "state.json"
    state.save(p, State(owner_id=1))
    assert state.load(p).owner_id == 1


def test_save_overwrites_whole_content(tmp_path):
    p = tmp_path / "state.json"
    state.save(p, State(owner_id=1, project="/x", account="a", session_id="s"))
    state.save(p, State(owner_id=2))
    assert state.load(p) == State(owner_id=2)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
