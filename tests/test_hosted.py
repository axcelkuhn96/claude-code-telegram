"""bot/hosted.py: argv do tmux/claude, painel (pronto/ocupado/diálogo), .jsonl e fim de turno.

Painéis copiados de `tmux capture-pane -p` reais do Claude Code 2.1.328 (largura cortada, URL
do Remote Control trocada por uma falsa). Linhas de .jsonl modeladas nas reais (só os campos
que importam).
"""

from __future__ import annotations

import json

import pytest

from bot import hosted
from bot.hosted import (
    Host,
    HostedTurn,
    JsonlTail,
    Tmux,
    TurnTracker,
    clean_paste,
    input_box,
    last_rc_url,
    pane_state,
    parse_dialog,
    prompt_matches,
)
from bot.stream import Event

SID = "2964a863-5ebb-4e2c-9004-2f3ce15a55f9"
SEP = "─" * 60
URL = "https://claude.ai/code/session_01FAKEFAKEFAKE"

PANE_READY = f"""\
 ▐▛███▛█   Claude Code v2.1.328
▝▜██████▀  Opus 5.5 with high effort · Claude Ultra
 ▝▝   ▝▝   ~/cbs-lab · /rc active
  /remote-control is active · Continue here, on your phone, or at {URL}

{SEP}
❯ Try "fix typecheck errors"
{SEP}
  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents
"""

PANE_TYPED = f"""\
❯ responda só OK
  linha 2: nada mais
● OK
✻ Brewed for 3s · done 19:17

{SEP}
❯ linha a
  linha b
{SEP}
  ⏵⏵ bypass permissions on (shift+tab to cycle)
"""

PANE_BUSY = f"""\
❯ Use AskUserQuestion de novo
● Answered Claude's questions:
  ⎿  · Qual cor você escolhe? → Verde
✢ Shimmying… (4s · ↓ 160 tokens)
  ⎿  Tip: Connect Claude to your IDE · /ide

{SEP}
❯
{SEP}
  ⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt
"""

PANE_ASK = f"""\
● OK
✻ Brewed for 3s · done 19:17
❯ Use a ferramenta AskUserQuestion para me perguntar uma cor (vermelho, verde ou azul).
{SEP}
 ☐ Cor

Qual cor você escolhe?

❯ 1. Vermelho
     Cor vermelha
  2. Verde
     Cor verde
  3. Azul
     Cor azul
  4. Type something.
{SEP}
  5. Chat about this

Enter to select · ↑/↓ to navigate · Esc to cancel
"""

PANE_ASK_CURSOR3 = PANE_ASK.replace("❯ 1. Vermelho", "  1. Vermelho").replace(
    "  3. Azul", "❯ 3. Azul"
)

PANE_PERMISSION = f"""\
● Bash(rm -rf build)
{SEP}
 Bash command

   rm -rf build
   Remove build dir

 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and don't ask again for rm commands in /home/x
   3. No, and tell Claude what to do differently (esc)

 Esc to cancel · Tab to amend
"""

PANE_TRUST = f"""\
{SEP}
 Accessing workspace:

 /home/dono/novo

 Quick safety check: Is this a project you created or one you trust?

 ❯ 1. Yes, I trust this folder
   2. No, exit

 Enter to confirm · Esc to cancel
"""

PANE_NUMBERED_REPLY = f"""\
● Passos:
  1. Instalar
  2. Rodar
✻ Worked for 3s · done 19:17

{SEP}
❯
{SEP}
  ⏵⏵ bypass permissions on (shift+tab to cycle)
"""


# ---------- argv ----------


def test_claude_argv_remote_control_com_igual_e_mcp_por_ultimo():
    argv = hosted.claude_argv("/bin/claude", SID, False, "pc-cbs-lab", "/r/mcp.json")
    assert argv == [
        "/bin/claude",
        "--dangerously-skip-permissions",
        "--remote-control=pc-cbs-lab",
        "--resume",
        SID,
        "--mcp-config",
        "/r/mcp.json",
    ]
    assert hosted.claude_argv("c", SID, True, "n", "m")[3] == "--session-id"


def test_nomes():
    assert hosted.session_name(SID) == "cc-2964a863"
    assert hosted.rc_session_name("/home/dono/cbs-lab/", "pc") == "pc-cbs-lab"
    assert hosted.rc_session_name("/", "pc") == "pc-root"


class Rec:
    def __init__(self, results=None):
        self.calls: list[tuple[list[str], str | None]] = []
        self.results = results or {}

    def __call__(self, argv, inp=None):
        self.calls.append((argv, inp))
        for key, res in self.results.items():
            if key in argv:
                return res
        return 0, ""


