"""Onda 4 / W1: teclado fixo, arquivos, histórico, conta esgotada, rodapé, fachada do Mini App."""

from __future__ import annotations

import asyncio
import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot import files, handlers, main as main_mod
from bot.handlers import (
    KEYBOARD_ACTIONS,
    KEYBOARD_ROWS,
    fmt_cost,
    fmt_duration,
    format_turns,
    is_limit_error,
)
from bot.runfeed import FeedStore
from bot.sessions import SessionInfo
from bot.usage import Profile
from tests.test_handlers import (
    CTX,
    NOW,
    OWNER,
    SID,
    SID2,
    STRANGER,
    fake_claude_bin,
    make_bot,
    sent_texts,
    upd,
    usage_item,
)


@pytest.fixture(autouse=True)
def sem_backoff(monkeypatch):
    monkeypatch.setattr(handlers, "SEND_BACKOFF_S", (0.0, 0.0))


# ---------- files.check_sendable ----------


@pytest.fixture
def fs(tmp_path):
    home = tmp_path / "home"
    state = home / ".config" / "claude-telegram"
    state.mkdir(parents=True)
    (home / "docs").mkdir()
    (home / "docs" / "relatorio.pdf").write_bytes(b"%PDF")
    return home, state


def test_check_sendable_aceita_arquivo_da_home(fs):
    home, state = fs
    p = files.check_sendable(str(home / "docs" / "relatorio.pdf"), home, state)
    assert p == (home / "docs" / "relatorio.pdf").resolve()


@pytest.mark.parametrize(
    "rel",
    [
        ".env",
        ".env.local",
        "app/.env.production",
        ".ssh/id_ed25519",
        ".gnupg/pubring.kbx",
        ".cloudflared/cert.pem",
        ".aws/credentials",
        ".claude/.credentials.json",
        ".claude-work/.credentials.json",
        ".claude.json",
        ".claude-personal/.claude.json",
        ".config/claude-telegram/env",
        ".config/claude-telegram/local.secret",
        ".netrc",
    ],
)
def test_check_sendable_nega_segredos(fs, rel):
    home, state = fs
    target = home / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("segredo")
    with pytest.raises(files.FileDenied):
        files.check_sendable(str(target), home, state)


def test_check_sendable_nega_link_para_segredo_e_para_fora(fs, tmp_path):
    home, state = fs
    (state / "env").write_text("x")
    (home / "docs" / "link-env").symlink_to(state / "env")
    with pytest.raises(files.FileDenied):
        files.check_sendable(str(home / "docs" / "link-env"), home, state)
    outside = tmp_path / "fora.txt"
    outside.write_text("x")
    (home / "docs" / "link-fora").symlink_to(outside)
    with pytest.raises(files.FileDenied, match="home"):
        files.check_sendable(str(home / "docs" / "link-fora"), home, state)
    (home / ".ssh").mkdir()
    (home / ".ssh" / "k").write_text("x")
    (home / "docs" / "dir-ssh").symlink_to(home / ".ssh")
    with pytest.raises(files.FileDenied):
        files.check_sendable(str(home / "docs" / "dir-ssh" / "k"), home, state)


def test_check_sendable_nega_traversal_relativo_e_invalidos(fs):
    home, state = fs
    for bad in (
        "docs/relatorio.pdf",
        str(home / "docs" / ".." / ".." / "etc" / "passwd"),
        "/etc/passwd",
        "",
        "   ",
        "/x\0y",
        None,
        5,
        str(home / "docs"),  # diretório
        str(home / "docs" / "nao-existe.txt"),
    ):
        with pytest.raises(files.FileDenied):
            files.check_sendable(bad, home, state)


def test_check_sendable_limite_de_50mb(fs):
    home, state = fs
    big = home / "docs" / "grande.bin"
    with big.open("wb") as f:
        f.truncate(files.MAX_SEND_BYTES + 1)
    with pytest.raises(files.FileDenied, match="grande"):
        files.check_sendable(str(big), home, state)
    ok = home / "docs" / "limite.bin"
    with ok.open("wb") as f:
        f.truncate(files.MAX_SEND_BYTES)
    assert files.check_sendable(str(ok), home, state) == ok.resolve()


@pytest.mark.parametrize(
    "rel",
    [
        ".git-credentials",
        ".npmrc",
        ".pypirc",
        ".pgpass",
        ".config/gh/hosts.yml",
        ".docker/config.json",
        ".kube/config",
        ".claude/projects/-home-x/abc.jsonl",
        ".claude-work/projects/-home-x/abc.jsonl",
    ],
)
def test_check_sendable_nega_mais_segredos(fs, rel):
    home, state = fs
    target = home / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x")
    with pytest.raises(files.FileDenied):
        files.check_sendable(str(target), home, state)


def test_read_sendable_le_do_fd_e_devolve_nome(fs):
    home, state = fs
    data, name = files.read_sendable(str(home / "docs" / "relatorio.pdf"), home, state)
    assert (data, name) == (b"%PDF", "relatorio.pdf")


def test_read_sendable_toctou_link_trocado_no_ultimo_componente(fs, monkeypatch):
    """check_sendable devolve um caminho que, na hora do open, virou link para um segredo."""
    home, state = fs
    (state / "env").write_text("segredo")
    swapped = home / "docs" / "ok.txt"
    swapped.symlink_to(state / "env")
    monkeypatch.setattr(files, "check_sendable", lambda *a: swapped)
    with pytest.raises(files.FileDenied):
        files.read_sendable("/ignorado", home, state)


def test_read_sendable_toctou_diretorio_pai_trocado_por_link(fs, monkeypatch):
    """O pai foi trocado por um link para ~/.ssh depois da checagem: o destino real do fd é negado."""
    home, state = fs
    (home / ".ssh").mkdir()
    (home / ".ssh" / "id").write_text("PRIVATE")
    (home / "docs" / "dir").symlink_to(home / ".ssh")
    monkeypatch.setattr(
        files, "check_sendable", lambda *a: home / "docs" / "dir" / "id"
    )
    with pytest.raises(files.FileDenied, match="não permitido"):
        files.read_sendable("/ignorado", home, state)


def test_read_sendable_toctou_arquivo_cresceu_alem_do_limite(fs, monkeypatch):
    home, state = fs
    big = home / "docs" / "cresceu.bin"
    with big.open("wb") as f:
        f.truncate(files.MAX_SEND_BYTES + 5)
    monkeypatch.setattr(
        files, "check_sendable", lambda *a: big
    )  # checagem "antiga": cabia
    with pytest.raises(files.FileDenied, match="grande"):
        files.read_sendable("/ignorado", home, state)


