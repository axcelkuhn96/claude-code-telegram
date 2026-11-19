from __future__ import annotations

import pytest

from bot.textsplit import split


def test_short_and_empty():
    assert split("olá") == ["olá"]
    assert split("") == []


def test_splits_on_line_boundaries_within_limit_without_loss():
    lines = [f"linha {i:03d} " + "x" * 20 for i in range(100)]
    text = "\n".join(lines)
    parts = split(text, limit=200)
    assert len(parts) > 1
    assert all(len(p) <= 200 for p in parts)
    # nenhuma linha partida e nenhuma perdida, na ordem
    assert "\n".join(parts).split("\n") == lines


def test_exact_fit_single_part():
    text = "a" * 50 + "\n" + "b" * 49
    assert split(text, limit=100) == [text]


def test_long_line_hard_split_without_loss():
    text = "z" * 5000
    parts = split(text, limit=4096)
    assert [len(p) for p in parts] == [4096, 904]
    assert "".join(parts) == text


def test_long_line_between_short_lines():
    text = "inicio\n" + "q" * 250 + "\nfim"
    parts = split(text, limit=100)
    assert all(len(p) <= 100 for p in parts)
    assert parts[0] == "inicio"
    assert parts[-1].endswith("\nfim")  # resto da linha dura junta com a linha seguinte
    assert "".join(parts).replace("\n", "").count("q") == 250


def test_code_fence_split_closes_and_reopens():
    body = [f"print({i})" for i in range(60)]
    text = "antes\n```python\n" + "\n".join(body) + "\n```\ndepois"
    parts = split(text, limit=200)
    assert len(parts) > 1
    assert all(len(p) <= 200 for p in parts)
    for p in parts:
        assert p.count("```") % 2 == 0, p  # cada parte tem cercas balanceadas
    assert parts[0].endswith("```") and "```python" in parts[0]
    assert parts[1].startswith("```python\n")
    # conteúdo preservado
    joined = "\n".join(parts)
    for line in body:
        assert line in joined
    assert parts[-1].endswith("depois")


def test_fence_fully_inside_one_part_untouched():
    text = "intro " * 30 + "\n```\ncodigo\n```\n" + "outro " * 30
    parts = split(text, limit=250)
    assert all(len(p) <= 250 for p in parts)
    assert any("```\ncodigo\n```" in p for p in parts)


def test_unclosed_fence_in_source_not_force_closed_when_not_split():
    text = "```\nabc"
    assert split(text, limit=100) == [text]


def test_fence_hard_split_long_line_inside_fence():
    text = "```\n" + "k" * 500 + "\n```"
    parts = split(text, limit=120)
    assert all(len(p) <= 120 for p in parts)
    for p in parts:
        assert p.count("```") % 2 == 0, p
    assert "".join(p.replace("```", "").replace("\n", "") for p in parts) == "k" * 500


def test_limit_too_small_raises():
    with pytest.raises(ValueError):
        split("abc", limit=5)


def u16(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def test_limit_is_utf16_code_units_for_emoji():
    text = "😀" * 5000  # 2 unidades UTF-16 cada
    parts = split(text, limit=4096)
    assert all(u16(p) <= 4096 for p in parts)
    assert "".join(parts) == text  # sem partir par substituto
    lines = "\n".join("😀" * 100 for _ in range(60))
    parts = split(lines, limit=1000)
    assert len(parts) > 1 and all(u16(p) <= 1000 for p in parts)
    assert "\n".join(parts) == lines


def test_info_string_inside_open_fence_is_not_a_close():
    body = "\n".join(f"linha {i}" for i in range(40))
    text = "```\n```py\n" + body + "\n```"  # "```py" dentro da cerca é conteúdo
    parts = split(text, limit=120)
    assert all(len(p) <= 120 for p in parts)
    assert parts[1].startswith("```\n")  # reabre a cerca original, não "```py"


def test_backticks_inline_on_one_line_not_a_fence():
    text = "use ```x``` aqui\n" + "a" * 90 + "\n" + "b" * 90
    parts = split(text, limit=100)
    assert all(len(p) <= 100 for p in parts)
    assert not any(p.endswith("```") for p in parts[1:])  # nada fechado/reaberto
    assert "\n".join(parts) == text


def test_tilde_fence_split_closes_with_tildes():
    text = "~~~\n" + "\n".join(f"l{i}" for i in range(80)) + "\n~~~"
    parts = split(text, limit=100)
    assert all(len(p) <= 100 for p in parts)
    assert parts[0].endswith("\n~~~") and parts[1].startswith("~~~\n")