def test_tmux_start_usa_scope_socket_proprio_e_env():
    rec = Rec()
    ok, _ = Tmux(rec).start(
        "cc-2964a863",
        "/p/x",
        {"CLAUDE_CONFIG_DIR": "/c3", "A": "b c"},
        ["claude", "-x"],
    )
    argv, _ = rec.calls[0]
    assert ok
    assert argv[:6] == [
        "systemd-run",
        "--user",
        "--scope",
        "--collect",
        "--quiet",
        "--",
    ]
    i = argv.index("-L")
    assert argv[i + 1] == "claude-tg" and "new-session" in argv
    assert argv[argv.index("-s") + 1] == "cc-2964a863"
    assert argv[argv.index("-x") + 1] == "220" and argv[argv.index("-y") + 1] == "50"
    assert argv[argv.index("-c") + 1] == "/p/x"
    assert "CLAUDE_CONFIG_DIR=/c3" in argv and "A=b c" in argv
    assert argv[-2:] == ["claude", "-x"]


def test_tmux_recusa_nome_fora_do_padrao():
    rec = Rec()
    t = Tmux(rec)
    assert t.start("x; rm", "/p", {}, ["c"]) == (False, "nome inválido")
    assert (
        not t.kill("cc-../../x") and not t.paste("bad", "oi") and not t.keys("bad", "a")
    )
    assert t.capture("bad") == "" and t.env("bad") == {} and not t.exists("bad")
    assert rec.calls == []


def test_tmux_paste_por_stdin_em_bracketed_paste():
    rec = Rec()
    assert Tmux(rec).paste("cc-2964a863", "a\nb\x1b[201~c")
    (load, inp), (paste, _) = rec.calls
    assert load[-4:] == ["load-buffer", "-b", "tg-cc-2964a863", "-"]
    assert inp == "a\nb[201~c"  # ESC some: nada sai do modo de colagem
    assert paste[3:] == [
        "paste-buffer",
        "-p",
        "-d",
        "-b",
        "tg-cc-2964a863",
        "-t",
        "cc-2964a863",
    ]


def test_tmux_keys_literal_e_kill_e_env_e_names():
    rec = Rec(
        {
            "show-environment": (0, "CLAUDE_SESSION_ID=abc\n-REMOVIDA\nX=1=2\n"),
            "list-sessions": (0, "cc-2964a863\noutra\ncc-zzzz\ncc-0000aaaa\n"),
        }
    )
    t = Tmux(rec)
    assert t.keys("cc-2964a863", "2", literal=True)
    assert rec.calls[-1][0][3:] == ["send-keys", "-t", "cc-2964a863", "-l", "2"]
    assert t.kill("cc-2964a863")
    assert rec.calls[-1][0][3:] == ["kill-session", "-t", "cc-2964a863"]
    assert t.env("cc-2964a863") == {"CLAUDE_SESSION_ID": "abc", "X": "1=2"}
    assert t.names() == ["cc-2964a863", "cc-0000aaaa"]


def test_clean_paste():
    assert clean_paste("!rm -rf /") == " !rm -rf /"
    assert clean_paste("#lembrar") == " #lembrar"
    assert clean_paste("a\r\nb\tc\x07") == "a\nb\tc"


def test_run_cmd_nunca_levanta(monkeypatch):
    code, out = hosted.run_cmd(["/nao/existe/binario"])
    assert code == 127 and out


# ---------- painel ----------


def test_pane_pronto_ocupado_e_caixa():
    assert pane_state(PANE_READY) == "ready"
    assert input_box(PANE_READY) == ""  # sugestão esmaecida da caixa vazia
    assert pane_state(PANE_BUSY) == "busy"
    assert input_box(PANE_BUSY) == ""
    assert input_box(PANE_TYPED) == "linha a\nlinha b"
    assert pane_state("carregando…") == "starting"
    assert (
        pane_state(PANE_NUMBERED_REPLY) == "ready"
    )  # lista numerada na resposta não é diálogo


def test_dialogo_ask_user_question():
    d = parse_dialog(PANE_ASK)
    assert d is not None
    assert d.options == (
        (1, "Vermelho"),
        (2, "Verde"),
        (3, "Azul"),
        (4, "Type something."),
        (5, "Chat about this"),
    )
    assert d.free == 4
    assert d.excerpt.startswith("☐ Cor\nQual cor você escolhe?")
    assert "Use a ferramenta" not in d.excerpt and SEP not in d.excerpt
    assert d.excerpt.endswith("Esc to cancel")
    assert pane_state(PANE_ASK) == "dialog"