def test_read_sendable_toctou_virou_diretorio_ou_fifo(fs, monkeypatch):
    home, state = fs
    monkeypatch.setattr(files, "check_sendable", lambda *a: home / "docs")
    with pytest.raises(files.FileDenied):
        files.read_sendable("/ignorado", home, state)
    fifo = home / "docs" / "pipe"
    os.mkfifo(fifo)
    monkeypatch.setattr(files, "check_sendable", lambda *a: fifo)
    with pytest.raises(
        files.FileDenied
    ):  # O_NOFOLLOW + fstat: não bloqueia lendo o fifo
        files.read_sendable("/ignorado", home, state)


def test_sanitize_name():
    assert files.sanitize_name("relatório final (1).pdf") == "relat_rio_final__1_.pdf"
    assert files.sanitize_name("../../etc/passwd") == "passwd"
    assert files.sanitize_name("..\\..\\x.txt") == "x.txt"
    assert files.sanitize_name(".bashrc") == "bashrc"
    assert (
        files.sanitize_name("") == "arquivo" and files.sanitize_name(None) == "arquivo"
    )
    assert len(files.sanitize_name("a" * 500 + ".txt")) <= 100


def test_save_inbox_permissoes_data_e_nomes_repetidos(tmp_path):
    home = tmp_path / "h"
    home.mkdir()
    now = datetime(2026, 11, 19, 12, 0)
    a = files.save_inbox(home, now, "nota.txt", b"1")
    b = files.save_inbox(home, now, "nota.txt", b"2")
    assert a.parent == home / ".cache/claude-telegram/inbox/2026-11-19"
    assert a.name == "nota.txt" and b.name == "nota-1.txt"
    assert a.read_bytes() == b"1" and b.read_bytes() == b"2"
    assert stat.S_IMODE(a.stat().st_mode) == 0o600
    for d in (a.parent, a.parent.parent, a.parent.parent.parent):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700
    evil = files.save_inbox(home, now, "../../../escape.txt", b"x")
    assert evil.parent == a.parent


# ---------- runfeed ----------


def test_runfeed_ring_300_after_seq_e_resultado():
    store = FeedStore()
    store.start("s")
    for i in range(350):
        store.add("s", "tool", tool="Bash", detail=str(i))
    snap = store.snapshot("s")
    assert snap["running"] is True and len(snap["events"]) == 300
    assert snap["events"][0]["seq"] == 51 and snap["events"][-1]["seq"] == 350
    assert [e["seq"] for e in store.snapshot("s", after_seq=348)["events"]] == [
        349,
        350,
    ]
    assert set(snap["events"][0]) == {"seq", "kind", "tool", "detail", "text", "ts"}
    store.finish(
        "s", {"text": "ok", "cost_usd": 1.0, "duration_ms": 5, "is_error": False}
    )
    done = store.snapshot("s")
    assert done["running"] is False and done["result"]["text"] == "ok"


def test_runfeed_expira_30min_apos_terminar_e_alias():
    t = [1000.0]
    store = FeedStore(clock=lambda: t[0])
    store.start("s")
    store.add("s", "text", text="x" * 9000)
    store.finish("s", None, alias="real")
    assert len(store.snapshot("s")["events"][0]["text"]) == 4000
    assert store.snapshot("real")["events"]  # alias aponta para o mesmo feed
    t[0] += 29 * 60
    assert store.snapshot("s")["events"]
    t[0] += 2 * 60
    assert store.snapshot("s") == {"running": False, "events": [], "result": None}
    assert store.snapshot("real")["events"] == []


def test_runfeed_sessao_desconhecida_e_add_sem_start():
    store = FeedStore()
    store.add("nada", "tool")  # não levanta
    assert store.snapshot("nada") == {"running": False, "events": [], "result": None}


# ---------- formatação ----------


def test_fmt_duration_e_cost():
    assert fmt_duration(4500) == "4.5s"
    assert fmt_duration(42_000) == "42s"
    assert fmt_duration(252_000) == "4m12s"
    assert fmt_duration(3_720_000) == "1h02m"
    assert fmt_cost(0.31) == "US$ 0,31"
    assert fmt_cost(12.345) == "US$ 12,35"
    assert fmt_cost(0.0042) == "US$ 0,0042"
    assert fmt_cost(0) == "US$ 0,00"


def test_is_limit_error_nova_regex():
    for t in (
        "You have exceeded your quota",
        "5-hour limit reached",
        "rate-limit hit",
        "ratelimit",
        "Usage limit",
        "Claude usage limit reached. Your limit will reset at 3pm",
        "5-hour limit reached ∙ resets 3pm",
        "You've hit your limit",
        "You've hit your weekly limit",
        "Credit balance is too low",
        "API Error: rate_limit_error",
        "Weekly limit reached",
    ):
        assert is_limit_error(t, True), t
    assert not is_limit_error("quota", False)
    assert not is_limit_error("could not generate", True)
    for t in (
        "Reached maximum number of turns (10)",
        "error_max_turns",
        "max turns reached",
        "context limit reached",
        "context window exceeded",
        "Prompt is too long",
        "The file reached its end",
        "Tool exceeded timeout",
    ):
        assert not is_limit_error(t, True), t


def test_format_turns():
    T = SimpleNamespace
    out = format_turns(
        [
            T(role="user", text="oi\ncomo vai", tool=None),
            T(role="assistant", text="x" * 700, tool=None),
            T(role="tool", text="saída " * 50, tool="Bash"),
        ]
    )
    blocks = out.split("\n\n")
    assert blocks[0] == "👤 oi\ncomo vai"
    assert blocks[1] == "🤖 " + "x" * 700  # resposta do assistente COMPLETA
    assert blocks[2].startswith("🔧 <i>Bash: ") and blocks[2].endswith("</i>")
    assert len(blocks[2]) <= 130  # ferramenta: uma linha curta


# ---------- teclado fixo ----------


def test_teclado_fixo_persistente_e_cobre_os_comandos(tmp_path):
    kb = make_bot(tmp_path).main_keyboard()
    assert kb.is_persistent is True and kb.resize_keyboard is True
    labels = [b.text for row in kb.keyboard for b in row]
    assert labels == [
        t for row in KEYBOARD_ROWS for t in row
    ]  # sem atalhos: sem 1ª linha extra
    assert len(labels) == 9 and set(labels) == set(KEYBOARD_ACTIONS)
    assert "🔒 Trancar" in labels and "📌 Fixar" in labels


