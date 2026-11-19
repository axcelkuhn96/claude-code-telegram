from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from bot import sessions
from bot.sessions import (
    copy_session_to,
    encode_cwd,
    list_recent,
    load_projects,
)


@dataclass
class FakeProfile:
    name: str
    config_dir: Path


CWD = "/home/you/project-b"
ENC = "-home-you-project-b"


def sid() -> str:
    return str(uuid.uuid4())


def write_session(
    config_dir: Path,
    session_id: str,
    lines: list,
    mtime: float,
    enc: str = ENC,
) -> Path:
    d = getattr(config_dir, "config_dir", config_dir) / "projects" / enc
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{session_id}.jsonl"
    p.write_text(
        "\n".join(l if isinstance(l, str) else json.dumps(l) for l in lines) + "\n",
        encoding="utf-8",
    )
    os.utime(p, (mtime, mtime))
    return p


def user(content, **extra) -> dict:
    return {"type": "user", "message": {"role": "user", "content": content}, **extra}


@pytest.fixture
def accounts(tmp_path):
    a = FakeProfile("claude", tmp_path / "a")
    b = FakeProfile("work", tmp_path / "b")
    return [a, b]


@pytest.fixture
def index(tmp_path):
    return tmp_path / "idx.json"  # ausente por padrão


# ---------- encode_cwd ----------


@pytest.mark.parametrize(
    "cwd,expected",
    [
        ("/home/you/project-b", "-home-you-project-b"),
        ("/home/you/project-c", "-home-you-project-c"),
        (
            "/home/you/project-c/.claude/worktrees/feature-1",
            "-home-you-project-c--claude-worktrees-feature-1",
        ),
        ("/srv/app.v2/N2ZODiaF38", "-srv-app-v2-N2ZODiaF38"),
        ("/home/you/a_b", "-home-you-a-b"),
        ("/", "-"),
    ],
)
def test_encode_cwd(cwd, expected):
    assert encode_cwd(cwd) == expected


# ---------- load_projects ----------


def test_load_projects_ordem_unicos_e_til(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "claude_profiles: []\n"
        "workspaces:\n"
        "- name: project-a\n  cwd: ~/project-a\n  terminals: []\n"
        "- name: cobra\n  cwd: ~/project-b\n"
        "- name: project-a2\n  cwd: ~/project-a\n"
        "- name: abs\n  cwd: /opt/proj\n"
        "- name: sem-cwd\n  cwd: null\n"
        "- name: omitido\n",
        encoding="utf-8",
    )
    home = tmp_path / "h"
    assert load_projects(cfg, home=home) == [
        str(home / "project-a"),
        str(home / "project-b"),
        "/opt/proj",
    ]


def test_load_projects_ausente_invalido_ou_sem_workspaces(tmp_path):
    assert load_projects(tmp_path / "nao-existe.yaml", home=tmp_path) == []
    bad = tmp_path / "bad.yaml"
    bad.write_text("workspaces: [unclosed\n  - : :", encoding="utf-8")
    assert load_projects(bad, home=tmp_path) == []
    empty = tmp_path / "empty.yaml"
    empty.write_text("claude_profiles: []\n", encoding="utf-8")
    assert load_projects(empty, home=tmp_path) == []
    notlist = tmp_path / "nl.yaml"
    notlist.write_text("workspaces: 3\n", encoding="utf-8")
    assert load_projects(notlist, home=tmp_path) == []


# ---------- list_recent ----------


def test_list_recent_ordena_por_mtime_e_limita(accounts, index):
    a, b = accounts
    ids = [sid() for _ in range(5)]
    for i, s in enumerate(ids):
        write_session(a if i % 2 == 0 else b, s, [user(f"p{i}")], 1000 + i)
    got = list_recent(CWD, accounts, index, limit=3)
    assert [g.session_id for g in got] == [ids[4], ids[3], ids[2]]
    assert [g.account for g in got] == ["claude", "work", "claude"]
    assert got[0].config_dir == a.config_dir
    assert got[0].cwd == CWD
    assert got[0].mtime.timestamp() == pytest.approx(1004)
    assert got[0].first_prompt == "p4"


def test_list_recent_limit_padrao_8(accounts, index):
    a, _ = accounts
    for i in range(10):
        write_session(a, sid(), [user("x")], 1000 + i)
    assert len(list_recent(CWD, accounts, index)) == 8


