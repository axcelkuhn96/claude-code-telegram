"""/sessoes: só as sessões abertas agora (registro vivo ∪ bot) + "Retomar…" com as recentes."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from bot import handlers
from bot.sessions import SessionInfo
from bot.usage import Profile
from tests.test_handlers import CTX, OWNER, SID, SID2, make_bot, sent_texts, upd

NOW = datetime.now(UTC)
SID3 = "33333333-3333-4333-8333-333333333333"


def open_s(sid, cwd, account, minutes_ago=5):
    return SimpleNamespace(
        pid=1,
        session_id=sid,
        cwd=cwd,
        account=account,
        config_dir=None,
        started_at=NOW - timedelta(minutes=minutes_ago),
        kind="interactive",
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Bot com 2 contas, `list_open` e arquivos de sessão falsos."""
    bot = make_bot(tmp_path, project="/home/x/proj", account="c1", session_id=SID)
    bot.profiles = lambda: [
        Profile("c1", tmp_path / "c1"),
        Profile("c2", tmp_path / "c2"),
    ]
    bot.now = lambda: (
        NOW
    )  # idades determinísticas (o módulo importa bem antes do teste rodar)
    state = SimpleNamespace(open=[], prompts={}, titles={})
    monkeypatch.setattr(
        handlers.sessions_mod,
        "tail_meta",
        lambda path, limit=60: (
            state.titles.get(path.stem, ""),
            state.prompts[path.stem],
        ),
        raising=False,
    )
    monkeypatch.setattr(
        handlers.sessions_mod, "list_open", lambda profs: state.open, raising=False
    )

    def session_file(cwd, sid, profs):
        if sid not in state.prompts:
            return None
        f = tmp_path / f"{sid}.jsonl"
        f.write_text("{}\n")
        started = next((o.started_at for o in state.open if o.session_id == sid), None)
        if started:  # mtime do arquivo = início da sessão falsa (ordem determinística)
            os.utime(f, (started.timestamp(), started.timestamp()))
        return f

    monkeypatch.setattr(
        handlers.sessions_mod, "session_file", session_file, raising=False
    )
    monkeypatch.setattr(
        handlers.sessions_mod,
        "last_prompt",
        lambda path, limit=60: state.prompts[path.stem],
        raising=False,
    )
    monkeypatch.setattr(
        handlers.sessions_mod,
        "last_exchange",
        lambda p: ("pergunta antiga", "resposta"),
        raising=False,
    )
    bot.session_list = lambda: []
    return bot, state


def buttons(bot):
    return bot.tg.send_message.call_args.kwargs["reply_markup"].inline_keyboard


async def test_cartoes_das_abertas_com_titulo_botoes_numericos_e_linha_final(env):
    bot, st = env
    st.open = [
        open_s(SID, "/home/x/proj", "c1", 2),
        open_s(SID2, "/home/x/outro", None, 90),
    ]
    st.prompts = {SID: "corrige o bug do login", SID2: "escreve os testes"}
    st.titles = {SID: "Bug do login <v2> & co"}
    await bot.cmd_sessoes(upd(), CTX)
    text = sent_texts(bot)[-1]
    assert bot.tg.send_message.call_args.kwargs["parse_mode"] == "HTML"
    assert text == (
        "🗂 <b>Abertas agora</b>\n🖥 = aberta no PC — evite usar ao mesmo tempo\n\n"
        "1️⃣ 🔵🖥 <b>Bug do login &lt;v2&gt; &amp; co</b>\n"
        "     “corrige o bug do login” · há 2 min · c1 · proj\n\n"
        "2️⃣ 🖥 <b>escreve os testes</b>\n"  # sem título: cai no último prompt
        "     “escreve os testes” · há 1 h · ? · outro"
    )
    rows = buttons(bot)
    assert [[b.text for b in r] for r in rows] == [
        ["1️⃣", "2️⃣"],
        ["📂 Retomar…", "🆕 Nova"],
    ]
    assert [b.callback_data for b in rows[0]] == [f"s:{SID}", f"s:{SID2}"]
    assert [b.callback_data for b in rows[1]] == ["o:r", "o:n"]
    assert all(len(b.callback_data.encode()) <= 64 for r in rows for b in r)
    assert SID[:8] not in text and SID2[:8] not in text  # nenhum id nas listas