async def test_botao_do_teclado_vira_comando_e_nao_prompt(tmp_path):
    bot = make_bot(tmp_path)
    bot.run_prompt = AsyncMock()
    for label, action in KEYBOARD_ACTIONS.items():
        setattr(bot, action, AsyncMock())
        await bot.on_text(upd(text=label), CTX)
        getattr(bot, action).assert_awaited_once()
    await asyncio.sleep(0.05)
    bot.run_prompt.assert_not_called()


async def test_menu_e_vinculo_mandam_o_teclado(tmp_path):
    bot = make_bot(tmp_path)
    await bot.cmd_menu(upd(), CTX)
    assert bot.tg.send_message.call_args.kwargs["reply_markup"].is_persistent is True
    novo = make_bot(tmp_path / "n", owner=None)
    contact = SimpleNamespace(user_id=STRANGER, phone_number="+55 (11) 99999-9999")
    await novo.on_contact(upd(user=STRANGER, contact=contact), CTX)
    assert novo.tg.send_message.call_args.kwargs["reply_markup"].is_persistent is True


async def test_menu_so_para_o_dono(tmp_path):
    bot = make_bot(tmp_path)
    await bot.cmd_menu(upd(user=STRANGER), CTX)
    await bot.cmd_historico(upd(user=STRANGER), CTX)
    await bot.on_file(upd(user=STRANGER), CTX)
    bot.tg.send_message.assert_not_called()


# ---------- resumo ao trocar de sessão ----------


def fake_history(monkeypatch, tmp_path, exchange=("faça x", "feito y"), turns=None):
    monkeypatch.setattr(
        handlers.sessions_mod,
        "session_file",
        lambda cwd, sid, profs: tmp_path / "s.jsonl",
        raising=False,
    )
    monkeypatch.setattr(
        handlers.sessions_mod, "last_exchange", lambda p: exchange, raising=False
    )
    calls = []

    def read_history(path, limit=50, before=None):
        calls.append((limit, before))
        return (turns or [], 7 if before is None and turns else None)

    monkeypatch.setattr(
        handlers.sessions_mod, "read_history", read_history, raising=False
    )
    return calls


async def test_pick_session_mostra_ultimo_prompt_e_resposta(tmp_path, monkeypatch):
    fake_history(monkeypatch, tmp_path, exchange=("p" * 500, "r" * 1200))
    bot = make_bot(tmp_path, project=str(tmp_path))
    bot.profiles = lambda: []
    bot.session_list = lambda: [
        SessionInfo(SID, None, tmp_path, str(tmp_path), NOW, "faça x")
    ]
    await bot.on_callback(upd(data=f"s:{SID}"), CTX)
    resumo = sent_texts(bot)[-1]
    lines = resumo.split("\n")
    assert lines[0].startswith("Você: ") and len(lines[0]) <= len("Você: ") + 300
    assert lines[1].startswith("Claude: ") and len(lines[1]) <= len("Claude: ") + 800
    assert lines[0].endswith("…") and lines[1].endswith("…")