def test_list_recent_dedup_vence_o_mais_recente(accounts, index):
    a, b = accounts
    s = sid()
    write_session(a, s, [user("antigo")], 1000)
    write_session(b, s, [user("novo")], 2000)
    got = list_recent(CWD, accounts, index)
    assert len(got) == 1
    assert got[0].account == "work"
    assert got[0].first_prompt == "novo"


def test_list_recent_empate_usa_indice_de_contas(accounts, tmp_path):
    a, b = accounts
    s = sid()
    write_session(a, s, [user("x")], 1500)
    write_session(b, s, [user("x")], 1500)
    idx = tmp_path / "idx.json"
    idx.write_text(
        json.dumps(
            {
                "version": 1,
                "sessions": {
                    s: {"profile": "work", "config_dir": str(b.config_dir), "ts": 1}
                },
            }
        ),
        encoding="utf-8",
    )
    assert list_recent(CWD, accounts, idx)[0].account == "work"
    # sem índice (ou índice apontando conta desconhecida) -> primeiro perfil
    idx.write_text(
        json.dumps({"version": 1, "sessions": {s: {"profile": "fantasma"}}}),
        encoding="utf-8",
    )
    assert list_recent(CWD, accounts, idx)[0].account == "claude"
    idx.write_text("{corrompido", encoding="utf-8")
    assert list_recent(CWD, accounts, idx)[0].account == "claude"


def test_list_recent_ignora_nao_uuid_outro_projeto_e_pasta_ausente(accounts, index):
    a = accounts[0]
    ok = sid()
    write_session(a, ok, [user("ok")], 1000)
    write_session(a, "nao-e-uuid", [user("x")], 2000)
    write_session(a, sid(), [user("outro")], 3000, enc="-home-you-project-a")
    (a.config_dir / "projects" / ENC / "notas.txt").write_text("x")
    got = list_recent(CWD, accounts, index)  # b nem tem a pasta
    assert [g.session_id for g in got] == [ok]


def test_list_recent_jsonl_vazio_tem_first_prompt_vazio(accounts, index):
    a, _ = accounts
    s = sid()
    p = a.config_dir / "projects" / ENC
    p.mkdir(parents=True)
    (p / f"{s}.jsonl").write_text("")
    got = list_recent(CWD, accounts, index)
    assert got[0].first_prompt == ""


# ---------- first_prompt ----------


def first_prompt_of(accounts, index, lines) -> str:
    write_session(
        accounts[0],
        sid(),
        lines,
        1000,
    )
    return list_recent(CWD, accounts[:1], index)[0].first_prompt


def test_first_prompt_content_string(accounts, index):
    assert (
        first_prompt_of(accounts, index, [user("  oi,   tudo\nbem?  ")])
        == "oi, tudo bem?"
    )


def test_first_prompt_lista_de_blocos_pega_o_texto(accounts, index):
    lines = [
        user([{"type": "image", "source": {}}, {"type": "text", "text": "veja isso"}])
    ]
    assert first_prompt_of(accounts, index, lines) == "veja isso"


def test_first_prompt_pula_meta_comando_sistema_toolresult_imagem(accounts, index):
    lines = [
        {"type": "mode", "mode": "normal"},
        user("<local-command-caveat>x</local-command-caveat>"),
        user("<task-notification>x</task-notification>"),
        user("<system-reminder>x</system-reminder>"),
        user("<system-reminder>x</system-reminder>", isMeta=True),
        user("texto meta", isMeta=True),
        user("resumo", isCompactSummary=True),
        user([{"type": "tool_result", "tool_use_id": "t", "content": "saida"}]),
        user("texto sidechain", isSidechain=True),
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "resposta"}]},
        },
        user("o prompt de verdade"),
        user("outro"),
    ]
    assert first_prompt_of(accounts, index, lines) == "o prompt de verdade"


def test_first_prompt_trunca_em_50(accounts, index):
    got = first_prompt_of(accounts, index, [user("a" * 200)])
    assert len(got) <= 51
    assert got.startswith("a" * 50)


def test_first_prompt_ignora_linha_json_invalida(accounts, index):
    lines = ["{nao e json", user("depois do lixo")]
    assert first_prompt_of(accounts, index, lines) == "depois do lixo"


def test_first_prompt_sem_prompt_de_usuario(accounts, index):
    lines = [{"type": "assistant", "message": {"content": "x"}}]
    assert first_prompt_of(accounts, index, lines) == ""


# ---------- copy_session_to ----------


@pytest.fixture
def dirs(tmp_path):
    (tmp_path / "dst").mkdir()  # config dir da conta de destino já existe
    return tmp_path / "src", tmp_path / "dst"