async def test_marcas_pc_ativa_e_trabalhando_no_bot(env):
    bot, st = env
    st.open = [open_s(SID, "/home/x/proj", "c1"), open_s(SID2, "/home/x/proj", "c2")]
    st.prompts = {SID: "a", SID2: "b"}
    bot.runs.running = lambda: [SID2]  # a execução do próprio bot não é "aberta no PC"
    await bot.cmd_sessoes(upd(), CTX)
    cards = sent_texts(bot)[-1].split("\n\n")[1:]
    by_sid = {SID: cards[0], SID2: cards[1]}
    assert by_sid[SID].startswith("1️⃣ 🔵🖥 ") and "⏳" not in by_sid[SID]
    assert by_sid[SID2].startswith("2️⃣ ⏳ ") and "🖥" not in by_sid[SID2]


async def test_idade_agora_min_h_d_e_conta_desconhecida(env):
    bot, st = env
    from bot.handlers import age_since

    assert [
        age_since(timedelta(seconds=s)) for s in (10, 300, 3 * 3600, 2 * 86400)
    ] == [
        "agora",
        "há 5 min",
        "há 3 h",
        "há 2 d",
    ]
    st.open = [open_s(SID2, "/home/x/proj", None)]
    st.prompts = {SID2: ""}
    await bot.cmd_sessoes(upd(), CTX)
    card = sent_texts(bot)[-1].split("\n\n")[1]
    assert (
        card == "1️⃣ 🖥 <b>(sem título)</b>\n     há 5 min · ? · proj"
    )  # sem prompt: sem aspas


async def test_prompt_cortado_em_45_e_titulo_em_80(env):
    bot, st = env
    st.open = [open_s(SID2, "/home/x/proj", "c1")]
    st.prompts = {SID2: "x" * 100}
    st.titles = {SID2: "T" * 200}
    await bot.cmd_sessoes(upd(), CTX)
    card = sent_texts(bot)[-1].split("\n\n")[1]
    assert "“" + "x" * 44 + "…”" in card
    assert "<b>" + "T" * 79 + "…</b>" in card


async def test_sessoes_do_bot_entram_sem_pc_e_usam_first_prompt(env):
    bot, st = env
    bot.bot_sessions[SID3] = SessionInfo(
        SID3,
        "c2",
        bot.cfg.home,
        "/home/x/bot-proj",
        NOW - timedelta(minutes=1),
        "meu primeiro pedido",
    )
    await bot.cmd_sessoes(upd(), CTX)
    card = sent_texts(bot)[-1].split("\n\n")[1]
    assert card == (  # rodou antes, não está rodando agora: sem ⏳; não é do PC: sem 🖥
        "1️⃣ <b>meu primeiro pedido</b>\n"
        "     “meu primeiro pedido” · há 1 min · c2 · bot-proj"
    )
    assert "🖥" not in card


async def test_aberta_no_pc_e_rodada_pelo_bot_aparece_uma_vez(env):
    bot, st = env
    st.open = [open_s(SID2, "/home/x/proj", "c1")]
    st.prompts = {SID2: "p"}
    bot.bot_sessions[SID2] = SessionInfo(
        SID2, "c1", bot.cfg.home, "/home/x/proj", NOW, "p"
    )
    await bot.cmd_sessoes(upd(), CTX)
    assert len(buttons(bot)[0]) == 1 and sent_texts(bot)[-1].count("<b>p</b>") == 1


async def test_nenhuma_aberta_mantem_retomar_e_nova(env):
    bot, st = env
    await bot.cmd_sessoes(upd(), CTX)
    assert sent_texts(bot)[-1] == "🗂 <b>Nenhuma sessão aberta agora</b>"
    assert [b.text for b in buttons(bot)[0]] == ["📂 Retomar…", "🆕 Nova"]


