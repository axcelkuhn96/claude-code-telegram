from __future__ import annotations

from bot.power import (
    parse_busctl,
    parse_gsettings,
    parse_linger,
    read_ac_battery,
)


def test_parse_linger():
    assert parse_linger("Linger=no\n") == "não"
    assert parse_linger("Linger=yes\n") == "sim"
    assert parse_linger("lixo") == "?"
    assert parse_linger(None) == "?"


def test_parse_gsettings():
    assert parse_gsettings("'nothing'\n") == "nothing"
    assert parse_gsettings("'suspend'") == "suspend"
    assert parse_gsettings("") == "?"
    assert parse_gsettings(None) == "?"


def test_parse_busctl():
    assert parse_busctl('s "suspend-then-hibernate"\n') == "suspend-then-hibernate"
    assert parse_busctl('s "ignore"') == "ignore"
    assert parse_busctl("b true") == "?"
    assert parse_busctl(None) == "?"


def test_read_ac_battery(tmp_path):
    (tmp_path / "ACAD").mkdir()
    (tmp_path / "ACAD" / "online").write_text("1\n")
    (tmp_path / "BAT1").mkdir()
    (tmp_path / "BAT1" / "capacity").write_text("87\n")
    assert read_ac_battery(tmp_path) == "AC ligado · bateria 87%"


def test_read_ac_battery_ausente(tmp_path):
    assert read_ac_battery(tmp_path) == "AC ? · bateria ?"


def test_read_ac_desligado(tmp_path):
    (tmp_path / "ACAD").mkdir()
    (tmp_path / "ACAD" / "online").write_text("0\n")
    assert read_ac_battery(tmp_path).startswith("AC desligado")