def test_copy_copied_cria_pasta_e_preserva_origem(dirs):
    src, dst = dirs
    s = sid()
    p = write_session(src, s, [user("conteudo")], 1000)
    before = p.read_bytes()
    assert copy_session_to(s, CWD, src, dst) == "copied"
    d = dst / "projects" / ENC / f"{s}.jsonl"
    assert d.read_bytes() == before
    assert p.read_bytes() == before
    assert d.stat().st_mtime == pytest.approx(1000)  # copy2 preserva mtime
    assert sorted(x.name for x in d.parent.iterdir()) == [f"{s}.jsonl"]


def test_copy_updated_quando_destino_mais_antigo(dirs):
    src, dst = dirs
    s = sid()
    write_session(src, s, [user("novo")], 2000)
    old = write_session(dst, s, [user("velho")], 1000)
    assert copy_session_to(s, CWD, src, dst) == "updated"
    assert "novo" in old.read_text()
    assert sorted(x.name for x in old.parent.iterdir()) == [f"{s}.jsonl"]


def test_copy_kept_quando_destino_mais_novo_ou_igual(dirs):
    src, dst = dirs
    s = sid()
    write_session(src, s, [user("origem")], 1000)
    newer = write_session(dst, s, [user("destino")], 2000)
    assert copy_session_to(s, CWD, src, dst) == "kept"
    assert "destino" in newer.read_text()
    os.utime(newer, (1000, 1000))
    assert copy_session_to(s, CWD, src, dst) == "kept"
    assert "destino" in newer.read_text()


def test_copy_idempotente(dirs):
    src, dst = dirs
    s = sid()
    write_session(src, s, [user("x")], 1000)
    assert copy_session_to(s, CWD, src, dst) == "copied"
    assert copy_session_to(s, CWD, src, dst) == "kept"


@pytest.mark.parametrize(
    "bad",
    [
        "../x",
        "nao-uuid",
        "",
        "..",
        "a/b",
        str(uuid.uuid4()) + "/../x",
        str(uuid.uuid4()).upper() + "x",
    ],
)
def test_copy_recusa_session_id_invalido(dirs, bad):
    src, dst = dirs
    with pytest.raises(ValueError):
        copy_session_to(bad, CWD, src, dst)
    assert not (dst / "projects").exists()


def test_copy_origem_ausente(dirs):
    src, dst = dirs
    with pytest.raises(FileNotFoundError):
        copy_session_to(sid(), CWD, src, dst)
    assert not (dst / "projects").exists()


def test_copy_recusa_origem_symlink_para_fora(dirs, tmp_path):
    src, dst = dirs
    s = sid()
    d = src / "projects" / ENC
    d.mkdir(parents=True)
    outside = tmp_path / "segredo.txt"
    outside.write_text("segredo")
    (d / f"{s}.jsonl").symlink_to(outside)
    with pytest.raises(ValueError):
        copy_session_to(s, CWD, src, dst)
    assert not (dst / "projects").exists()


def test_copy_cria_pastas_com_modo_0700(dirs):
    src, dst = dirs
    s = sid()
    write_session(src, s, [user("x")], 1000)
    copy_session_to(s, CWD, src, dst)
    assert (dst / "projects").stat().st_mode & 0o777 == 0o700
    assert (dst / "projects" / ENC).stat().st_mode & 0o777 == 0o700


def test_copy_exige_dst_dir_existente(tmp_path):
    src = tmp_path / "src"
    s = sid()
    write_session(src, s, [user("x")], 1000)
    with pytest.raises(FileNotFoundError):
        copy_session_to(s, CWD, src, tmp_path / "nao-existe")
    assert not (tmp_path / "nao-existe").exists()


@pytest.mark.parametrize("cwd", ["", "   "])
def test_copy_recusa_cwd_vazio(dirs, cwd):
    src, dst = dirs
    with pytest.raises(ValueError):
        copy_session_to(sid(), cwd, src, dst)
    assert not (dst / "projects").exists()


def test_copy_recusa_pasta_destino_symlink_para_fora(dirs, tmp_path):
    src, dst = dirs
    s = sid()
    write_session(src, s, [user("x")], 1000)
    outside = tmp_path / "fora"
    outside.mkdir()
    (dst / "projects").mkdir()
    (dst / "projects" / ENC).symlink_to(outside)
    with pytest.raises(ValueError):
        copy_session_to(s, CWD, src, dst)
    assert list(outside.iterdir()) == []


