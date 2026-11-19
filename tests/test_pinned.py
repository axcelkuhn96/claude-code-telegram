from __future__ import annotations

import asyncio
import json
import os
import stat
from datetime import UTC, datetime
from types import SimpleNamespace

from telegram.error import BadRequest, NetworkError, RetryAfter

from bot.pinned import PinnedStatus, placeholder, status_line
from bot.usage import AccountUsage, Window

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _usage(five=None, seven=None, name="work"):
    return AccountUsage(
        name=name,
        display_name=name,
        email=None,
        five_hour=Window(five, None),
        seven_day=Window(seven, None),
        severity="normal",
        state="ok",
        fetched_at=NOW,
    )


# --- status_line / placeholder -------------------------------------------


def test_status_line_completa():
    s = status_line("project-a", "Refatorar autenticação", "work", _usage(9, 4))
    assert s == "📍 project-a · Refatorar autenticação · work (91% livre)"


def test_status_line_usa_o_maior_entre_5h_e_7d():
    s = status_line("p", None, "work", _usage(10, 60))
    assert "(40% livre)" in s


def test_status_line_ignora_janela_nula():
    assert "(70% livre)" in status_line("p", None, "c", _usage(None, 30))
    assert "(80% livre)" in status_line("p", None, "c", _usage(20, None))


def test_status_line_uso_desconhecido_omite_parenteses():
    assert status_line("p", None, "c", None) == "📍 p · c"
    assert status_line("p", None, "c", _usage(None, None)) == "📍 p · c"


def test_status_line_clamp():
    assert "(0% livre)" in status_line("p", None, "c", _usage(130, 5))
    assert "(100% livre)" in status_line("p", None, "c", _usage(-5, 0))


def test_status_line_working_e_locked():
    s = status_line("p", None, "c", None, working=2, locked=True)
    assert s.startswith("🔒")
    assert s.endswith("⏳ 2")
    assert "⏳" not in status_line("p", None, "c", None, working=0)
    assert "🔒" not in status_line("p", None, "c", None)


def test_status_line_partes_ausentes():
    assert status_line(None, "t", None, None) == "📍 t"
    assert status_line("p", None, None, None) == "📍 p"
    assert status_line(None, None, None, None)  # nunca vazio


def test_status_line_titulo_truncado_em_40():
    s = status_line("p", "x" * 100, "c", None)
    titulo = s.split(" · ")[1]
    assert len(titulo) == 40 and titulo.endswith("…")


def test_status_line_total_ate_120():
    s = status_line("p" * 200, "t", "c" * 200, _usage(1, 1), working=3, locked=True)
    assert len(s) <= 120
    assert s.endswith("⏳ 3")


def test_placeholder_compacto():
    assert (
        placeholder("project-a", "API", "work", _usage(9, 4)) == "project-a · work · 91%"
    )


def test_placeholder_limite_64_e_vazio():
    assert (
        len(placeholder("p" * 100, "t", "c", _usage(1, 1), working=2, locked=True))
        <= 64
    )
    assert placeholder(None, None, None, None)  # nunca vazio


def test_placeholder_working_locked():
    p = placeholder("p", None, "c", None, working=2, locked=True)
    assert p.startswith("🔒") and "⏳2" in p


# --- PinnedStatus ----------------------------------------------------------


class FakeBot:
    def __init__(self):
        self.calls: list[tuple] = []
        self.edit_errors: list[Exception] = []
        self.send_errors: list[Exception] = []
        self.pin_errors: list[Exception] = []
        self._next_id = 100

    async def send_message(self, chat_id, text, **kw):
        self.calls.append(("send", chat_id, text, kw))
        if self.send_errors:
            raise self.send_errors.pop(0)
        self._next_id += 1
        return SimpleNamespace(message_id=self._next_id)

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kw):
        self.calls.append(("edit", chat_id, message_id, text))
        if self.edit_errors:
            raise self.edit_errors.pop(0)
        return True

    async def pin_chat_message(self, chat_id, message_id, **kw):
        self.calls.append(("pin", chat_id, message_id, kw))
        if self.pin_errors:
            raise self.pin_errors.pop(0)
        return True

    def kinds(self):
        return [c[0] for c in self.calls]


def _ps(tmp_path, bot=None, interval=0.05):
    bot = bot or FakeBot()
    return bot, PinnedStatus(bot, 42, tmp_path / "pinned.json", min_interval=interval)


async def test_primeiro_update_envia_mudo_e_fixa(tmp_path):
    bot, ps = _ps(tmp_path)
    await ps.update("A")
    assert bot.kinds() == ["send", "pin"]
    assert bot.calls[0][1:3] == (42, "A")
    assert bot.calls[0][3]["disable_notification"] is True
    assert bot.calls[1][1:3] == (42, 101)
    assert bot.calls[1][3]["disable_notification"] is True
    await ps.close()


async def test_persiste_0600_atomico(tmp_path):
    _bot, ps = _ps(tmp_path)
    await ps.update("A")
    p = tmp_path / "pinned.json"
    assert json.loads(p.read_text()) == {"chat_id": 42, "message_id": 101, "text": "A"}
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert not list(tmp_path.glob("*.tmp"))
    await ps.close()


async def test_texto_igual_nao_chama_api(tmp_path):
    bot, ps = _ps(tmp_path)
    await ps.update("A")
    n = len(bot.calls)
    await ps.update("A")
    assert len(bot.calls) == n
    await ps.close()