async def test_no_maximo_10_cartoes_em_fileiras_de_5(env):
    bot, st = env
    st.open = [open_s(str(uuid.uuid4()), "/home/x/p", "c1", i + 1) for i in range(20)]
    st.prompts = {o.session_id: "p" for o in st.open}
    await bot.cmd_sessoes(upd(), CTX)
    rows = buttons(bot)
    assert [len(r) for r in rows] == [5, 5, 2]  # 10 números + [Retomar][Nova]
    assert rows[1][-1].text == "🔟" and sent_texts(bot)[-1].count("<b>p</b>") == 10


async def test_sem_modulo_list_open_nao_quebra(env, monkeypatch):
    bot, st = env
    monkeypatch.delattr(handlers.sessions_mod, "list_open", raising=False)
    await bot.cmd_sessoes(upd(), CTX)
    assert "Nenhuma sessão aberta" in sent_texts(bot)[-1]


async def test_resumo_ao_trocar_traz_o_titulo_em_negrito(env):
    bot, st = env
    st.open = [open_s(SID2, "/home/x/outro", "c2")]
    st.prompts = {SID2: "x"}
    st.titles = {SID2: "Assistente <MCP>"}
    await bot.on_callback(upd(data=f"s:{SID2}"), CTX)
    resumo = bot.tg.send_message.call_args
    assert resumo.args[1] == (
        "🔵 <b>Assistente &lt;MCP&gt;</b>\nVocê: pergunta antiga\nClaude: resposta"
    )
    assert resumo.kwargs["parse_mode"] == "HTML"


async def test_api_sessions_traz_title(env):
    bot, st = env
    st.open = [open_s(SID, "/home/x/proj", "c1"), open_s(SID2, "/home/x/proj", "c1")]
    st.prompts = {SID: "último", SID2: "outro"}
    st.titles = {SID: "Meu título"}
    out = {s["session_id"]: s for s in await bot.api_sessions("open")}
    assert (
        out[SID]["title"] == "Meu título" and out[SID2]["title"] == "outro"
    )  # fallback: last_prompt


async def test_selecionar_aberta_de_outro_projeto_troca_projeto_conta_e_mostra_resumo(
    env,
):
    bot, st = env
    st.open = [open_s(SID2, "/home/x/outro", "c2")]
    st.prompts = {SID2: "x"}
    bot.state.project, bot.state.account = "/home/x/proj", "c1"
    await bot.on_callback(upd(data=f"s:{SID2}"), CTX)
    assert (bot.state.project, bot.state.session_id, bot.state.account) == (
        "/home/x/outro",
        SID2,
        "c2",
    )
    texts = sent_texts(bot)
    assert "outro" in texts[0] and "c2" in texts[0]
    assert texts[1] == "Você: pergunta antiga\nClaude: resposta"
    assert (bot.cfg.state_dir / "state.json").exists()


async def test_selecionar_sessao_do_retomar_mantem_projeto(env):
    bot, st = env
    bot.session_list = lambda: [
        SessionInfo(SID2, "c2", bot.cfg.home, "/home/x/proj", NOW, "x")
    ]
    await bot.on_callback(upd(data=f"s:{SID2}"), CTX)
    assert bot.state.project == "/home/x/proj" and bot.state.session_id == SID2
    assert bot.state.account == "c2"


async def test_selecionar_sessao_desconhecida_ou_uuid_invalido(env):
    bot, _ = env
    await bot.on_callback(upd(data=f"s:{SID3}"), CTX)
    assert "não encontrada" in sent_texts(bot)[-1]
    n = len(sent_texts(bot))
    await bot.on_callback(upd(data="s:../../x"), CTX)
    assert len(sent_texts(bot)) == n
    assert bot.state.session_id == SID