def test_copy_uuid_maiusculo_puro_recusado(dirs):
    src, dst = dirs
    s = sid().upper()
    write_session(src, s.lower(), [user("x")], 1000)
    with pytest.raises(ValueError):
        copy_session_to(s, CWD, src, dst)


def test_copy_mesma_pasta_devolve_kept_sem_copiar(tmp_path):
    base = tmp_path / "a"
    s = sid()
    p = write_session(base, s, [user("x")], 1000)
    assert copy_session_to(s, CWD, base, base) == "kept"
    assert sorted(x.name for x in p.parent.iterdir()) == [f"{s}.jsonl"]


def test_copy_projects_compartilhado_por_symlink_devolve_kept(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    s = sid()
    write_session(a, s, [user("x")], 1000)
    b.mkdir()
    (b / "projects").symlink_to(a / "projects")
    assert copy_session_to(s, CWD, a, b) == "kept"
    assert sorted(x.name for x in (a / "projects" / ENC).iterdir()) == [f"{s}.jsonl"]


def test_copy_mesma_pasta_com_origem_ausente_continua_erro(tmp_path):
    base = tmp_path / "a"
    base.mkdir()
    with pytest.raises(FileNotFoundError):
        copy_session_to(sid(), CWD, base, base)


# ---------- projects/ compartilhado entre contas (symlink) ----------


@pytest.fixture
def shared(tmp_path):
    a = FakeProfile("claude", tmp_path / "a")
    b = FakeProfile("work", tmp_path / "b")
    c = FakeProfile("personal", tmp_path / "c")
    for x in (b, c):
        x.config_dir.mkdir()
        (x.config_dir / "projects").symlink_to(a.config_dir / "projects")
    return [a, b, c]


def test_list_recent_projects_compartilhado_dedup_e_conta_do_indice(shared, tmp_path):
    s = sid()
    write_session(shared[0], s, [user("compartilhada")], 1000)
    idx = tmp_path / "idx.json"
    idx.write_text(
        json.dumps({"version": 1, "sessions": {s: {"profile": "personal"}}}),
        encoding="utf-8",
    )
    got = list_recent(CWD, shared, idx)
    assert len(got) == 1
    assert got[0].account == "personal"
    assert got[0].config_dir == shared[2].config_dir
    assert got[0].first_prompt == "compartilhada"


def test_list_recent_projects_compartilhado_sem_indice_conta_none(shared, tmp_path):
    s = sid()
    write_session(shared[0], s, [user("x")], 1000)
    for idx_content in (None, "{corrompido", json.dumps({"sessions": {s: {"ts": 1}}})):
        idx = tmp_path / "idx2.json"
        if idx_content is not None:
            idx.write_text(idx_content, encoding="utf-8")
        got = list_recent(CWD, shared, idx)
        assert len(got) == 1
        assert got[0].account is None
        assert got[0].config_dir == shared[0].config_dir


# ---------- M1 / M4 ----------


def test_first_prompt_varre_todos_os_blocos_de_texto(accounts, index):
    lines = [
        user(
            [
                {"type": "text", "text": "<system-reminder>x</system-reminder>"},
                {"type": "text", "text": "[Image: source: /x.png]"},
                {"type": "text", "text": "texto real"},
            ]
        )
    ]
    assert first_prompt_of(accounts, index, lines) == "texto real"


def test_load_projects_loga_warning_se_ilegivel(tmp_path, caplog):
    bad = tmp_path / "bad.yaml"
    bad.write_text("workspaces: [unclosed\n  - : :", encoding="utf-8")
    with caplog.at_level("WARNING", logger="bot.sessions"):
        assert load_projects(bad, home=tmp_path) == []
        assert load_projects(tmp_path / "nao-existe.yaml", home=tmp_path) == []
    assert len(caplog.records) == 2


# ---------- clean_prompt / last_prompt ----------

TEAMMATE = (
    'Another Claude session sent a message:\n<teammate-message teammate_id="w1" '
    'summary="x">\n{}\n</teammate-message>'
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        (TEAMMATE.format("Faz o X agora"), "Faz o X agora"),
        (TEAMMATE.format("  "), ""),
        (
            'Another Claude session sent a message:\n<teammate-message teammate_id="w1">\nsem fechar',
            "sem fechar",
        ),
        ("Another Claude session sent a message: oi", "oi"),
        (
            'veja <pasted_content id="1">muito\ntexto\n</pasted_content> por favor',
            "veja [colado] por favor",
        ),
        ('<pasted_content id="2">sem fechar de verdade', "[colado]"),
        (
            (
                "<command-message>foo</command-message>\n<command-name>/foo</command-name>\n"
                "<command-args>bar  baz</command-args>"
            ),
            "/foo bar baz",
        ),
        ("<command-name>/clear</command-name>", "/clear"),
        ("oi<system-reminder>lixo\nlixo</system-reminder> tudo bem", "oi tudo bem"),
        ("<system-reminder>so lixo</system-reminder>", ""),
        ("<system-reminder>sem fechar\nlixo", ""),
        ("olha [Image #3] e [Image: source: /x/y.png] ok", "olha 🖼 e 🖼 ok"),
        ("[Image #1]", "🖼"),
        ("  muito\n\n   espaço\t aqui  ", "muito espaço aqui"),
        ("<task-notification>x</task-notification>", ""),
        ("<local-command-stdout>x", ""),
        ("", ""),
        ("   ", ""),
    ],
)
def test_clean_prompt(raw, expected):
    assert sessions.clean_prompt(raw) == expected


