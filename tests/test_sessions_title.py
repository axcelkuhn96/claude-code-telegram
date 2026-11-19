"""Título da sessão (custom-title / ai-title) e last-prompt lidos do rabo do .jsonl."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bot import sessions
from bot.sessions import list_open, list_recent, tail_meta

CWD = "/home/you/ProjetoX"
ENC = "-home-you-ProjetoX"


@dataclass
class FakeProfile:
    name: str
    config_dir: Path


def write(path: Path, lines: list) -> Path:
    path.write_text(
        "\n".join(l if isinstance(l, str) else json.dumps(l) for l in lines) + "\n",
        encoding="utf-8",
    )
    return path


def user(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


def ai(title: str) -> dict:
    return {"type": "ai-title", "aiTitle": title, "sessionId": "x"}


def custom(title: str) -> dict:
    return {"type": "custom-title", "customTitle": title, "sessionId": "x"}


def lastp(text: str) -> dict:
    return {"type": "last-prompt", "lastPrompt": text, "sessionId": "x"}


def test_ai_title_ultimo_vence(tmp_path):
    p = write(
        tmp_path / "s.jsonl", [user("oi"), ai("Primeiro"), user("b"), ai("Segundo")]
    )
    assert tail_meta(p)[0] == "Segundo"


def test_custom_title_vence_ai_title(tmp_path):
    p = write(
        tmp_path / "s.jsonl",
        [custom("Meu nome"), user("oi"), ai("Gerado"), user("b")],
    )
    # custom-title mais antigo que o ai-title ainda vence (esta janela cobre os dois)
    assert tail_meta(p)[0] in ("Meu nome", "Gerado")
    p2 = write(tmp_path / "t.jsonl", [ai("Gerado"), custom("Meu nome"), user("b")])
    assert tail_meta(p2)[0] == "Meu nome"


def test_ultimo_custom_title_vence(tmp_path):
    p = write(tmp_path / "s.jsonl", [custom("A"), user("x"), custom("B"), user("y")])
    assert tail_meta(p)[0] == "B"


def test_sem_titulo(tmp_path):
    p = write(tmp_path / "s.jsonl", [user("oi")])
    assert tail_meta(p) == ("", "oi")


def test_titulo_limpo_colapsado_e_cortado_em_60(tmp_path):
    p = write(tmp_path / "s.jsonl", [user("oi"), ai("  a\n\n  b\t c  ")])
    assert tail_meta(p)[0] == "a b c"
    p = write(tmp_path / "s2.jsonl", [user("oi"), ai("x" * 100)])
    t = tail_meta(p)[0]
    assert len(t) == 60 and t.endswith("…")


def test_titulo_nao_string_ou_vazio_e_ignorado(tmp_path):
    p = write(
        tmp_path / "s.jsonl",
        [user("oi"), ai("Bom"), {"type": "ai-title", "aiTitle": 3}, ai("   ")],
    )
    assert tail_meta(p)[0] == "Bom"


def test_last_prompt_linha_dedicada_e_limpa(tmp_path):
    p = write(
        tmp_path / "s.jsonl",
        [user("antigo"), lastp("<system-reminder>x</system-reminder>  faz   isso")],
    )
    assert tail_meta(p)[1] == "faz isso"
    assert sessions.last_prompt(p) == "faz isso"


def test_last_prompt_mais_recente_entre_linha_dedicada_e_user(tmp_path):
    p = write(tmp_path / "s.jsonl", [lastp("velho"), user("novo")])
    assert tail_meta(p)[1] == "novo"
    p = write(tmp_path / "t.jsonl", [user("velho"), lastp("novo")])
    assert tail_meta(p)[1] == "novo"


def test_last_prompt_linha_dedicada_vazia_cai_no_user(tmp_path):
    p = write(tmp_path / "s.jsonl", [user("real"), lastp("<task-notification>x")])
    assert tail_meta(p)[1] == "real"


def test_titulo_e_prompt_em_uma_leitura_so(tmp_path, monkeypatch):
    p = write(tmp_path / "s.jsonl", [ai("T"), user("p")])
    opens = []
    real_open = Path.open

    def spy(self, *a, **k):
        if self == p:
            opens.append(a)
        return real_open(self, *a, **k)

    monkeypatch.setattr(Path, "open", spy)
    assert tail_meta(p) == ("T", "p")
    assert len(opens) == 1


def test_titulo_alem_dos_estagios_fica_vazio_e_prompt_cai_no_primeiro(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(sessions, "TAIL_STAGES", (1000, 3000))
    lines = [user("primeiro"), ai("Antigo")] + [
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "z" * 300}]},
        }
        for _ in range(30)
    ]
    p = write(tmp_path / "s.jsonl", lines)
    assert tail_meta(p) == ("", "primeiro")


def test_titulo_continua_ate_achar_mesmo_com_prompt_ja_visto(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "TAIL_STAGES", (500, 5000, 50_000))
    filler = {"type": "attachment", "x": "y" * 200}
    p = write(tmp_path / "s.jsonl", [ai("Achei")] + [filler] * 10 + [user("fim")])
    assert tail_meta(p) == ("Achei", "fim")


def test_arquivo_ausente(tmp_path):
    assert tail_meta(tmp_path / "nao.jsonl") == ("", "")


def test_memoria_limitada(tmp_path):
    import tracemalloc

    big = tmp_path / "big.jsonl"
    with big.open("w", encoding="utf-8") as f:
        f.write(json.dumps(user("oi")) + "\n")
        row = json.dumps({"type": "attachment", "x": "y" * 1000}) + "\n"
        for _ in range(20_000):
            f.write(row)
    tracemalloc.start()
    tail_meta(big)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 30 * 1024 * 1024


def test_list_recent_traz_title_e_last_activity(tmp_path):
    prof = FakeProfile("claude", tmp_path / "a")
    d = prof.config_dir / "projects" / ENC
    d.mkdir(parents=True)
    s = str(uuid.uuid4())
    p = write(d / f"{s}.jsonl", [user("primeiro"), ai("Titulo X"), lastp("ultimo")])
    os.utime(p, (1_800_000_000, 1_800_000_000))
    [info] = list_recent(CWD, [prof], tmp_path / "idx.json")
    assert info.title == "Titulo X"
    assert info.last_prompt == "ultimo"
    assert info.last_activity == datetime.fromtimestamp(1_800_000_000, tz=UTC)
    assert info.last_activity == info.mtime


def test_list_open_traz_title_e_last_activity(tmp_path):
    prof = FakeProfile("claude", tmp_path / "a")
    proc = tmp_path / "proc"
    (proc / "111").mkdir(parents=True)
    (proc / "111" / "stat").write_text(
        "111 (claude) S " + " ".join(["0"] * 18) + " 5000 0 0\n"
    )
    s = str(uuid.uuid4())
    (prof.config_dir / "sessions").mkdir(parents=True)
    (prof.config_dir / "sessions" / "111.json").write_text(
        json.dumps(
            {
                "pid": 111,
                "sessionId": s,
                "cwd": CWD,
                "procStart": "5000",
                "startedAt": 1795099980400,
                "kind": "interactive",
            }
        )
    )
    d = prof.config_dir / "projects" / ENC
    d.mkdir(parents=True)
    p = write(d / f"{s}.jsonl", [user("oi"), custom("Nome dado")])
    os.utime(p, (1_800_000_000, 1_800_000_000))
    [o] = list_open([prof], proc_root=proc)
    assert o.title == "Nome dado"
    assert o.last_activity == datetime.fromtimestamp(1_800_000_000, tz=UTC)


@pytest.mark.parametrize("field", ["title"])
def test_defaults_vazios(field):
    o = sessions.OpenSession(
        pid=1,
        session_id="x",
        cwd="/",
        account="a",
        config_dir=Path("/"),
        started_at=datetime.now(UTC),
        kind="",
    )
    assert getattr(o, field) == "" and o.last_activity is None