async def test_troca_de_conta_com_sessao_mostra_resumo(tmp_path, monkeypatch):
    fake_history(monkeypatch, tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    c1, c2 = tmp_path / "c1", tmp_path / "c2"
    enc = handlers.encode_cwd(str(proj))
    (c1 / "projects" / enc).mkdir(parents=True)
    (c1 / "projects" / enc / f"{SID}.jsonl").write_text("{}\n")
    c2.mkdir()
    bot = make_bot(tmp_path, project=str(proj), account="c1", session_id=SID)
    bot.profiles = lambda: [Profile("c1", c1), Profile("c2", c2)]
    await bot.switch_account(OWNER, "c2")
    assert "🔀" in sent_texts(bot)[0]
    assert sent_texts(bot)[1] == "Você: faça x\nClaude: feito y"


async def test_sem_historico_disponivel_nao_manda_resumo(tmp_path, monkeypatch):
    fake_history(monkeypatch, tmp_path, exchange=(None, None))
    bot = make_bot(tmp_path, project=str(tmp_path))
    await bot.show_last_exchange(OWNER, SID)
    bot.tg.send_message.assert_not_called()


# ---------- /historico ----------


def turns(n):
    return [
        SimpleNamespace(
            role="user" if i % 2 == 0 else "assistant", text=f"m{i}", tool=None, ts=None
        )
        for i in range(n)
    ]


async def test_historico_padrao_6_e_botao_anteriores(tmp_path, monkeypatch):
    calls = fake_history(monkeypatch, tmp_path, turns=turns(6))
    bot = make_bot(tmp_path, project=str(tmp_path), session_id=SID)
    bot.profiles = lambda: []
    await bot.cmd_historico(upd(), CTX)
    assert calls == [(6, None)]
    sent = bot.tg.send_message.call_args
    assert "👤 m0" in sent.args[1] and "🤖 m1" in sent.args[1]
    btn = sent.kwargs["reply_markup"].inline_keyboard[0][0]
    assert btn.text == "⬅️ anteriores"
    assert btn.callback_data == f"h:{SID}:7:6" and len(btn.callback_data.encode()) <= 64
    await bot.on_callback(upd(data=btn.callback_data), CTX)
    assert calls[-1] == (6, 7)


async def test_historico_n_clampado_e_sem_botao_na_ultima_pagina(tmp_path, monkeypatch):
    calls = fake_history(monkeypatch, tmp_path, turns=turns(2))
    bot = make_bot(tmp_path, project=str(tmp_path), session_id=SID)
    bot.profiles = lambda: []
    await bot.cmd_historico(upd(), SimpleNamespace(args=["999"]))
    await bot.cmd_historico(upd(), SimpleNamespace(args=["0"]))
    await bot.cmd_historico(upd(), SimpleNamespace(args=["abc"]))
    assert [c[0] for c in calls] == [handlers.HISTORY_MAX_N, 1, 6]
    # cursor None (before dado) -> sem botão
    await bot.on_callback(upd(data=f"h:{SID}:7:6"), CTX)
    assert bot.tg.send_message.call_args.kwargs["reply_markup"] is None


async def test_historico_sem_sessao_ou_indisponivel(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, project=str(tmp_path))
    await bot.cmd_historico(upd(), CTX)
    assert "Sem sessão" in sent_texts(bot)[0]
    bot.state.session_id = SID
    monkeypatch.setattr(
        handlers.sessions_mod, "session_file", lambda *a: None, raising=False
    )
    await bot.cmd_historico(upd(), CTX)
    assert "indisponível" in sent_texts(bot)[-1]


async def test_historico_callback_invalido_ignorado(tmp_path, monkeypatch):
    calls = fake_history(monkeypatch, tmp_path, turns=turns(1))
    bot = make_bot(tmp_path, project=str(tmp_path))
    for data in ("h:nao-uuid:1:1", f"h:{SID}:x:1", f"h:{SID}:1", "h:"):
        await bot.on_callback(upd(data=data), CTX)
    assert calls == []


# ---------- conta esgotada e reenvio ----------


def limit_fixture(tmp_path, text="5-hour limit reached"):
    f = tmp_path / "limit.jsonl"
    f.write_text(
        json.dumps({"type": "system", "subtype": "init", "session_id": SID})
        + "\n"
        + json.dumps(
            {
                "type": "result",
                "is_error": True,
                "result": text,
                "session_id": SID,
                "total_cost_usd": 0.0,
                "duration_ms": 10,
            }
        )
        + "\n"
    )
    return f


async def test_conta_esgotada_oferece_contas_e_reenvia_o_mesmo_prompt(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    bot = make_bot(tmp_path, project=str(proj), account="c1", session_id=SID)
    bot.cfg.claude_bin = fake_claude_bin(tmp_path, f"fixture {limit_fixture(tmp_path)}")
    bot.profiles = lambda: [
        Profile("c1", tmp_path / "c1"),
        Profile("c2", tmp_path / "c2"),
    ]
    bot.usage = AsyncMock(return_value=[usage_item("c1", 100.0), usage_item("c2", 9.0)])
    await bot.run_prompt(OWNER, "faça aquilo")
    texts = sent_texts(bot)
    assert any("conta c1 esgotada" in t for t in texts)
    last = bot.tg.send_message.call_args
    btn = last.kwargs["reply_markup"].inline_keyboard[0][0]
    assert btn.text == "continuar na c2 · 9%"
    assert btn.callback_data.startswith("r:") and len(btn.callback_data.encode()) <= 64
    bot.run_prompt = AsyncMock()
    await bot.on_callback(upd(data=btn.callback_data), CTX)
    await asyncio.sleep(0.05)
    assert bot.state.account == "c2"
    bot.run_prompt.assert_awaited_once_with(OWNER, "faça aquilo")


async def test_reenvio_nao_roda_se_a_sessao_mudou_ou_token_expirou(tmp_path):
    bot = make_bot(tmp_path, project=str(tmp_path), account="c1", session_id=SID)
    bot.profiles = lambda: [
        Profile("c1", tmp_path / "c1"),
        Profile("c2", tmp_path / "c2"),
    ]
    bot.run_prompt = AsyncMock()
    await bot.on_callback(upd(data="r:deadbeef:c2"), CTX)
    assert "expirou" in sent_texts(bot)[-1]
    bot._retry["abcd1234"] = (SID2, str(tmp_path), "x")  # outra sessão
    await bot.on_callback(upd(data="r:abcd1234:c2"), CTX)
    assert "mudou" in sent_texts(bot)[-1]
    await asyncio.sleep(0.05)
    bot.run_prompt.assert_not_called()
    assert bot.state.account == "c1"


async def test_conta_esgotada_sem_outra_utilizavel(tmp_path):
    bot = make_bot(tmp_path)
    bot.usage = AsyncMock(
        return_value=[usage_item("c1", 100.0), usage_item("c2", 5.0, state="expired")]
    )
    await bot._suggest_accounts(OWNER, "c1", retry=(SID, "/p", "x"))
    assert "não há outra conta utilizável" in sent_texts(bot)[0]


# ---------- rodapé, aviso de término e feed ----------


async def test_fim_longo_ou_sessao_nao_ativa_manda_aviso_separado(
    tmp_path, monkeypatch
):
    proj = tmp_path / "proj"
    proj.mkdir()
    bot = make_bot(tmp_path, project=str(proj), account="c1", session_id=SID)
    bot.cfg.claude_bin = fake_claude_bin(tmp_path, "ok")
    bot.profiles = lambda: [Profile("c1", tmp_path / "c1")]
    await bot.run_prompt(OWNER, "rápido")  # 5 ms e sessão ativa: sem aviso extra
    assert not any("terminou" in t for t in sent_texts(bot))
    monkeypatch.setattr(handlers, "LONG_RUN_NOTICE_S", 0.0)
    bot.tg.send_message.reset_mock()
    await bot.run_prompt(OWNER, "longo")
    aviso = [t for t in sent_texts(bot) if "terminou" in t]
    assert len(aviso) == 1 and aviso[0].startswith("✅ terminou [proj · ")
    assert aviso[0].endswith("em 0.0s")


async def test_sessao_nao_ativa_manda_aviso_e_rodape(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    bot = make_bot(tmp_path, project=str(proj), account="c1", session_id=SID)
    bot.cfg.claude_bin = fake_claude_bin(tmp_path, "ok")
    bot.profiles = lambda: [Profile("c1", tmp_path / "c1")]
    orig = handlers.Progress.start

    async def start(self, text):
        await orig(self, text)
        bot.state.session_id = SID2

    monkeypatch.setattr(handlers.Progress, "start", start)
    await bot.run_prompt(OWNER, "x")
    texts = sent_texts(bot)
    assert any(t.startswith(f"[proj · {SID[:8]}]") and "⏱" in t for t in texts)
    assert any(t.startswith("✅ terminou [proj · ") for t in texts)


async def test_run_alimenta_o_feed_para_o_painel(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    bot = make_bot(tmp_path, project=str(proj), account="c1", session_id=SID)
    bot.cfg.claude_bin = fake_claude_bin(tmp_path, "ok")
    bot.profiles = lambda: [Profile("c1", tmp_path / "c1")]
    assert bot.run_feed(SID)["events"] == []
    await bot.run_prompt(OWNER, "x")
    feed = bot.run_feed(SID)
    assert feed["running"] is False
    assert [e["kind"] for e in feed["events"]] == ["init", "tool", "result"]
    assert feed["events"][1]["tool"] == "Bash" and feed["events"][1]["detail"] == "ls"
    assert feed["result"] == {
        "text": "OK",
        "cost_usd": 0.01,
        "duration_ms": 5,
        "is_error": False,
    }
    assert bot.run_feed(SID, after_seq=2)["events"][0]["kind"] == "result"


# ---------- arquivos: receber ----------


def file_upd(kind, *, file_size=10, caption=None, name=None, user=OWNER, reply_to=None):
    u = upd(user=user, reply_to=reply_to)
    m = u.message
    m.caption = caption
    m.photo = m.document = m.video = m.audio = None
    media = SimpleNamespace(
        file_id="fid", file_unique_id="uid", file_size=file_size, file_name=name
    )
    if kind == "photo":
        m.photo = [
            SimpleNamespace(file_id="small", file_unique_id="u0", file_size=1),
            SimpleNamespace(file_id="big", file_unique_id="u1", file_size=file_size),
        ]
    else:
        setattr(m, kind, media)
    return u


def with_download(bot, payload=b"conteudo"):
    bot.tg.get_file = AsyncMock(
        return_value=SimpleNamespace(
            download_as_bytearray=AsyncMock(return_value=bytearray(payload))
        )
    )


@pytest.mark.parametrize("kind", ["document", "video", "audio", "photo"])
async def test_receber_arquivo_salva_no_inbox_e_vira_prompt(tmp_path, kind):
    bot = make_bot(tmp_path)
    with_download(bot)
    bot.run_prompt = AsyncMock()
    await bot.on_file(file_upd(kind, caption="analise isso", name="Relatório.pdf"), CTX)
    await asyncio.sleep(0.05)
    saved = list((bot.cfg.home / ".cache/claude-telegram/inbox").glob("*/*"))
    assert len(saved) == 1 and saved[0].read_bytes() == b"conteudo"
    assert stat.S_IMODE(saved[0].stat().st_mode) == 0o600
    prompt = bot.run_prompt.await_args.args[1]
    assert prompt == f"analise isso\n\n[arquivo anexado: {saved[0]}]"
    assert os.path.isabs(saved[0])


async def test_receber_arquivo_sem_legenda_usa_texto_padrao(tmp_path):
    bot = make_bot(tmp_path)
    with_download(bot)
    bot.run_prompt = AsyncMock()
    await bot.on_file(file_upd("document", name="a.txt"), CTX)
    await asyncio.sleep(0.05)
    assert bot.run_prompt.await_args.args[1].startswith(
        "Arquivo enviado pelo dono\n\n[arquivo anexado: "
    )


async def test_receber_arquivo_grande_demais_nao_baixa(tmp_path):
    bot = make_bot(tmp_path)
    with_download(bot)
    await bot.on_file(
        file_upd("document", file_size=21 * 1024 * 1024, name="x.bin"), CTX
    )
    bot.tg.get_file.assert_not_called()
    assert "grande demais" in sent_texts(bot)[0]


async def test_receber_arquivo_falha_de_download(tmp_path):
    from telegram.error import BadRequest

    bot = make_bot(tmp_path)
    bot.tg.get_file = AsyncMock(side_effect=BadRequest("file is too big"))
    bot.run_prompt = AsyncMock()
    await bot.on_file(file_upd("document", name="x.bin"), CTX)
    assert "Falha ao baixar" in sent_texts(bot)[0]
    bot.run_prompt.assert_not_called()


async def test_receber_nome_malicioso_fica_no_inbox(tmp_path):
    bot = make_bot(tmp_path)
    with_download(bot)
    bot.run_prompt = AsyncMock()
    await bot.on_file(file_upd("document", name="../../../.ssh/authorized_keys"), CTX)
    inbox = bot.cfg.home / ".cache/claude-telegram/inbox"
    assert [p.name for p in inbox.glob("*/*")] == ["authorized_keys"]
    assert not (bot.cfg.home / ".ssh").exists()


async def test_audio_e_voz_estao_roteados_separados(tmp_path):
    from telegram import Update as TgUpdate
    from telegram.ext import Application

    from telegram import User

    bot = make_bot(tmp_path)
    app = Application.builder().token("1:abc").build()
    app.bot._bot_user = User(id=1, first_name="b", is_bot=True, username="b_bot")
    bot.register(app)
    base = {
        "message_id": 1,
        "date": 1_800_000_000,
        "chat": {"id": 1, "type": "private"},
        "from": {"id": 1, "is_bot": False, "first_name": "x"},
    }

    def route(**extra):
        u = TgUpdate.de_json({"update_id": 1, "message": {**base, **extra}}, app.bot)
        return next((h.callback for h in app.handlers[0] if h.check_update(u)), None)

    assert (
        route(voice={"file_id": "a", "file_unique_id": "b", "duration": 1})
        == bot.on_voice
    )
    assert (
        route(audio={"file_id": "a", "file_unique_id": "b", "duration": 1})
        == bot.on_file
    )
    assert route(document={"file_id": "a", "file_unique_id": "b"}) == bot.on_file
    assert (
        route(photo=[{"file_id": "a", "file_unique_id": "b", "width": 1, "height": 1}])
        == bot.on_file
    )
    assert (
        route(
            video={
                "file_id": "a",
                "file_unique_id": "b",
                "width": 1,
                "height": 1,
                "duration": 1,
            }
        )
        == bot.on_file
    )
    cmd = route(
        text="/historico", entities=[{"type": "bot_command", "offset": 0, "length": 10}]
    )
    assert cmd == bot.cmd_historico
    cmd = route(
        text="/menu", entities=[{"type": "bot_command", "offset": 0, "length": 5}]
    )
    assert cmd == bot.cmd_menu


# ---------- arquivos: enviar (/send_file) ----------


def home_file(bot, rel="docs/r.pdf", data=b"%PDF"):
    p = bot.cfg.home / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


async def test_send_file_envia_documento_com_legenda_prefixada(tmp_path):
    bot = make_bot(tmp_path)
    bot.tg.send_document = AsyncMock()
    p = home_file(bot)
    ok, err = await bot.send_file(
        str(p), "o relatório", {"cwd": "/a/proj", "session_id": SID}
    )
    assert (ok, err) == (True, None)
    kw = bot.tg.send_document.call_args
    assert kw.args[0] == OWNER and kw.kwargs["document"] == b"%PDF"
    assert kw.kwargs["filename"] == "r.pdf"
    assert kw.kwargs["caption"] == f"[proj · {SID[:8]}] o relatório"


async def test_send_file_legenda_limitada_a_1024(tmp_path):
    bot = make_bot(tmp_path)
    bot.tg.send_document = AsyncMock()
    p = home_file(bot)
    await bot.send_file(str(p), "x" * 1000, {"cwd": "/" + "d" * 80 + "/proj"})
    assert len(bot.tg.send_document.call_args.kwargs["caption"]) <= 1024


async def test_send_file_nega_segredo_sem_vazar_detalhe(tmp_path):
    bot = make_bot(tmp_path)
    bot.tg.send_document = AsyncMock()
    secret = home_file(bot, ".ssh/id_rsa", b"PRIVATE")
    ok, err = await bot.send_file(str(secret), None, {})
    assert ok is False and err == "caminho não permitido"
    envf = home_file(bot, "app/.env", b"K=1")
    assert (await bot.send_file(str(envf), None, {}))[0] is False
    bot.tg.send_document.assert_not_called()


async def test_send_file_sem_dono_e_falha_do_telegram(tmp_path):
    from telegram.error import Forbidden

    sem = make_bot(tmp_path / "a", owner=None)
    assert (await sem.send_file("/x", None, {})) == (False, "bot sem dono vinculado")
    bot = make_bot(tmp_path / "b")
    p = home_file(bot)
    bot.tg.send_document = AsyncMock(side_effect=Forbidden("blocked"))
    ok, err = await bot.send_file(str(p), None, {})
    assert ok is False and err == "falha ao enviar pelo Telegram"


async def test_localapi_send_file_rota(tmp_path):
    from bot.localapi import LocalApi
    from tests.test_localapi import SECRET, auth, http

    seen = []

    async def on_send(caminho, legenda, origem):
        seen.append((caminho, legenda, origem))
        return (caminho == "/ok", None if caminho == "/ok" else "negado")

    async def nop(*a):
        return None

    srv = LocalApi(SECRET, nop, nop, port=0, on_send_file=on_send)
    await srv.start()
    try:
        body = json.dumps(
            {"caminho": "/ok", "legenda": "l", "origem": {"cwd": "/x"}}
        ).encode()
        assert await http(srv.port, path="/send_file", headers=auth(), body=body) == (
            200,
            {"ok": True},
        )
        assert seen == [("/ok", "l", {"cwd": "/x"})]
        body = json.dumps({"caminho": "/no"}).encode()
        assert await http(srv.port, path="/send_file", headers=auth(), body=body) == (
            200,
            {"ok": False, "erro": "negado"},
        )
        for bad in (
            {"caminho": ""},
            {"caminho": 5},
            {"caminho": "/ok", "legenda": "x" * 1001},
            {"caminho": "/ok", "legenda": 5},
            {},
        ):
            status, _ = await http(
                srv.port,
                path="/send_file",
                headers=auth(),
                body=json.dumps(bad).encode(),
            )
            assert status == 400
        status, _ = await http(srv.port, path="/send_file", body=b"{}")  # sem secret
        assert status == 401
    finally:
        await srv.stop()


async def test_localapi_send_file_404_sem_handler():
    from bot.localapi import LocalApi
    from tests.test_localapi import SECRET, auth, http

    async def nop(*a):
        return None

    srv = LocalApi(SECRET, nop, nop, port=0)
    await srv.start()
    try:
        status, _ = await http(
            srv.port, path="/send_file", headers=auth(), body=b'{"caminho":"/x"}'
        )
        assert status == 404
    finally:
        await srv.stop()


# ---------- fachada do Mini App ----------


def facade_bot(tmp_path, **kw):
    bot = make_bot(tmp_path, **kw)
    bot.profiles = lambda: [
        Profile("c1", tmp_path / "c1"),
        Profile("c2", tmp_path / "c2"),
    ]
    return bot


async def test_api_state(tmp_path):
    bot = facade_bot(
        tmp_path, project=str(make_bot(tmp_path).cfg.home), account="c1", session_id=SID
    )
    st = await bot.api_state()
    assert st["working"] == [] and st["slots"] == {}
    assert (
        st["project_label"] == "~ (home)"
        and st["account"] == "c1"
        and st["session_id"] == SID
    )
    assert st["running"] == [] and set(st["power"]) == {"ac", "battery"}
    bot.state.project = "/x/meu-app"
    assert (await bot.api_state())["project_label"] == "meu-app"
    bot.state.project = None
    assert (await bot.api_state())["project_label"] is None


async def test_api_accounts_formato_e_ordem(tmp_path):
    bot = facade_bot(tmp_path, account="c2")
    bot.usage = AsyncMock(
        return_value=[
            usage_item("c1", 100.0),
            usage_item("c2", 10.0),
            usage_item("c3", 5.0, state="expired"),
        ]
    )
    out = await bot.api_accounts()
    assert [a["name"] for a in out] == ["c2", "c1", "c3"]
    first = out[0]
    assert set(first) == {
        "name",
        "five_hour",
        "seven_day",
        "severity",
        "state",
        "active",
        "usable",
    }
    assert first["active"] is True and first["usable"] is True
    assert first["five_hour"] == {"percent": 10.0, "resets_at": None}
    assert out[1]["usable"] is False or out[1]["five_hour"]["percent"] == 100.0
    assert out[2]["usable"] is False and out[2]["state"] == "expired"
    json.dumps(out)  # serializável


async def test_select_account(tmp_path):
    bot = facade_bot(tmp_path, account="c1")
    assert await bot.select_account("c2") is None and bot.state.account == "c2"
    assert "desconhecida" in await bot.select_account("nao-existe")
    bot.state.project, bot.state.session_id = str(tmp_path), SID
    bot.runs.busy = lambda sid: True
    assert "execução" in await bot.select_account("c1")
    assert bot.state.account == "c2"


def test_api_projects_e_select_project(tmp_path, monkeypatch):
    bot = facade_bot(tmp_path, project=None, session_id=SID)
    monkeypatch.setattr(handlers, "load_projects", lambda *a, **k: ["/p0"])
    home = str(bot.cfg.home)
    out = bot.api_projects()
    assert out == [
        {"path": home, "label": "~ (home)", "active": False},
        {"path": "/p0", "label": "p0", "active": False},
    ]
    assert bot.select_project("/p0") is None
    assert bot.state.project == "/p0" and bot.state.session_id is None
    assert bot.api_projects()[1]["active"] is True
    assert bot.select_project("/etc") == "projeto desconhecido"
    assert bot.select_project(None) == "projeto desconhecido"
    assert bot.state.project == "/p0"


async def test_api_sessions_select_session_e_new_session(tmp_path, monkeypatch):
    bot = facade_bot(tmp_path, project=str(tmp_path), session_id=SID)
    infos = [
        SessionInfo(SID, "c1", tmp_path, str(tmp_path), NOW, "primeiro"),
        SessionInfo(
            SID2, None, tmp_path, str(tmp_path), NOW - timedelta(hours=1), "segundo"
        ),
    ]
    bot.session_list = lambda: infos
    bot.runs.running = lambda: [SID2]
    bot.now = lambda: NOW
    out = await bot.api_sessions()
    assert out[0] == {
        "session_id": SID,
        "short": SID[:8],
        "project": str(tmp_path),
        "project_label": tmp_path.name,
        "account": "c1",
        "mtime": NOW.isoformat(),
        "age_s": 0,
        "last_prompt": "primeiro",
        "title": "primeiro",  # sem título do Claude: cai no último prompt
        "first_prompt": "primeiro",
        "active": True,
        "running": False,
        "pc": False,
        "hosted": False,
        "working": False,
        "open": False,
    }
    assert (
        out[1]["running"] is True
        and out[1]["working"] is True  # execução do bot viva = trabalhando
        and out[1]["active"] is False
        and out[1]["account"] is None
        and out[1]["age_s"] == 3600
    )
    assert await bot.select_session(SID2) is None and bot.state.session_id == SID2
    assert await bot.select_session("../x") == "sessão inválida"
    assert (
        await bot.select_session("33333333-3333-4333-8333-333333333333")
        == "sessão não encontrada"
    )
    sid = bot.new_session()
    assert bot.state.session_id == sid and handlers.is_uuid(sid)
    bot.state.project = None
    assert await bot.api_sessions() == []
    assert await bot.select_session(SID) == "sessão não encontrada"


async def test_select_session_do_painel_manda_resumo_ao_chat(tmp_path, monkeypatch):
    fake_history(monkeypatch, tmp_path)
    bot = facade_bot(tmp_path, project=str(tmp_path))
    bot.profiles = lambda: []
    bot.session_list = lambda: [
        SessionInfo(SID, None, tmp_path, str(tmp_path), NOW, "x")
    ]
    assert await bot.select_session(SID) is None
    texts = sent_texts(bot)
    assert texts[-2].startswith("🔵 ativa: ") and texts[-2].endswith(
        f"{SID[:8]} — mande a mensagem aqui"
    )
    assert "Você: faça x" in texts[-1]


async def test_api_history(tmp_path, monkeypatch):
    T = SimpleNamespace
    ts = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
    calls = fake_history(
        monkeypatch,
        tmp_path,
        turns=[
            T(role="user", text="oi", tool=None, ts=ts),
            T(role="tool", text="r", tool="Bash", ts=None),
        ],
    )
    bot = facade_bot(tmp_path, project=str(tmp_path))
    out = await bot.api_history(SID, limit=500, before=None)
    assert out == {
        "turns": [
            {"role": "user", "text": "oi", "tool": None, "ts": ts.isoformat()},
            {"role": "tool", "text": "r", "tool": "Bash", "ts": None},
        ],
        "before": 7,
    }
    assert calls == [(200, None)]  # limite clampado
    assert await bot.api_history("nao-uuid") == {"turns": [], "before": None}
    monkeypatch.setattr(
        handlers.sessions_mod, "session_file", lambda *a: None, raising=False
    )
    assert await bot.api_history(SID) == {"turns": [], "before": None}


async def test_api_history_sem_modulo_de_historico(tmp_path, monkeypatch):
    for name in ("session_file", "read_history", "last_exchange"):
        monkeypatch.delattr(handlers.sessions_mod, name, raising=False)
    bot = facade_bot(tmp_path, project=str(tmp_path))
    assert await bot.api_history(SID) == {"turns": [], "before": None}
    assert await bot.last_exchange_for(SID) == (None, None)


async def test_historico_nao_trava_o_loop(tmp_path, monkeypatch):
    """read_history/last_exchange/session_file lentos rodam em thread: outra corrotina termina antes."""
    import time

    def slow(*a, **k):
        time.sleep(0.4)
        return [], None

    monkeypatch.setattr(
        handlers.sessions_mod,
        "session_file",
        lambda *a: tmp_path / "s.jsonl",
        raising=False,
    )
    monkeypatch.setattr(handlers.sessions_mod, "read_history", slow, raising=False)
    monkeypatch.setattr(
        handlers.sessions_mod,
        "last_exchange",
        lambda p: (time.sleep(0.4), ("a", "b"))[1],
        raising=False,
    )
    bot = facade_bot(tmp_path, project=str(tmp_path))
    bot.profiles = lambda: []
    order = []

    async def ticker():
        for _ in range(3):
            await asyncio.sleep(0.02)
        order.append("loop livre")

    async def hist():
        await bot.api_history(SID)
        order.append("history")

    async def exch():
        await bot.last_exchange_for(SID)
        order.append("exchange")

    await asyncio.gather(hist(), exch(), ticker())
    assert order[0] == "loop livre" and set(order[1:]) == {"history", "exchange"}


async def test_send_history_e_resumo_nao_travam_o_loop(tmp_path, monkeypatch):
    import time

    monkeypatch.setattr(
        handlers.sessions_mod,
        "session_file",
        lambda *a: tmp_path / "s.jsonl",
        raising=False,
    )
    monkeypatch.setattr(
        handlers.sessions_mod,
        "read_history",
        lambda *a, **k: (time.sleep(0.3), ([], None))[1],
        raising=False,
    )
    bot = facade_bot(tmp_path, project=str(tmp_path), session_id=SID)
    bot.profiles = lambda: []
    done = []

    async def ticker():
        await asyncio.sleep(0.05)
        done.append("ticker")

    await asyncio.gather(bot.cmd_historico(upd(), CTX), ticker())
    assert done == ["ticker"]
    assert "sem mensagens" in sent_texts(bot)[-1]


def test_painel_nao_envia_prompt():
    """Decisão do dono: o Mini App não manda prompts (a fachada não expõe submit_prompt)."""
    assert not hasattr(handlers.Bot, "submit_prompt")


def test_new_session_com_projeto_e_conta(tmp_path, monkeypatch):
    bot = facade_bot(tmp_path, project="/velho", account="c1", session_id=SID)
    monkeypatch.setattr(handlers, "load_projects", lambda *a, **k: ["/p0"])
    sid = bot.new_session(project="/p0", account="c2")
    assert handlers.is_uuid(sid)
    assert (bot.state.project, bot.state.account, bot.state.session_id) == (
        "/p0",
        "c2",
        sid,
    )
    # inválidos: erro e NADA muda
    assert bot.new_session(project="/etc") == "projeto desconhecido"
    assert bot.new_session(account="zzz") == "conta desconhecida"
    assert (bot.state.project, bot.state.account, bot.state.session_id) == (
        "/p0",
        "c2",
        sid,
    )
    assert handlers.is_uuid(bot.new_session())  # sem args: como antes


async def test_api_sessions_scope_open_de_qualquer_projeto(tmp_path, monkeypatch):
    bot = facade_bot(tmp_path, project="/a/proj", session_id=SID)
    bot.now = lambda: NOW
    monkeypatch.setattr(
        bot,
        "_open_sessions_sync",
        lambda: [
            {
                "session_id": SID,
                "cwd": "/a/proj",
                "account": "c1",
                "mtime": NOW - timedelta(seconds=7),
                "pc": True,
                "bot": False,
                "last_prompt": "faz x",
                "first_prompt": "começo",
                "working": True,
            },
            {
                "session_id": SID2,
                "cwd": str(bot.cfg.home),
                "account": None,
                "mtime": NOW,
                "pc": False,
                "bot": True,
                "last_prompt": "",
                "first_prompt": "",
                "working": True,
            },
        ],
    )
    bot.runs.running = lambda: [SID2]
    out = await bot.api_sessions("open")
    assert [s["project_label"] for s in out] == ["proj", "~ (home)"]
    assert out[0] == {
        "session_id": SID,
        "short": SID[:8],
        "project": "/a/proj",
        "project_label": "proj",
        "account": "c1",
        "mtime": (NOW - timedelta(seconds=7)).isoformat(),
        "age_s": 7,
        "last_prompt": "faz x",
        "title": "faz x",
        "first_prompt": "começo",
        "active": True,
        "running": False,
        "pc": True,
        "hosted": False,
        "working": True,
        "open": True,
    }
    assert (
        out[1]["running"] is True
        and out[1]["pc"] is False
        and out[1]["account"] is None
    )
    json.dumps(out)


async def test_use_session_e_end_session(tmp_path):
    bot = facade_bot(tmp_path, project=str(tmp_path))
    bot.session_list = lambda: [
        SessionInfo(SID, "c1", tmp_path, str(tmp_path), NOW, "x")
    ]
    assert await bot.use_session(SID) == {"ok": True} and bot.state.session_id == SID
    assert await bot.use_session("../x") == {"error": "sessão inválida"}
    assert await bot.end_session("lixo") == {"ok": False, "error": "sessão inválida"}
    assert await bot.end_session(SID) == {"ok": True, "outcome": "not_running"}
    run = MagicMock(stop=AsyncMock())
    bot.runs.get = lambda sid: run
    bot.runs.busy = lambda sid: True
    assert await bot.end_session(SID) == {"ok": True, "outcome": "stopped"}
    run.stop.assert_awaited_once()
    assert SID in bot._stopped


async def test_end_session_nunca_mata_sessao_aberta_no_pc(tmp_path, monkeypatch):
    """Sessão do terminal/Horizon (não é execução do bot) não é tocada: outcome not_running."""
    bot = facade_bot(tmp_path, project=str(tmp_path))
    monkeypatch.setattr(
        handlers.sessions_mod, "list_open", lambda profs: [], raising=False
    )
    killed = []
    monkeypatch.setattr(os, "kill", lambda *a: killed.append(a))
    monkeypatch.setattr(os, "killpg", lambda *a: killed.append(a))
    assert await bot.end_session(SID) == {"ok": True, "outcome": "not_running"}
    assert killed == []


async def test_stop_active(tmp_path):
    bot = facade_bot(tmp_path, session_id=SID)
    assert await bot.stop_active() is False
    run = MagicMock(stop=AsyncMock())
    bot.runs.get = lambda sid: run
    bot.runs.busy = lambda sid: True
    assert await bot.stop_active() is True
    run.stop.assert_awaited_once()
    assert SID in bot._stopped


# ---------- menu button (Mini App) e wiring ----------


async def test_menu_button_so_com_webapp_url_https(tmp_path, monkeypatch):
    bot = make_bot(tmp_path)
    bot.tg.set_chat_menu_button = AsyncMock()
    monkeypatch.delenv("WEBAPP_URL", raising=False)
    await bot.apply_menu_button()
    bot.tg.set_chat_menu_button.assert_not_called()
    monkeypatch.setenv("WEBAPP_URL", "http://inseguro.example")
    await bot.apply_menu_button()
    bot.tg.set_chat_menu_button.assert_not_called()
    monkeypatch.setenv("WEBAPP_URL", "https://painel.example.app")
    await bot.apply_menu_button()
    kw = bot.tg.set_chat_menu_button.call_args.kwargs
    assert kw["chat_id"] == OWNER
    assert kw["menu_button"].text == "Painel"
    assert kw["menu_button"].web_app.url == "https://painel.example.app"
    sem = make_bot(tmp_path / "x", owner=None)
    sem.tg.set_chat_menu_button = AsyncMock()
    await sem.apply_menu_button()
    sem.tg.set_chat_menu_button.assert_not_called()


async def test_menu_button_falha_do_telegram_nao_levanta(tmp_path, monkeypatch):
    from telegram.error import BadRequest

    bot = make_bot(tmp_path)
    bot.tg.set_chat_menu_button = AsyncMock(side_effect=BadRequest("x"))
    monkeypatch.setenv("WEBAPP_URL", "https://painel.example.app")
    await bot.apply_menu_button()


async def test_start_stop_webapp_com_modulo_fake(tmp_path, monkeypatch):
    calls = []

    class FakeWebapp:
        @staticmethod
        async def start(facade, cfg):
            calls.append(("start", facade, cfg))
            return "server"

        @staticmethod
        async def stop(server):
            calls.append(("stop", server))

    monkeypatch.setattr(main_mod, "_load_webapp", lambda: FakeWebapp)
    bot = make_bot(tmp_path)
    server = await main_mod.start_webapp(bot, bot.cfg)
    assert server == "server" and calls[0][1:] == (bot, bot.cfg)
    await main_mod.stop_webapp(server)
    assert calls[-1] == ("stop", "server")


async def test_webapp_ausente_ou_com_falha_nao_derruba_o_bot(tmp_path, monkeypatch):
    bot = make_bot(tmp_path)
    monkeypatch.setattr(main_mod, "_load_webapp", lambda: None)
    assert await main_mod.start_webapp(bot, bot.cfg) is None

    class Quebrado:
        @staticmethod
        async def start(facade, cfg):
            raise OSError("porta ocupada")

        @staticmethod
        async def stop(server):
            raise RuntimeError("x")

    monkeypatch.setattr(main_mod, "_load_webapp", lambda: Quebrado)
    assert await main_mod.start_webapp(bot, bot.cfg) is None
    await main_mod.stop_webapp("s")  # erro no stop é engolido
    await main_mod.stop_webapp(None)