def test_clean_prompt_nao_str():
    assert sessions.clean_prompt(None) == ""  # type: ignore[arg-type]


def test_first_prompt_limpo(accounts, index):
    lines = [
        user("<system-reminder>x</system-reminder>"),
        user(TEAMMATE.format("revisa " + "o" * 100)),
    ]
    got = first_prompt_of(accounts, index, lines)
    assert got == "revisa " + "o" * 43 + "…"


def test_first_prompt_so_imagem_vira_icone(accounts, index):
    assert first_prompt_of(accounts, index, [user("[Image #1]")]) == "🖼"


def test_first_prompt_prefere_texto_a_icone_em_blocos(accounts, index):
    lines = [
        user(
            [
                {"type": "text", "text": "[Image #1]"},
                {"type": "text", "text": "descreve a imagem"},
            ]
        )
    ]
    assert first_prompt_of(accounts, index, lines) == "descreve a imagem"


def assistant_line(text: str = "resp") -> dict:
    return {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    }


def make_file(tmp_path, lines) -> Path:
    p = tmp_path / "s.jsonl"
    p.write_text(
        "\n".join(x if isinstance(x, str) else json.dumps(x) for x in lines) + "\n",
        encoding="utf-8",
    )
    return p


def test_last_prompt_ultimo_do_usuario_limpo_e_cortado_em_60(tmp_path):
    p = make_file(
        tmp_path,
        [
            user("primeiro"),
            assistant_line(),
            user(TEAMMATE.format("z" * 200)),
            assistant_line(),
        ],
    )
    got = sessions.last_prompt(p)
    assert got == "z" * 59 + "…"
    assert len(got) == 60


def test_last_prompt_pula_vazios_olhando_para_tras(tmp_path):
    p = make_file(
        tmp_path,
        [
            user("prompt antigo valido"),
            assistant_line(),
            user("<system-reminder>x</system-reminder>"),
            user("<task-notification>y</task-notification>"),
            user(TEAMMATE.format("   ")),
            user("meta", isMeta=True),
            user([{"type": "tool_result", "tool_use_id": "t", "content": "saida"}]),
            assistant_line("resposta"),
        ],
    )
    assert sessions.last_prompt(p) == "prompt antigo valido"


def test_last_prompt_sem_prompt_ou_arquivo_ausente(tmp_path):
    p = make_file(tmp_path, [assistant_line(), {"type": "mode", "mode": "x"}])
    assert sessions.last_prompt(p) == ""
    assert sessions.last_prompt(tmp_path / "nao-existe.jsonl") == ""


def test_last_prompt_le_so_o_final_do_arquivo(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "TAIL_STAGES", (2000,))
    fillers = [assistant_line("f" * 80) for _ in range(100)]
    perto = make_file(
        tmp_path, [user("do comeco"), *fillers, user("do fim"), *fillers[:5]]
    )
    assert sessions.last_prompt(perto) == "do fim"
    # último prompt além do teto: cai no primeiro (a linha cortada não quebra nada)
    longe = tmp_path / "longe.jsonl"
    longe.write_text(
        "\n".join(json.dumps(x) for x in [user("do comeco"), *fillers]) + "\n",
        encoding="utf-8",
    )
    assert sessions.last_prompt(longe) == "do comeco"


def lines_bytes(lines) -> list[bytes]:
    return [json.dumps(x).encode() + b"\n" for x in lines]