def test_dialogo_mesmo_com_cursor_em_outra_opcao_tem_a_mesma_chave():
    assert parse_dialog(PANE_ASK_CURSOR3).key == parse_dialog(PANE_ASK).key
    other = PANE_ASK.replace("Qual cor", "Qual fruta")
    assert parse_dialog(other).key != parse_dialog(PANE_ASK).key


def test_dialogo_permissao_e_confianca():
    p = parse_dialog(PANE_PERMISSION)
    assert [n for n, _ in p.options] == [1, 2, 3] and p.free is None
    assert "Do you want to proceed?" in p.excerpt and "rm -rf build" in p.excerpt
    assert "● Bash" not in p.excerpt
    t = parse_dialog(PANE_TRUST)
    assert t.options == ((1, "Yes, I trust this folder"), (2, "No, exit"))
    assert "Quick safety check" in t.excerpt


def test_sem_dialogo():
    assert parse_dialog(PANE_READY) is None
    assert parse_dialog(PANE_BUSY) is None
    assert parse_dialog(PANE_NUMBERED_REPLY) is None


# ---------- .jsonl ----------


def user(text, kind="human", **kw):
    d = {
        "type": "user",
        "isSidechain": False,
        "message": {"role": "user", "content": text},
        "origin": {"kind": kind},
        "promptSource": "typed" if kind == "human" else "system",
    }
    d.update(kw)
    return d


def tool_result():
    return {
        "type": "user",
        "message": {
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "x"}]
        },
    }


def assistant(*blocks, stop="end_turn", **kw):
    d = {
        "type": "assistant",
        "isSidechain": False,
        "message": {"stop_reason": stop, "content": list(blocks)},
    }
    d.update(kw)
    return d


def text(t):
    return {"type": "text", "text": t}


def tool(name="Bash", **inp):
    return {"type": "tool_use", "id": "t1", "name": name, "input": inp}


def system(sub, **kw):
    return {"type": "system", "subtype": sub, "isSidechain": False, **kw}


def feed_all(tr, lines):
    out = []
    for d in lines:
        out += tr.feed(d)
    return out


def test_turno_completo_fim_por_stop_hook_summary():
    tr = TurnTracker()
    evs = feed_all(
        tr,
        [
            {"type": "file-history-snapshot"},
            user("responda só OK"),
            {"type": "attachment"},
            assistant({"type": "thinking", "thinking": ""}, stop="tool_use"),
            assistant(tool(command="ls -la"), stop="tool_use"),
            tool_result(),
            assistant(text("parcial"), stop="tool_use"),
            assistant(tool("Read", file_path="/x/y.py"), stop="tool_use"),
            tool_result(),
            assistant({"type": "thinking", "thinking": ""}),
            assistant(text("OK")),
            {"type": "last-prompt"},
            system("stop_hook_summary", hookCount=2, preventedContinuation=False),
            system("turn_duration", durationMs=3567),
        ],
    )
    kinds = [e.kind for e in evs]
    assert kinds == ["prompt", "tool", "text", "tool", "text", "end"]
    assert evs[0].human and evs[0].raw == "responda só OK"
    assert evs[1].tool == "Bash" and evs[1].detail == "ls -la"
    assert evs[-1].text == "OK"  # só o texto depois da última ferramenta
    assert not tr.in_turn


def test_fim_por_turn_duration_sem_stop_hook():
    tr = TurnTracker()
    evs = feed_all(
        tr,
        [user("oi"), assistant(text("olá")), system("turn_duration", durationMs=950)],
    )
    assert (
        evs[-1].kind == "end" and evs[-1].text == "olá" and evs[-1].duration_ms == 950
    )


def test_erro_de_api_fecha_o_turno_com_erro():
    tr = TurnTracker()
    evs = feed_all(
        tr,
        [
            user("oi"),
            assistant(
                text("You've hit your weekly limit · resets 6pm"),
                stop="stop_sequence",
                isApiErrorMessage=True,
                error="rate_limit",
            ),
        ],
    )
    assert evs[-1].kind == "end" and evs[-1].is_error
    assert "weekly limit" in evs[-1].text


def test_interrompido_fecha_sem_texto_e_prompt_novo_fecha_o_anterior():
    tr = TurnTracker()
    evs = feed_all(
        tr,
        [
            user("a"),
            assistant(tool(), stop="tool_use"),
            user("[Request interrupted by user for tool use]"),
        ],
    )
    assert [e.kind for e in evs] == ["prompt", "tool", "end"] and evs[-1].text == ""
    assert feed_all(tr, [user("[Request interrupted by user]")]) == []
    evs = feed_all(tr, [user("x"), assistant(text("meio"), stop=None), user("y")])
    assert [e.kind for e in evs] == ["prompt", "text", "end", "prompt"]


