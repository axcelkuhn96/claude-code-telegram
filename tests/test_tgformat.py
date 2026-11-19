from html.parser import HTMLParser

import pytest

from bot.tgformat import md_to_tg_html, split_html

ALLOWED = {"b", "i", "u", "s", "code", "pre", "a", "blockquote"}


class _Checker(HTMLParser):
    """Falha se houver tag fora da lista permitida ou desbalanceada."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.stack: list[str] = []
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        assert tag in ALLOWED, f"tag nao permitida: {tag}"
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack and self.stack[-1] == tag, (
            f"fecha {tag} com pilha {self.stack}"
        )
        self.stack.pop()

    def handle_data(self, data):
        self.text.append(data)


def check(html: str) -> str:
    c = _Checker()
    c.feed(html)
    c.close()
    assert c.stack == [], f"tags abertas: {c.stack}"
    return "".join(c.text)


def u16(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


# ---------- md_to_tg_html ----------


@pytest.mark.parametrize(
    ("md", "html"),
    [
        ("**negrito**", "<b>negrito</b>"),
        ("__negrito__", "<b>negrito</b>"),
        ("*italico*", "<i>italico</i>"),
        ("_italico_", "<i>italico</i>"),
        ("~~riscado~~", "<s>riscado</s>"),
        ("***ambos***", "<b><i>ambos</i></b>"),
        ("**a *b* c**", "<b>a <i>b</i> c</b>"),
        ("*a **b** c*", "<i>a <b>b</b> c</i>"),
        ("`x = 1`", "<code>x = 1</code>"),
        ("a **b** e *c*", "a <b>b</b> e <i>c</i>"),
    ],
)
def test_inline_basico(md, html):
    assert md_to_tg_html(md) == html


@pytest.mark.parametrize(
    "texto",
    [
        "use snake_case_name aqui",
        "arquivo my_file_name.py",
        "2 * 3 * 4 = 24",
        "a * b e c * d",
        "__init__.py",
        "foo_ e _bar",
        "https://exemplo.com/a_b_c_d?x=1",
    ],
)
def test_nao_formata_dentro_de_palavras(texto):
    out = md_to_tg_html(texto)
    assert "<i>" not in out
    assert "<b>" not in out


def test_dunder_init_nao_vira_negrito():
    assert md_to_tg_html("veja `__init__.py` e __init__.py") == (
        "veja <code>__init__.py</code> e __init__.py"
    )


def test_escapa_html_e_ampersand():
    assert md_to_tg_html("a < b & c > d <script>x</script>") == (
        "a &lt; b &amp; c &gt; d &lt;script&gt;x&lt;/script&gt;"
    )


def test_escapa_dentro_de_code_inline():
    assert md_to_tg_html("`a<b>&c`") == "<code>a&lt;b&gt;&amp;c</code>"


def test_inline_code_protege_markdown():
    assert md_to_tg_html("`**x** _y_`") == "<code>**x** _y_</code>"


def test_bloco_de_codigo_com_linguagem():
    md = "antes\n```python\nx = '**a**' < 2 & _y_\n```\ndepois"
    assert md_to_tg_html(md) == (
        "antes\n"
        "<pre><code class=\"language-python\">x = '**a**' &lt; 2 &amp; _y_</code></pre>\n"
        "depois"
    )


def test_bloco_de_codigo_sem_linguagem():
    assert md_to_tg_html("```\n# nao e titulo\n- nem item\n```") == (
        "<pre># nao e titulo\n- nem item</pre>"
    )


def test_bloco_de_codigo_nao_fechado_e_fechado_na_saida():
    out = md_to_tg_html("```js\nlet a = 1;\nmais")
    assert out == '<pre><code class="language-js">let a = 1;\nmais</code></pre>'


def test_linguagem_invalida_nao_injeta_atributo():
    out = md_to_tg_html('```a"><b>\nx\n```')
    check(out)
    assert 'class="language-a' not in out


def test_link_http():
    assert md_to_tg_html("[doc](https://ex.com/a?x=1&y=2)") == (
        '<a href="https://ex.com/a?x=1&amp;y=2">doc</a>'
    )


def test_link_com_formatacao_no_texto():
    assert (
        md_to_tg_html("[**doc**](http://ex.com)")
        == '<a href="http://ex.com"><b>doc</b></a>'
    )


def test_link_com_parenteses_na_url():
    assert md_to_tg_html("[w](https://pt.wikipedia.org/wiki/Foo_(bar))") == (
        '<a href="https://pt.wikipedia.org/wiki/Foo_(bar)">w</a>'
    )


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "JaVaScRiPt:alert(1)",
        "data:text/html,x",
        "file:///etc/passwd",
        "tg://x",
    ],
)
def test_link_com_esquema_inseguro_vira_texto(url):
    out = md_to_tg_html(f"[clique]({url})")
    assert "<a" not in out
    assert "clique" in out
    check(out)


def test_url_nua_com_underscores_nao_e_formatada():
    assert (
        md_to_tg_html("veja https://ex.com/_a_/_b_ ok")
        == "veja https://ex.com/_a_/_b_ ok"
    )


def test_titulos_viram_negrito():
    assert md_to_tg_html("# Titulo\n## Sub `x`\n###### Fim") == (
        "<b>Titulo</b>\n<b>Sub <code>x</code></b>\n<b>Fim</b>"
    )


def test_titulo_com_negrito_nao_aninha_b():
    assert md_to_tg_html("## **Ola** mundo") == "<b>Ola mundo</b>"


def test_bullets():
    md = "- um\n* dois\n  - filho\n+ tres\n1. numerado"
    assert md_to_tg_html(md) == "• um\n• dois\n  • filho\n• tres\n1. numerado"


def test_bullet_nao_confunde_com_italico():
    assert md_to_tg_html("*italico* e\n* item") == "<i>italico</i> e\n• item"


def test_blockquote():
    assert md_to_tg_html("> linha **a**\n> linha b\nfora") == (
        "<blockquote>linha <b>a</b>\nlinha b</blockquote>\nfora"
    )


def test_tabela_vira_pre():
    md = "| a | b |\n|---|---|\n| 1 | <2 |\n\ntexto"
    assert md_to_tg_html(md) == (
        "<pre>| a | b |\n|---|---|\n| 1 | &lt;2 |</pre>\n\ntexto"
    )


def test_linha_com_pipe_isolada_nao_e_tabela():
    assert md_to_tg_html("| so uma linha") == "| so uma linha"


def test_markdown_dentro_de_tabela_fica_intacto():
    out = md_to_tg_html("| **a** | b |\n|---|---|")
    assert "<b>" not in out
    assert out.startswith("<pre>")


def test_emoji_e_unicode_preservados():
    assert md_to_tg_html("**oi** 😀 ação") == "<b>oi</b> 😀 ação"


def test_idempotente_em_texto_simples():
    txt = "Linha um.\n\nLinha dois, com acentuação e emoji 😀.\n  recuo"
    once = md_to_tg_html(txt)
    assert once == txt
    assert md_to_tg_html(once) == txt


def test_vazio():
    assert md_to_tg_html("") == ""


def test_caracteres_de_controle_do_input_nao_quebram_placeholder():
    out = md_to_tg_html("`a` \x00 0 \x01 `b`")
    assert out.count("<code>") == 2
    check(out)


@pytest.mark.parametrize(
    "md",
    [
        "**a *b** c*",
        "*a **b* c**",
        "**abre sem fechar",
        "*abre sem fechar",
        "`abre sem fechar",
        "[link sem url](",
        "[a](https://x.com) **b** `c` _d_ ~~e~~",
        "> **q\n> *r",
        "# **t** _u_ `v`",
        "```\n**x\n```\n**y**",
        "**`code` dentro**",
        "`**`",
        "_**a**_ e **_b_**",
        "****",
        "**",
        "* * *",
        "- **a\n- b**",
    ],
)
def test_nunca_emite_tags_desbalanceadas(md):
    check(md_to_tg_html(md))


# ---------- split_html ----------


def test_split_curto_devolve_inteiro():
    assert split_html("<b>oi</b>") == ["<b>oi</b>"]


def test_split_vazio():
    assert split_html("") == []


def test_split_em_quebra_de_linha():
    linhas = [f"linha {i:03d} " + "x" * 20 for i in range(40)]
    html = "\n".join(linhas)
    parts = split_html(html, limit=200)
    assert len(parts) > 1
    assert all(u16(p) <= 200 for p in parts)
    for p in parts:  # cortes so em fim de linha: cada parte tem linhas inteiras
        assert all(ln in linhas for ln in p.split("\n"))
    assert "\n".join(parts) == html


def test_split_reabre_tag_inline_na_proxima_parte():
    html = "<b>" + "\n".join(f"item {i:03d}" for i in range(60)) + "</b>"
    parts = split_html(html, limit=120)
    assert len(parts) > 2
    for p in parts:
        assert p.startswith("<b>")
        assert p.endswith("</b>")
        assert u16(p) <= 120
        check(p)


def test_split_reabre_link_com_href():
    html = (
        '<a href="https://ex.com/?a=1&amp;b=2">'
        + "\n".join(["texto longo " * 3] * 20)
        + "</a>"
    )
    parts = split_html(html, limit=150)
    assert len(parts) > 1
    for p in parts:
        assert p.startswith('<a href="https://ex.com/?a=1&amp;b=2">')
        check(p)


def test_split_pre_longo_reabre_pre_code():
    corpo = "\n".join(f"linha_{i:04d} = {i} &lt; 9" for i in range(300))
    html = f'antes\n<pre><code class="language-python">{corpo}</code></pre>\ndepois'
    parts = split_html(html, limit=500)
    assert len(parts) > 3
    for p in parts:
        assert u16(p) <= 500
        check(p)
    assert parts[0].startswith("antes\n<pre>")
    for p in parts[1:-1]:
        assert p.startswith('<pre><code class="language-python">')
        assert p.endswith("</code></pre>")
    assert parts[-1].endswith("</code></pre>\ndepois")
    visivel = "".join(check(p) for p in parts)
    assert "linha_0000" in visivel
    assert "linha_0299" in visivel


def test_split_pre_sem_perder_linhas():
    linhas = [f"l{i:04d}" for i in range(500)]
    html = "<pre>" + "\n".join(linhas) + "</pre>"
    parts = split_html(html, limit=300)
    achadas = [ln for p in parts for ln in check(p).split("\n")]
    assert achadas == linhas


def test_split_blockquote_aninhado_em_ordem():
    html = "<blockquote>" + "\n".join(["cita " * 5] * 30) + "</blockquote>"
    parts = split_html(html, limit=200)
    assert len(parts) > 1
    for p in parts:
        assert p.startswith("<blockquote>")
        assert p.endswith("</blockquote>")


def test_split_emoji_conta_utf16():
    html = "😀" * 3000  # 6000 unidades UTF-16, sem quebra de linha
    parts = split_html(html)
    assert len(parts) >= 2
    assert all(u16(p) <= 4096 for p in parts)
    assert "".join(parts) == html  # nenhum par substituto partido


def test_split_emoji_com_limite_pequeno():
    html = "ab😀" * 100
    parts = split_html(html, limit=67)
    assert all(u16(p) <= 67 for p in parts)
    assert "".join(parts) == html


def test_split_nao_parte_entidade():
    html = "&amp;" * 400
    parts = split_html(html, limit=100)
    assert len(parts) > 1
    assert all(u16(p) <= 100 for p in parts)
    for p in parts:
        assert p.replace("&amp;", "") == ""
    assert "".join(parts) == html


def test_split_linha_gigante_sem_quebra_corta_duro_mantendo_tags():
    html = "<b>" + "a" * 1000 + "</b>"
    parts = split_html(html, limit=100)
    assert all(u16(p) <= 100 for p in parts)
    for p in parts:
        check(p)
    assert "".join(check(p) for p in parts) == "a" * 1000


def test_split_conta_tags_no_limite():
    # texto visivel curto mas HTML longo: o limite conservador vale para o HTML
    html = "\n".join("<b>a</b><i>b</i><u>c</u>" for _ in range(50))
    parts = split_html(html, limit=120)
    assert all(u16(p) <= 120 for p in parts)


def test_split_idempotente_em_texto_simples():
    txt = "um\ndois\ntres"
    assert split_html(txt) == [txt]


def test_fim_a_fim_markdown_longo():
    md = "# Titulo\n\n" + "\n".join(
        f"- item **{i}** com `cod_{i}` e _it_" for i in range(400)
    )
    md += "\n\n```python\n" + "\n".join(f"x{i} = {i}" for i in range(400)) + "\n```\n"
    parts = split_html(md_to_tg_html(md))
    assert len(parts) > 2
    for p in parts:
        assert u16(p) <= 4096
        check(p)