def test_last_prompt_le_para_tras_progressivamente(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "TAIL_STAGES", (2000, 20_000, 200_000))
    fillers = [assistant_line("f" * 80) for _ in range(300)]  # ~30 KB
    # o último prompt está a ~15 KB do fim: fora do 1º estágio, dentro do 2º
    p = make_file(
        tmp_path,
        [user("primeiro"), *fillers[:150], user("o ultimo"), *fillers[150:200]],
    )
    assert sessions.last_prompt(p) == "o ultimo"


def test_last_prompt_linha_na_fronteira_do_estagio_e_lida_inteira(
    tmp_path, monkeypatch
):
    fillers = [assistant_line("f" * 80) for _ in range(60)]
    prompt = user("prompt cruzando a fronteira " + "x" * 400)
    head = lines_bytes([user("primeiro"), *fillers])
    mid = lines_bytes([prompt])
    tail = lines_bytes(fillers[:20])
    p = tmp_path / "s.jsonl"
    p.write_bytes(b"".join(head + mid + tail))
    # fronteira do 1º estágio cai no meio da linha do prompt
    stage1 = sum(len(x) for x in tail) + len(mid[0]) // 2
    monkeypatch.setattr(sessions, "TAIL_STAGES", (stage1, stage1 * 3, 10**7))
    assert sessions.last_prompt(p) == "prompt cruzando a fronteira " + "x" * 31 + "…"


def test_last_prompt_alem_do_teto_cai_no_primeiro_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "TAIL_STAGES", (1000, 3000))
    fillers = [assistant_line("f" * 80) for _ in range(200)]
    p = make_file(
        tmp_path,
        [user("primeiro"), *fillers[:100], user("ultimo mas longe"), *fillers[100:]],
    )
    assert sessions.last_prompt(p) == "primeiro"


def test_last_prompt_arquivo_inteiro_sem_prompt_nao_reescaneia(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "TAIL_STAGES", (500, 5000, 50_000))

    def boom(_):
        raise AssertionError("first_prompt não deve ser chamado")

    monkeypatch.setattr(sessions, "first_prompt", boom)
    p = make_file(tmp_path, [assistant_line("f" * 80) for _ in range(20)])
    assert sessions.last_prompt(p) == ""


def test_last_prompt_progressivo_tem_memoria_limitada(tmp_path):
    import tracemalloc

    big = tmp_path / "big.jsonl"
    filler = json.dumps(assistant_line("f" * 1000))
    with big.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(user("do comeco")) + "\n")
        for _ in range(20_000):  # ~20 MB
            fh.write(filler + "\n")
        fh.write(json.dumps(user("do meio do fim")) + "\n")
        for _ in range(2_500):  # ~2.5 MB depois do prompt: fora do 1º e do 2º estágio
            fh.write(filler + "\n")
    assert big.stat().st_size > 20_000_000
    tracemalloc.start()
    try:
        got = sessions.last_prompt(big)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert got == "do meio do fim"
    assert peak < 15_000_000


def test_last_prompt_nao_carrega_o_arquivo_inteiro(tmp_path):
    import tracemalloc

    big = tmp_path / "big.jsonl"
    filler = json.dumps(assistant_line("f" * 1000))
    with big.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(user("do comeco")) + "\n")
        for _ in range(6000):  # ~6 MB
            fh.write(filler + "\n")
        fh.write(json.dumps(user("do fim")) + "\n")
    assert big.stat().st_size > 5_000_000
    tracemalloc.start()
    try:
        got = sessions.last_prompt(big)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert got == "do fim"
    assert peak < 2_000_000


def test_list_recent_preenche_last_prompt_e_first_prompt_limpos(accounts, index):
    a, _ = accounts
    s = sid()
    write_session(
        a,
        s,
        [
            user("<system-reminder>x</system-reminder>"),
            user("<command-name>/clear</command-name>"),
            assistant_line(),
            user(TEAMMATE.format("ultima instrucao")),
            assistant_line(),
        ],
        1000,
    )
    got = list_recent(CWD, accounts, index)[0]
    assert got.first_prompt == "/clear"
    assert got.last_prompt == "ultima instrucao"


def test_sessioninfo_last_prompt_tem_default_vazio():
    info = sessions.SessionInfo(
        session_id="x",
        account=None,
        config_dir=Path("/x"),
        cwd="/c",
        mtime=sessions.datetime.now(sessions.UTC),
        first_prompt="p",
    )
    assert info.last_prompt == ""


def test_modulo_nao_importa_usage():
    # T3 usa só .name/.config_dir; não deve depender de bot.usage
    assert "usage" not in sessions.__dict__