def test_origens_nao_humanas_e_lixo():
    tr = TurnTracker()
    evs = feed_all(
        tr,
        [
            user(
                "<task-notification>\n<task-id>b2</task-id>", kind="task-notification"
            ),
            assistant(text("fim")),
            system("stop_hook_summary"),
            user("Another Claude session sent a message: oi", kind="peer", isMeta=True),
        ],
    )
    assert [e.kind for e in evs] == ["prompt", "text", "end", "prompt"]
    assert not evs[0].human and not evs[-1].human
    assert (
        tr.feed("não é dict") == []
        and tr.feed({"type": "user", "isSidechain": True}) == []
    )
    assert tr.feed({"type": "assistant", "message": "x"}) == []


def test_sem_origin_usa_prompt_source():
    tr = TurnTracker()
    d = {"type": "user", "message": {"content": [text("oi")]}, "promptSource": "queued"}
    (ev,) = tr.feed(d)
    assert ev.human and ev.text == "oi"
    tr.reset()
    (ev,) = tr.feed({"type": "user", "message": {"content": "x"}, "isMeta": True})
    assert not ev.human


def test_bridge_status_da_o_link():
    tr = TurnTracker()
    (ev,) = tr.feed(
        system("bridge_status", url=URL, content="/remote-control is active")
    )
    assert ev.kind == "url" and ev.text == URL
    assert tr.feed(system("bridge_status", url="https://evil.example/x")) == []


def test_force_end():
    tr = TurnTracker()
    assert tr.force_end() == []
    feed_all(tr, [user("a"), assistant(text("b"))])
    (ev,) = tr.force_end()
    assert ev.text == "b" and not tr.in_turn


def test_prompt_matches():
    assert prompt_matches("responda só OK", "responda só OK")
    assert prompt_matches("a\n  b", "a b")
    assert prompt_matches("!ls", " !ls")
    assert prompt_matches("x" * 500, '\n\n<pasted_content id="fc08">\nxxx')
    assert not prompt_matches("oi", "outra coisa")
    assert not prompt_matches("", "")


def test_jsonl_tail_le_so_linhas_completas_e_segue_do_offset(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_bytes(b'{"a":1}\n{"b":')
    t = JsonlTail(p, 0)
    assert t.read() == [{"a": 1}]
    with open(p, "ab") as f:
        f.write(b'2}\nlixo\n[1]\n{"c":3}\n')
    assert t.read() == [{"b": 2}, {"c": 3}]
    assert t.read() == []
    p.write_bytes(b"")  # truncado: recomeça do fim
    assert t.read() == [] and t.offset == 0
    assert JsonlTail(tmp_path / "nao.jsonl").read() == []


def test_jsonl_tail_a_partir_do_offset(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_bytes(b'{"velho":1}\n')
    t = JsonlTail(p, hosted.file_size(p))
    with open(p, "ab") as f:
        f.write(b'{"novo":1}\n')
    assert t.read() == [{"novo": 1}]
    assert hosted.file_size(tmp_path / "nao") == 0


def test_last_rc_url(tmp_path):
    p = tmp_path / "s.jsonl"
    lines = [
        system("bridge_status", url=URL + "OLD"),
        user("x"),
        system("bridge_status", url=URL),
    ]
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    assert last_rc_url(p) == URL
    assert last_rc_url(tmp_path / "nao") is None


# ---------- objetos vivos ----------


async def test_hosted_turn_entrega_ate_o_result():
    turn = HostedTurn("oi")
    turn.push(Event("tool", tool="Bash"))
    turn.push(Event("result", text="ok"))
    turn.push(Event("text", text="depois"))
    got = [e.kind async for e in turn.events()]
    assert got == ["tool", "result"]
    assert turn.returncode is None and not turn.timed_out and turn.stderr_tail() == ""


async def test_hosted_turn_close_encerra():
    turn = HostedTurn("oi")
    turn.close()
    assert [e async for e in turn.events()] == []


async def test_host_active_e_stop(tmp_path):
    host = Host(SID, "/p", "personal", tmp_path, JsonlTail(tmp_path / "x.jsonl"))
    assert host.name == "cc-2964a863" and host.hosted
    assert not host.active
    host.ours = HostedTurn("x")
    assert host.active
    host.ours = None
    host.tracker.in_turn = True
    assert host.active
    host.alive = False
    assert not host.active
    called = []

    async def cb(h):
        called.append(h)

    host.stop_cb = cb
    await host.stop()
    assert called == [host]
    host.pause_timeout()
    host.resume_timeout()


@pytest.mark.parametrize("pane", [PANE_ASK, PANE_PERMISSION, PANE_TRUST])
def test_excerpt_limitado(pane):
    assert len(parse_dialog(pane * 3).excerpt) <= hosted.EXCERPT_MAX
