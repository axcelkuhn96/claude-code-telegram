"""C1 (onda 4): histórico de uma sessão a partir do .jsonl. Fixtures 100% sintéticas."""

from __future__ import annotations

import json
import os
import tracemalloc
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bot import sessions
from bot.sessions import Turn, last_exchange, read_history, session_file

CWD = "/home/you/Projeto"
ENC = "-home-you-Projeto"


@dataclass
class FakeProfile:
    name: str
    config_dir: Path


def sid() -> str:
    return str(uuid.uuid4())


def ts(n: int) -> str:
    return f"2026-11-19T12:{n // 60:02d}:{n % 60:02d}.000Z"


def u(text, n=0, **extra) -> dict:
    return {
        "type": "user",
        "message": {"role": "user", "content": text},
        "timestamp": ts(n),
        **extra,
    }


def a(text, n=0) -> dict:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        "timestamp": ts(n),
    }


def tool_use(tid, name, n=0, **inp) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}],
        },
        "timestamp": ts(n),
    }


def tool_result(tid, content, n=0) -> dict:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": tid, "content": content}
            ],
        },
        "toolUseResult": {"stdout": "x"},
        "timestamp": ts(n),
    }


def write(path: Path, lines: list) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(x if isinstance(x, str) else json.dumps(x) for x in lines) + "\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def f(tmp_path):
    return tmp_path / "s.jsonl"


def simple(n: int) -> list:
    """n turnos alternando user/assistant: u0 a1 u2 a3 ..."""
    return [u(f"t{i}", i) if i % 2 == 0 else a(f"t{i}", i) for i in range(n)]


# ---------- read_history: o que vira turno ----------


def test_conversa_basica_em_ordem_cronologica(f):
    write(f, [u("oi", 1), a("olá", 2), u("faz X", 3), a("feito", 4)])
    turns, cursor = read_history(f)
    assert [(t.role, t.text) for t in turns] == [
        ("user", "oi"),
        ("assistant", "olá"),
        ("user", "faz X"),
        ("assistant", "feito"),
    ]
    assert cursor is None
    assert turns[0].tool is None
    assert turns[0].ts == datetime(2026, 11, 19, 12, 0, 1, tzinfo=UTC)


def test_user_com_lista_de_blocos_junta_textos_validos(f):
    write(
        f,
        [
            u(
                [
                    {"type": "text", "text": "veja isso"},
                    {"type": "image", "source": {}},
                    {"type": "text", "text": "[Image: source: /x.png]"},
                    {"type": "text", "text": "<system-reminder>x</system-reminder>"},
                    {"type": "text", "text": "e isso"},
                ]
            )
        ],
    )
    turns, _ = read_history(f)
    assert [t.text for t in turns] == ["veja isso\n\ne isso"]


def test_ignora_ruido(f):
    write(
        f,
        [
            "",
            "{nao e json",
            {"type": "mode", "mode": "normal"},
            {"type": "queue-operation", "operation": "enqueue", "content": "fila"},
            {"type": "attachment", "attachment": {"type": "hook_success"}},
            {"type": "system", "subtype": "stop_hook_summary", "content": "hook"},
            {"type": "ai-title", "aiTitle": "titulo"},
            {"type": "last-prompt", "lastPrompt": "x"},
            u("<command-name>/clear</command-name>"),
            u("<local-command-caveat>x</local-command-caveat>"),
            u("<system-reminder>x</system-reminder>"),
            u("[Image: source: /tmp/x.png]"),
            u("meta injetada", isMeta=True),
            u("lateral", isSidechain=True),
            u("resumo da compactação", isCompactSummary=True),
            u("   "),
            {
                "type": "assistant",
                "message": {"content": [{"type": "thinking", "thinking": "pensando"}]},
            },
            tool_use("t1", "Read", file_path="/x"),
            a("   "),
            {
                "type": "assistant",
                "isSidechain": True,
                "message": {"content": [{"type": "text", "text": "sub"}]},
            },
            u("prompt real", 5),
            a("resposta real", 6),
        ],
    )
    turns, cursor = read_history(f)
    assert [(t.role, t.text) for t in turns] == [
        ("user", "prompt real"),
        ("assistant", "resposta real"),
    ]
    assert cursor is None


