from __future__ import annotations

import socket

from bot.notify import sd_notify


def test_noop_sem_socket(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert sd_notify("READY=1") is False


def test_envia_para_socket_normal(tmp_path, monkeypatch):
    path = str(tmp_path / "notify.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    srv.bind(path)
    srv.settimeout(2)
    monkeypatch.setenv("NOTIFY_SOCKET", path)
    assert sd_notify("READY=1") is True
    assert srv.recv(100) == b"READY=1"
    srv.close()


def test_envia_para_socket_abstract(monkeypatch):
    name = "claude-telegram-test-notify"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    srv.bind("\0" + name)
    srv.settimeout(2)
    monkeypatch.setenv("NOTIFY_SOCKET", "@" + name)
    assert sd_notify("WATCHDOG=1") is True
    assert srv.recv(100) == b"WATCHDOG=1"
    srv.close()


def test_socket_inexistente_nao_levanta(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTIFY_SOCKET", str(tmp_path / "nao-existe.sock"))
    assert sd_notify("READY=1") is False