async def test_update_edita_mensagem_existente(tmp_path):
    bot, ps = _ps(tmp_path, interval=0)
    await ps.update("A")
    await ps.update("B")
    assert bot.calls[-1] == ("edit", 42, 101, "B")
    assert bot.kinds().count("send") == 1
    await ps.close()


async def test_retoma_do_store_sem_reenviar(tmp_path):
    _bot, ps = _ps(tmp_path, interval=0)
    await ps.update("A")
    await ps.close()
    bot2, ps2 = _ps(tmp_path, interval=0)
    await ps2.update("A")
    assert bot2.calls == []
    await ps2.update("B")
    assert bot2.calls == [("edit", 42, 101, "B")]
    await ps2.close()


async def test_store_de_outro_chat_e_ignorado(tmp_path):
    (tmp_path / "pinned.json").write_text(
        json.dumps({"chat_id": 7, "message_id": 5, "text": "A"})
    )
    bot, ps = _ps(tmp_path)
    await ps.update("A")
    assert bot.kinds() == ["send", "pin"]
    await ps.close()


async def test_store_corrompido_e_ignorado(tmp_path):
    (tmp_path / "pinned.json").write_text("{nao json")
    bot, ps = _ps(tmp_path)
    await ps.update("A")
    assert bot.kinds() == ["send", "pin"]
    await ps.close()


async def test_edit_not_found_reenvia_e_fixa(tmp_path):
    bot, ps = _ps(tmp_path, interval=0)
    await ps.update("A")
    bot.edit_errors.append(BadRequest("Message to edit not found"))
    await ps.update("B")
    assert bot.kinds() == ["send", "pin", "edit", "send", "pin"]
    assert bot.calls[3][2] == "B"
    assert json.loads((tmp_path / "pinned.json").read_text())["message_id"] == 102
    await ps.close()


async def test_not_modified_silencioso(tmp_path):
    bot, ps = _ps(tmp_path, interval=0)
    await ps.update("A")
    bot.edit_errors.append(BadRequest("Message is not modified: exactly the same"))
    await ps.update("B")  # não levanta
    assert bot.kinds().count("send") == 1
    n = len(bot.calls)
    await ps.update("B")  # texto já registrado como atual
    assert len(bot.calls) == n
    await ps.close()


async def test_pin_falho_nao_levanta_e_mensagem_fica(tmp_path):
    bot, ps = _ps(tmp_path)
    bot.pin_errors.append(BadRequest("not enough rights"))
    await ps.update("A")
    assert json.loads((tmp_path / "pinned.json").read_text())["message_id"] == 101
    await ps.close()


async def test_send_falho_nunca_levanta_e_tenta_depois(tmp_path):
    bot, ps = _ps(tmp_path)
    bot.send_errors.append(NetworkError("boom"))
    await ps.update("A")  # não levanta
    assert not (tmp_path / "pinned.json").exists()
    await ps.update("A")  # tenta de novo (nada foi enviado ainda)
    assert bot.kinds().count("send") == 2
    await ps.close()


async def test_edits_com_rate_limit_e_coalescem_ultimo_texto(tmp_path):
    bot, ps = _ps(tmp_path, interval=0.2)
    await ps.update("A")
    await asyncio.sleep(0.25)
    await ps.update("B")  # janela livre
    await ps.update("C")  # dentro da janela: pendente
    await ps.update("D")  # sobrescreve o pendente
    edits = [c for c in bot.calls if c[0] == "edit"]
    assert [e[3] for e in edits] == ["B"]
    await asyncio.sleep(0.4)
    edits = [c for c in bot.calls if c[0] == "edit"]
    assert [e[3] for e in edits] == ["B", "D"]
    assert json.loads((tmp_path / "pinned.json").read_text())["text"] == "D"
    await ps.close()


async def test_pendente_igual_ao_atual_nao_edita(tmp_path):
    bot, ps = _ps(tmp_path, interval=0.2)
    await ps.update("A")
    await asyncio.sleep(0.25)
    await ps.update("B")
    await ps.update("C")  # pendente
    await ps.update("B")  # volta ao atual: descarta pendente
    await asyncio.sleep(0.4)
    assert [c[3] for c in bot.calls if c[0] == "edit"] == ["B"]
    await ps.close()


async def test_close_cancela_flush_pendente(tmp_path):
    bot, ps = _ps(tmp_path, interval=0.2)
    await ps.update("A")
    await asyncio.sleep(0.25)
    await ps.update("B")
    await ps.update("C")
    await ps.close()
    await asyncio.sleep(0.4)
    assert [c[3] for c in bot.calls if c[0] == "edit"] == ["B"]


async def test_retry_after_honrado_sem_bloquear_e_reaplica(tmp_path):
    bot, ps = _ps(tmp_path, interval=0)
    await ps.update("A")
    bot.edit_errors.append(RetryAfter(0.1))
    await ps.update("B")  # retorna já
    await asyncio.sleep(0.4)
    edits = [c for c in bot.calls if c[0] == "edit"]
    assert [e[3] for e in edits] == ["B", "B"]
    assert json.loads((tmp_path / "pinned.json").read_text())["text"] == "B"
    await ps.close()


async def test_erro_inesperado_nao_propaga(tmp_path):
    bot, ps = _ps(tmp_path, interval=0)
    await ps.update("A")
    bot.edit_errors.append(RuntimeError("segredo-123456:ABCDEFGHIJKLMNOPQRSTUVWX"))
    await ps.update("B")  # não levanta
    await ps.close()