def test_tool_result_vira_turno_tool_com_nome_e_resumo(f):
    write(
        f,
        [
            u("roda", 1),
            tool_use("t1", "Bash", 2, command="ls"),
            tool_result("t1", "linha1\nlinha2   com   espaços", 3),
            tool_use("t2", "Grep", 4, pattern="x"),
            tool_result(
                "t2",
                [{"type": "text", "text": "achou"}, {"type": "image", "source": {}}],
                5,
            ),
            tool_result("t-desconhecido", "sem uso conhecido", 6),
            tool_result("t3", [{"type": "image", "source": {}}], 7),
            a("pronto", 8),
        ],
    )
    turns, _ = read_history(f)
    assert [(t.role, t.tool, t.text) for t in turns] == [
        ("user", None, "roda"),
        ("tool", "Bash", "linha1 linha2 com espaços"),
        ("tool", "Grep", "achou [imagem]"),
        ("tool", None, "sem uso conhecido"),
        ("tool", None, "[imagem]"),
        ("assistant", None, "pronto"),
    ]
    assert turns[1].ts is not None


def test_tool_result_volumoso_e_truncado_em_200(f):
    write(f, [tool_use("t1", "Read", 1), tool_result("t1", "x" * 50_000, 2)])
    turns, _ = read_history(f)
    assert len(turns) == 1
    assert len(turns[0].text) <= 200
    assert turns[0].text.endswith("…")


def test_texto_longo_truncado_em_4000(f):
    write(f, [u("a" * 10_000, 1), a("b" * 4000, 2), a("c" * 4001, 3)])
    turns, _ = read_history(f)
    assert [len(t.text) for t in turns] == [4000, 4000, 4000]
    assert turns[0].text.endswith("…")
    assert turns[1].text == "b" * 4000
    assert turns[2].text.endswith("…")


def test_user_preserva_quebras_de_linha(f):
    write(f, [u("linha 1\nlinha 2\n\nlinha 4", 1)])
    turns, _ = read_history(f)
    assert turns[0].text == "linha 1\nlinha 2\n\nlinha 4"


def test_timestamp_ausente_ou_invalido_vira_none(f):
    line1 = u("x")
    del line1["timestamp"]
    line2 = u("y")
    line2["timestamp"] = "ontem"
    write(f, [line1, line2])
    turns, _ = read_history(f)
    assert [t.ts for t in turns] == [None, None]


def test_arquivo_ausente(tmp_path):
    assert read_history(tmp_path / "nao-existe.jsonl") == ([], None)
    assert last_exchange(tmp_path / "nao-existe.jsonl") == (None, None)


# ---------- read_history: paginação ----------


def texts(turns: list[Turn]) -> list[str]:
    return [t.text for t in turns]


def test_pagina_do_fim_e_cursor(f):
    write(f, simple(10))
    turns, cursor = read_history(f, limit=4)
    assert texts(turns) == ["t6", "t7", "t8", "t9"]
    assert cursor == 6


def test_pagina_anterior_via_before(f):
    write(f, simple(10))
    turns, cursor = read_history(f, limit=4, before=6)
    assert texts(turns) == ["t2", "t3", "t4", "t5"]
    assert cursor == 2
    turns, cursor = read_history(f, limit=4, before=2)
    assert texts(turns) == ["t0", "t1"]
    assert cursor is None


def test_percorrer_tudo_reconstroi_a_ordem(f):
    write(f, simple(23))
    pages = []
    turns, cursor = read_history(f, limit=5)
    pages.insert(0, texts(turns))
    while cursor is not None:
        turns, cursor = read_history(f, limit=5, before=cursor)
        pages.insert(0, texts(turns))
    assert [t for p in pages for t in p] == [f"t{i}" for i in range(23)]