async def test_retomar_lista_recentes_do_projeto_so_botoes(env):
    bot, _ = env
    bot.runs.running = lambda: [SID2]
    bot.session_list = lambda: [
        SessionInfo(
            SID,
            "c1",
            bot.cfg.home,
            "/home/x/proj",
            NOW - timedelta(minutes=10),
            "primeiro",
            "ultimo prompt",
        ),
        SessionInfo(
            SID2,
            None,
            bot.cfg.home,
            "/home/x/proj",
            NOW - timedelta(hours=3),
            "so o primeiro",
            "",
        ),
    ]
    await bot.on_callback(upd(data="o:r"), CTX)
    text = sent_texts(bot)[-1]
    assert bot.tg.send_message.call_args.kwargs["parse_mode"] == "HTML"
    assert text == (
        "📂 <b>Recentes em proj</b> — toque para ativar\n\n"
        "1️⃣ 🔵 <b>ultimo prompt</b>\n     “ultimo prompt” · há 10 min · c1\n\n"
        "2️⃣ ⏳ <b>so o primeiro</b>\n     “so o primeiro” · há 3 h · ?"
    )
    rows = buttons(bot)
    assert [[b.text for b in r] for r in rows] == [
        ["1️⃣", "2️⃣"]
    ]  # só números, sem linha extra
    assert [b.callback_data for b in rows[0]] == [f"s:{SID}", f"s:{SID2}"]
    assert SID[:8] not in text and SID2[:8] not in text  # nenhum id no texto


async def test_retomar_sem_projeto_e_sem_sessoes(env):
    bot, _ = env
    bot.state.project = None
    await bot.on_callback(upd(data="o:r"), CTX)
    assert "/projetos" in sent_texts(bot)[-1]
    bot.state.project = "/home/x/proj"
    await bot.on_callback(upd(data="o:r"), CTX)
    assert "Nenhuma sessão neste projeto" in sent_texts(bot)[-1]


async def test_nova_pelo_botao(env):
    bot, _ = env
    await bot.on_callback(upd(data="o:n"), CTX)
    assert bot.state.session_id not in (None, SID)
    assert "🆕" in sent_texts(bot)[-1]
    bot.state.project = None
    before = bot.state.session_id
    await bot.on_callback(upd(data="o:n"), CTX)
    assert bot.state.session_id == before and "/projetos" in sent_texts(bot)[-1]


async def test_api_sessions_marca_open_e_pc(env):
    bot, st = env
    st.open = [open_s(SID, "/home/x/proj", "c1"), open_s(SID3, "/home/x/proj", "c1")]
    bot.session_list = lambda: [
        SessionInfo(SID, "c1", bot.cfg.home, "/home/x/proj", NOW, "a"),
        SessionInfo(SID2, "c1", bot.cfg.home, "/home/x/proj", NOW, "b"),
    ]
    out = {s["session_id"]: s for s in await bot.api_sessions()}
    assert (out[SID]["open"], out[SID]["pc"]) == (True, True)
    assert (out[SID2]["open"], out[SID2]["pc"]) == (False, False)
    bot.runs.running = lambda: [SID]
    out = {s["session_id"]: s for s in await bot.api_sessions()}
    assert (out[SID]["open"], out[SID]["pc"], out[SID]["running"]) == (
        True,
        False,
        True,
    )


async def test_sessoes_trancado_so_aviso(env):
    bot, st = env
    bot.totp_secret = "ABCDEFGHIJKLMNOP"
    st.open = [open_s(SID, "/home/x/proj", "c1")]
    await bot.cmd_sessoes(upd(), CTX)
    assert sent_texts(bot) == ["🔒 trancado — /destravar <código>"]
    await bot.on_callback(upd(data="o:r"), CTX)
    await bot.on_callback(upd(data=f"s:{SID}"), CTX)
    assert set(sent_texts(bot)) == {"🔒 trancado — /destravar <código>"}


def test_sessao_aberta_arquivo_ilegivel_nao_derruba(env, monkeypatch):
    bot, st = env
    st.open = [open_s(SID, "/home/x/proj", "c1")]
    monkeypatch.setattr(
        handlers.sessions_mod,
        "session_file",
        MagicMock(side_effect=OSError("x")),
        raising=False,
    )
    items = bot._open_sessions_sync()
    assert items[0]["session_id"] == SID and items[0]["last_prompt"] == ""
    assert os.path.isabs(str(bot.cfg.home))