def test_limit_maior_que_o_total_nao_tem_cursor(f):
    write(f, simple(3))
    turns, cursor = read_history(f, limit=50)
    assert texts(turns) == ["t0", "t1", "t2"]
    assert cursor is None


def test_before_zero_e_negativo_devolvem_vazio(f):
    write(f, simple(3))
    assert read_history(f, before=0) == ([], None)
    assert read_history(f, before=-5) == ([], None)


def test_before_alem_do_fim_equivale_ao_fim(f):
    write(f, simple(10))
    assert read_history(f, limit=3, before=999) == read_history(f, limit=3)


def test_limit_invalido_e_ajustado(f):
    write(f, simple(5))
    turns, cursor = read_history(f, limit=0)
    assert texts(turns) == ["t4"]
    assert cursor == 4
    turns, _ = read_history(f, limit=10_000)
    assert len(turns) == 5


def test_indices_estaveis_quando_o_arquivo_cresce(f):
    write(f, simple(10))
    _, cursor = read_history(f, limit=4)
    with f.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(a("novo", 99)) + "\n")
    # o cursor antigo continua apontando pro mesmo trecho anterior
    turns, _ = read_history(f, limit=4, before=cursor)
    assert texts(turns) == ["t2", "t3", "t4", "t5"]


def test_streaming_nao_carrega_o_arquivo_na_memoria(tmp_path):
    big = tmp_path / "big.jsonl"
    payload = "y" * 20_000
    with big.open("w", encoding="utf-8") as fh:
        for i in range(500):
            fh.write(json.dumps(u(f"p{i}", i)) + "\n")
            fh.write(json.dumps(tool_result(f"t{i}", payload, i)) + "\n")
    assert big.stat().st_size > 9_000_000
    tracemalloc.start()
    try:
        turns, cursor = read_history(big, limit=50)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(turns) == 50
    assert cursor == 950
    assert peak < 3_000_000


# ---------- last_exchange ----------


def test_last_exchange_ultimo_prompt_e_ultima_resposta(f):
    write(
        f,
        [
            u("primeiro", 1),
            a("r1", 2),
            u("segundo", 3),
            tool_use("t1", "Bash", 4),
            tool_result("t1", "saida", 5),
            a("r2a", 6),
            a("r2b", 7),
            u("<system-reminder>x</system-reminder>", 8),
            u("meta", 9, isMeta=True),
        ],
    )
    assert last_exchange(f) == ("segundo", "r2b")


def test_last_exchange_so_usuario_ou_vazio(f):
    write(f, [u("so eu", 1)])
    assert last_exchange(f) == ("so eu", None)
    write(f, [{"type": "mode", "mode": "x"}])
    assert last_exchange(f) == (None, None)
    write(f, [])
    assert last_exchange(f) == (None, None)


def test_last_exchange_trunca_em_4000(f):
    write(f, [u("p" * 9000, 1), a("r" * 9000, 2)])
    p, r = last_exchange(f)
    assert len(p) == 4000
    assert len(r) == 4000


# ---------- session_file ----------


def put(profile: FakeProfile, s: str, mtime: float, cwd_enc: str = ENC) -> Path:
    p = profile.config_dir / "projects" / cwd_enc / f"{s}.jsonl"
    write(p, [u("x")])
    os.utime(p, (mtime, mtime))
    return p


@pytest.fixture
def accounts(tmp_path):
    return [FakeProfile("a", tmp_path / "a"), FakeProfile("b", tmp_path / "b")]


def test_session_file_acha_a_mais_recente_entre_contas(accounts):
    s = sid()
    put(accounts[0], s, 1000)
    newer = put(accounts[1], s, 2000)
    assert session_file(CWD, s, accounts) == newer.resolve()


def test_session_file_unica_conta(accounts):
    s = sid()
    p = put(accounts[1], s, 1000)
    assert session_file(CWD, s, accounts) == p.resolve()


def test_session_file_ausente_ou_outro_projeto(accounts):
    s = sid()
    assert session_file(CWD, s, accounts) is None
    put(accounts[0], s, 1000, cwd_enc="-home-you-Outro")
    assert session_file(CWD, s, accounts) is None


@pytest.mark.parametrize(
    "bad", ["", "../x", "nao-uuid", "a/b", "..", str(uuid.uuid4()).upper()]
)
def test_session_file_recusa_id_invalido(accounts, bad):
    assert session_file(CWD, bad, accounts) is None


def test_session_file_cwd_vazio(accounts):
    s = sid()
    put(accounts[0], s, 1000)
    assert session_file("", s, accounts) is None


def test_session_file_recusa_symlink_para_fora(accounts, tmp_path):
    s = sid()
    d = accounts[0].config_dir / "projects" / ENC
    d.mkdir(parents=True)
    outside = tmp_path / "segredo.jsonl"
    outside.write_text("{}")
    (d / f"{s}.jsonl").symlink_to(outside)
    assert session_file(CWD, s, accounts) is None


def test_session_file_projects_compartilhado_por_symlink(accounts):
    s = sid()
    p = put(accounts[0], s, 1000)
    accounts[1].config_dir.mkdir()
    (accounts[1].config_dir / "projects").symlink_to(
        accounts[0].config_dir / "projects"
    )
    assert session_file(CWD, s, accounts) == p.resolve()


# ---------- review W2: linha gigante e thread-safety ----------


def test_linha_gigante_e_ignorada_e_indices_seguem_estaveis(f, monkeypatch):
    monkeypatch.setattr(sessions, "MAX_LINE", 1000)
    write(
        f,
        [
            u("antes", 1),
            u("g" * 50_000, 2),  # excede MAX_LINE: descartada
            a("meio", 3),
            tool_use("t1", "Bash", 4),
            tool_result("t1", "r" * 5000, 5),  # também descartada
            u("x" * 1000, 6),  # linha > MAX_LINE por causa do JSON: descartada
            a("fim", 7),
        ],
    )
    turns, cursor = read_history(f)
    assert [t.text for t in turns] == ["antes", "meio", "fim"]
    assert cursor is None
    # paginação continua coerente com o descarte
    page, cur = read_history(f, limit=2)
    assert [t.text for t in page] == ["meio", "fim"]
    assert cur == 1
    assert last_exchange(f) == ("antes", "fim")


def test_ultima_linha_gigante_sem_quebra_final(f, monkeypatch):
    monkeypatch.setattr(sessions, "MAX_LINE", 1000)
    f.write_text(json.dumps(u("ok", 1)) + "\n" + "z" * 20_000, encoding="utf-8")
    assert [t.text for t in read_history(f)[0]] == ["ok"]


def test_uma_linha_de_30mb_nao_estoura_memoria(tmp_path):
    big = tmp_path / "big.jsonl"
    with big.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(u("antes", 1)) + "\n")
        fh.write('{"type":"user","message":{"content":"' + "q" * 30_000_000 + '"}}\n')
        fh.write(json.dumps(a("depois", 2)) + "\n")
    tracemalloc.start()
    try:
        turns, _ = read_history(big)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert [t.text for t in turns] == ["antes", "depois"]
    assert peak < 40_000_000  # MAX_LINE (8M chars) é o teto; nunca os 30M da linha


def test_leituras_concorrentes_em_threads_dao_o_mesmo_resultado(f):
    write(f, simple(60))
    expected = read_history(f, limit=10)
    expected_last = last_exchange(f)

    def work(_):
        return read_history(f, limit=10), last_exchange(f)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(work, range(32)))
    assert all(r == (expected, expected_last) for r in results)
