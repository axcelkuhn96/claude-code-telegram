from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from bot import usage
from bot.usage import AccountUsage, Profile, Window

UTC = timezone.utc
NOW = datetime(2026, 11, 19, 15, 0, 0, tzinfo=UTC)
NOW_MS = int(NOW.timestamp() * 1000)
FAR_MS = NOW_MS + 3_600_000
TOKEN = "sk-ant-oat01-FAKE-TOKEN-FOR-TESTS"

USAGE_BODY = {
    "five_hour": {"utilization": 37.0, "resets_at": "2026-11-19T18:00:00.123456+00:00"},
    "seven_day": {"utilization": 12, "resets_at": "2026-11-23T00:00:00Z"},
}


@pytest.fixture(autouse=True)
def _clear_memo():
    usage.clear_memo()
    yield
    usage.clear_memo()


def write_creds(config_dir: Path, token: str = TOKEN, expires_ms: int = FAR_MS) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": token, "expiresAt": expires_ms}})
    )


def write_account(
    path: Path, display: str = "Ada", email: str = "ada@example.com"
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"oauthAccount": {"displayName": display, "emailAddress": email}})
    )


def make_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def ok_handler(
    calls: list[httpx.Request] | None = None,
    body: dict | None = None,
    status: int = 200,
):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        return httpx.Response(status, json=body if body is not None else USAGE_BODY)

    return handler


def no_network(request: httpx.Request) -> httpx.Response:
    raise AssertionError("rede não deveria ser chamada")


def acct(
    name="a",
    five=None,
    seven=None,
    state="ok",
    severity="normal",
    email="ada@example.com",
    fetched=NOW,
):
    return AccountUsage(
        name=name,
        display_name=name,
        email=email,
        five_hour=Window(five, NOW + timedelta(hours=2) if five is not None else None),
        seven_day=Window(seven, NOW + timedelta(days=3) if seven is not None else None),
        severity=severity,
        state=state,
        fetched_at=fetched,
    )


# 1. load_profiles ---------------------------------------------------------


def test_load_profiles_from_yaml(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "claude_profiles:\n- name: a\n  config_dir: ~/.claude-a\n- name: b\n  config_dir: /abs/b\nworkspaces: []\n"
    )
    profiles = usage.load_profiles(cfg, home=tmp_path)
    assert profiles == [
        Profile("a", tmp_path / ".claude-a"),
        Profile("b", Path("/abs/b")),
    ]


@pytest.mark.parametrize(
    "content", [None, "workspaces: []\n", ": : not yaml [", "claude_profiles: []\n"]
)
def test_load_profiles_defaults(tmp_path, content):
    cfg = tmp_path / "config.yaml"
    if content is not None:
        cfg.write_text(content)
    profiles = usage.load_profiles(cfg, home=tmp_path)
    assert [(p.name, p.config_dir) for p in profiles] == [
        ("claude", tmp_path / ".claude"),
        ("work", tmp_path / ".claude-work"),
        ("personal", tmp_path / ".claude-personal"),
        ("client", tmp_path / ".claude-client"),
    ]


# 2. parse_usage_response --------------------------------------------------


def test_parse_utilization():
    five, seven, sev = usage.parse_usage_response(USAGE_BODY)
    assert five.percent == 37.0
    assert five.resets_at == datetime(2026, 11, 19, 18, 0, 0, 123456, tzinfo=UTC)
    assert seven.percent == 12.0
    assert seven.resets_at == datetime(2026, 11, 23, tzinfo=UTC)
    assert sev == "normal"


def test_parse_percent_key():
    five, seven, _ = usage.parse_usage_response(
        {"five_hour": {"percent": 80}, "seven_day": {"percent": 5.5}}
    )
    assert (five.percent, seven.percent) == (80.0, 5.5)


def test_parse_null_windows():
    five, seven, sev = usage.parse_usage_response(
        {"five_hour": None, "seven_day": {"utilization": None}}
    )
    assert five == Window(None, None)
    assert seven == Window(None, None)
    assert sev == "normal"


def test_parse_limits_fallback():
    body = {
        "five_hour": None,
        "limits": [
            {
                "group": "session",
                "utilization": 41,
                "resets_at": "2026-11-19T17:00:00Z",
                "is_active": True,
            },
            {
                "kind": "weekly_all",
                "utilization": 66,
                "is_active": True,
                "severity": "warning",
            },
        ],
    }
    five, seven, sev = usage.parse_usage_response(body)
    assert five.percent == 41.0
    assert seven.percent == 66.0
    assert sev == "warning"


def test_parse_severity_worst_active_limit():
    body = {
        "limits": [
            {
                "group": "weekly",
                "utilization": 10,
                "is_active": True,
                "severity": "warning",
            },
            {
                "group": "five_hour",
                "utilization": 10,
                "is_active": True,
                "severity": "critical",
            },
            {"group": "x", "is_active": False, "severity": "critical"},
        ]
    }
    assert usage.parse_usage_response(body)[2] == "critical"


@pytest.mark.parametrize(
    "five,seven,expected",
    [
        (95, 0, "critical"),
        (90, 0, "critical"),
        (10, 75, "warning"),
        (74.9, 10, "normal"),
    ],
)
def test_parse_severity_computed(five, seven, expected):
    body = {"five_hour": {"utilization": five}, "seven_day": {"utilization": seven}}
    assert usage.parse_usage_response(body)[2] == expected


def test_parse_garbage_values():
    five, seven, _ = usage.parse_usage_response(
        {
            "five_hour": {"utilization": "abc", "resets_at": "ontem"},
            "seven_day": "x",
            "limits": "y",
        }
    )
    assert five == Window(None, None)
    assert seven == Window(None, None)


# 3. read_horizon_cache ----------------------------------------------------


def cache_entry(name="claude", ms=NOW_MS, state="ok", five=10.0):
    return {
        "name": name,
        "display_name": name.title(),
        "email": "pessoa@example.com",
        "five_hour": {"percent": five, "resets_at": "2026-11-19T18:00:00+00:00"},
        "seven_day": {"percent": None, "resets_at": None},
        "severity": "critical",
        "state": state,
        "fetched_at_ms": ms,
    }


def test_cache_fresh(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(
        json.dumps(
            [
                cache_entry("claude", NOW_MS - 60_000),
                cache_entry("work", NOW_MS - 120_000),
            ]
        )
    )
    items = usage.read_horizon_cache(p, NOW)
    assert [i.name for i in items] == ["claude", "work"]
    first = items[0]
    assert first.display_name == "Claude"
    assert first.email == "pessoa@example.com"
    assert first.five_hour.percent == 10.0
    assert first.seven_day == Window(None, None)
    assert first.severity == "critical"
    assert first.state == "ok"
    assert first.fetched_at == datetime.fromtimestamp((NOW_MS - 60_000) / 1000, tz=UTC)


def test_cache_old_returns_none(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps([cache_entry(ms=NOW_MS - 16 * 60_000)]))
    assert usage.read_horizon_cache(p, NOW) is None


def test_cache_boundary_15_min_is_fresh(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps([cache_entry(ms=NOW_MS - 15 * 60_000)]))
    assert usage.read_horizon_cache(p, NOW) is not None


@pytest.mark.parametrize(
    "content", [None, "", "{not json", "{}", "[]", '[{"name": 1}]', '["x"]']
)
def test_cache_missing_or_corrupt(tmp_path, content):
    p = tmp_path / "c.json"
    if content is not None:
        p.write_text(content)
    assert usage.read_horizon_cache(p, NOW) is None


def test_cache_unknown_state_becomes_stale(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps([cache_entry(state="weird")]))
    assert usage.read_horizon_cache(p, NOW)[0].state == "stale"


# 4. fetch_usage -----------------------------------------------------------


async def test_fetch_ok(tmp_path):
    cdir = tmp_path / ".claude-x"
    write_creds(cdir)
    write_account(cdir / ".claude.json")
    calls: list[httpx.Request] = []
    async with make_client(ok_handler(calls)) as client:
        r = await usage.fetch_usage(Profile("x", cdir), client, NOW, home=tmp_path)
    assert r.state == "ok"
    assert (r.name, r.display_name, r.email) == ("x", "Ada", "ada@example.com")
    assert r.five_hour.percent == 37.0 and r.seven_day.percent == 12.0
    assert r.fetched_at == NOW
    assert len(calls) == 1
    req = calls[0]
    assert str(req.url) == "https://api.anthropic.com/api/oauth/usage"
    assert req.method == "GET"
    assert req.headers["authorization"] == f"Bearer {TOKEN}"
    assert req.headers["anthropic-beta"] == "oauth-2025-04-20"


async def test_fetch_home_claude_reads_home_claude_json(tmp_path):
    cdir = tmp_path / ".claude"
    write_creds(cdir)
    write_account(tmp_path / ".claude.json", "Home", "home@example.com")
    write_account(cdir / ".claude.json", "Wrong", "wrong@example.com")
    async with make_client(ok_handler()) as client:
        r = await usage.fetch_usage(Profile("claude", cdir), client, NOW, home=tmp_path)
    assert (r.display_name, r.email) == ("Home", "home@example.com")


async def test_fetch_without_account_file_uses_profile_name(tmp_path):
    cdir = tmp_path / ".c"
    write_creds(cdir)
    async with make_client(ok_handler()) as client:
        r = await usage.fetch_usage(Profile("zed", cdir), client, NOW, home=tmp_path)
    assert (r.display_name, r.email) == ("zed", None)


@pytest.mark.parametrize("delta_ms", [-1000, 0, 30_000, 60_000])
async def test_fetch_expired_token_no_network(tmp_path, delta_ms):
    cdir = tmp_path / ".c"
    write_creds(cdir, expires_ms=NOW_MS + delta_ms)
    write_account(cdir / ".claude.json")
    async with make_client(no_network) as client:
        r = await usage.fetch_usage(Profile("x", cdir), client, NOW, home=tmp_path)
    assert r.state == "expired"
    assert r.email == "ada@example.com"


async def test_fetch_just_beyond_margin_calls_api(tmp_path):
    cdir = tmp_path / ".c"
    write_creds(cdir, expires_ms=NOW_MS + 60_001)
    calls: list[httpx.Request] = []
    async with make_client(ok_handler(calls)) as client:
        r = await usage.fetch_usage(Profile("x", cdir), client, NOW, home=tmp_path)
    assert r.state == "ok" and len(calls) == 1


@pytest.mark.parametrize(
    "content,state",
    [
        (None, "missing"),
        ("", "missing"),
        ("{bad", "missing"),
        ("{}", "missing"),
        ('{"claudeAiOauth": {}}', "expired"),
        ('{"claudeAiOauth": {"accessToken": 5, "expiresAt": "x"}}', "expired"),
    ],
)
async def test_fetch_missing_or_malformed_credentials_no_network(
    tmp_path, content, state
):
    cdir = tmp_path / ".c"
    cdir.mkdir()
    if content is not None:
        (cdir / ".credentials.json").write_text(content)
    async with make_client(no_network) as client:
        r = await usage.fetch_usage(Profile("x", cdir), client, NOW, home=tmp_path)
    assert r.state == state
    assert r.five_hour == Window(None, None)


async def test_fetch_missing_file_is_missing(tmp_path):
    async with make_client(no_network) as client:
        r = await usage.fetch_usage(
            Profile("x", tmp_path / "nada"), client, NOW, home=tmp_path
        )
    assert r.state == "missing"


@pytest.mark.parametrize("status", [401, 403])
async def test_fetch_401_403_expired(tmp_path, status):
    cdir = tmp_path / ".c"
    write_creds(cdir)
    async with make_client(ok_handler(status=status, body={"error": "x"})) as client:
        r = await usage.fetch_usage(Profile("x", cdir), client, NOW, home=tmp_path)
    assert r.state == "expired"


@pytest.mark.parametrize("status", [429, 500, 503, 404])
async def test_fetch_other_http_error_stale(tmp_path, status):
    cdir = tmp_path / ".c"
    write_creds(cdir)
    async with make_client(ok_handler(status=status, body={"error": "x"})) as client:
        r = await usage.fetch_usage(Profile("x", cdir), client, NOW, home=tmp_path)
    assert r.state == "stale"


async def test_fetch_timeout_stale_without_leaking_token(tmp_path):
    cdir = tmp_path / ".c"
    write_creds(cdir)

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout", request=request)

    async with make_client(boom) as client:
        r = await usage.fetch_usage(Profile("x", cdir), client, NOW, home=tmp_path)
    assert r.state == "stale"
    assert TOKEN not in repr(r)


async def test_fetch_invalid_json_stale(tmp_path):
    cdir = tmp_path / ".c"
    write_creds(cdir)

    def handler(request):
        return httpx.Response(200, content=b"<html>nope</html>")

    async with make_client(handler) as client:
        r = await usage.fetch_usage(Profile("x", cdir), client, NOW, home=tmp_path)
    assert r.state == "stale"


async def test_fetch_does_not_modify_credentials(tmp_path):
    cdir = tmp_path / ".c"
    write_creds(cdir)
    before = (cdir / ".credentials.json").read_bytes()
    async with make_client(ok_handler()) as client:
        await usage.fetch_usage(Profile("x", cdir), client, NOW, home=tmp_path)
    assert (cdir / ".credentials.json").read_bytes() == before
    assert sorted(p.name for p in cdir.iterdir()) == [".credentials.json"]


# 5. get_all_usage ---------------------------------------------------------


def two_profiles(tmp_path):
    out = []
    for n in ("claude", "work"):
        d = tmp_path / f".{n}"
        write_creds(d)
        out.append(Profile(n, d))
    return out


async def test_get_all_uses_fresh_cache_without_network(tmp_path):
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps([cache_entry("claude"), cache_entry("work")]))
    async with make_client(no_network) as client:
        items = await usage.get_all_usage(
            two_profiles(tmp_path), cache, client, NOW, home=tmp_path
        )
    assert [i.name for i in items] == ["claude", "work"]


async def test_get_all_old_cache_goes_to_api_and_memoizes(tmp_path):
    cache = tmp_path / "cache.json"
    cache.write_text(
        json.dumps(
            [
                cache_entry("claude", NOW_MS - 3_600_000),
                cache_entry("work", NOW_MS - 3_600_000),
            ]
        )
    )
    calls: list[httpx.Request] = []
    profiles = two_profiles(tmp_path)
    async with make_client(ok_handler(calls)) as client:
        a = await usage.get_all_usage(profiles, cache, client, NOW, home=tmp_path)
        b = await usage.get_all_usage(
            profiles, cache, client, NOW + timedelta(minutes=4), home=tmp_path
        )
        assert len(calls) == 2
        assert [i.name for i in a] == ["claude", "work"]
        assert b == a
        c = await usage.get_all_usage(
            profiles, cache, client, NOW + timedelta(minutes=6), home=tmp_path
        )
    assert len(calls) == 4
    assert [i.state for i in c] == ["ok", "ok"]


async def test_get_all_missing_cache_goes_to_api(tmp_path):
    calls: list[httpx.Request] = []
    async with make_client(ok_handler(calls)) as client:
        items = await usage.get_all_usage(
            two_profiles(tmp_path), tmp_path / "nope.json", client, NOW, home=tmp_path
        )
    assert len(calls) == 2 and len(items) == 2


async def test_get_all_cache_missing_a_profile_goes_to_api(tmp_path):
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps([cache_entry("claude")]))
    calls: list[httpx.Request] = []
    async with make_client(ok_handler(calls)) as client:
        items = await usage.get_all_usage(
            two_profiles(tmp_path), cache, client, NOW, home=tmp_path
        )
    assert len(calls) == 2 and [i.name for i in items] == ["claude", "work"]


async def test_get_all_one_failure_does_not_break_others(tmp_path):
    profiles = two_profiles(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={})

    async with make_client(handler) as client:
        items = await usage.get_all_usage(
            profiles, tmp_path / "nope.json", client, NOW, home=tmp_path
        )
    assert [i.state for i in items] == ["expired", "expired"]


# 6. format_usage ----------------------------------------------------------


def test_mask_email():
    assert usage.mask_email("pessoa@example.com") == "p…@example.com"
    assert usage.mask_email(None) == "—"
    assert usage.mask_email("sem-arroba") == "—"


def test_format_line_contents():
    item = AccountUsage(
        "work",
        "Pessoa",
        "pessoa@example.com",
        Window(42.4, datetime(2026, 11, 19, 18, 5, tzinfo=UTC)),
        Window(80.0, datetime(2026, 11, 23, 3, 30, tzinfo=UTC)),
        "warning",
        "ok",
        NOW,
    )
    out = usage.format_usage([item], None, NOW)
    assert out.startswith("<b>Contas Claude</b>\n\n")
    assert "<b>work</b> 🟡" in out
    assert "pessoa" not in out  # sem e-mail
    assert "   5h ▰▰▰▰▱▱▱▱▱▱ 42% · reset 18:05" in out
    assert "   7d ▰▰▰▰▰▰▰▰▱▱ 80% · reset 23/11 03:30" in out
    assert "⚠️" not in out  # estado ok não aparece


def test_format_bar_cells():
    assert usage._bar(0) == "▱" * 10
    assert usage._bar(9) == "▰" + "▱" * 9  # mínimo 1 se há uso
    assert usage._bar(97) == "▰" * 9 + "▱"
    assert usage._bar(100) == "▰" * 10
    assert usage._bar(150) == "▰" * 10
    assert all(len(usage._bar(p)) == 10 for p in (0, 1, 50, 99, 100))


def test_format_exemplo_do_dono():
    items = [
        AccountUsage(
            "client",
            "S",
            "s@x.com",
            Window(None, None),
            Window(97.0, datetime(2026, 11, 24, 13, 59, tzinfo=UTC)),
            "critical",
            "ok",
            NOW,
        ),
        AccountUsage(
            "work",
            "C",
            "c@x.com",
            Window(None, None),
            Window(9.0, datetime(2026, 11, 26, 11, 59, tzinfo=UTC)),
            "normal",
            "ok",
            NOW,
        ),
    ]
    assert usage.format_usage(items, "work", NOW) == (
        "<b>Contas Claude</b>\n\n"
        "✅ <b>work</b> 🟢\n"
        "   7d ▰▱▱▱▱▱▱▱▱▱ 9% · reset 26/11 11:59\n\n"
        "<b>client</b> 🔴\n"
        "   7d ▰▰▰▰▰▰▰▰▰▱ 97% · reset 24/11 13:59"
    )


def test_format_ordem_melhor_primeiro_esgotada_e_invalida_por_ultimo():
    items = [
        acct("esgotada", five=10, seven=100),
        acct("expirada", state="expired"),
        acct("alta", five=60, seven=5),
        acct("baixa", five=5, seven=50),
    ]
    out = usage.format_usage(items, None, NOW)
    names = [n for n in ("baixa", "alta", "esgotada", "expirada") if n in out]
    assert sorted(names, key=out.index) == ["baixa", "alta", "esgotada", "expirada"]
    assert [i.name for i in usage.sort_usage(items)] == [
        "baixa",
        "alta",
        "esgotada",
        "expirada",
    ]


def test_format_escapa_html_no_nome():
    out = usage.format_usage([acct("<b>&x", five=1, seven=1)], None, NOW)
    assert "&lt;b&gt;&amp;x" in out and "<b><b>" not in out


def test_format_quatro_contas_cabe_em_uma_mensagem():
    items = [acct(f"conta-{n}", five=n * 20, seven=n * 20) for n in range(4)]
    out = usage.format_usage(items, "conta-1", NOW)
    assert len(out.encode("utf-16-le")) // 2 <= 4096


def test_button_label():
    assert (
        usage.button_label(acct("work", five=5, seven=9), None) == "🟢 work · 9%"
    )
    assert (
        usage.button_label(acct("claude", five=None, seven=100), "claude")
        == "✅ 🔴 claude · 100%"
    )
    assert usage.button_label(acct("x", state="expired"), None) == "⚪ x · —"


def test_format_resets_in_now_timezone():
    tz = timezone(timedelta(hours=-3))
    now = NOW.astimezone(tz)
    item = acct("a", five=10, seven=10)
    item = AccountUsage(
        **{
            **item.__dict__,
            "five_hour": Window(10, datetime(2026, 11, 19, 18, 5, tzinfo=UTC)),
        }
    )
    assert "reset 15:05" in usage.format_usage([item], None, now)


@pytest.mark.parametrize(
    "five,seven,severity,emoji",
    [
        (10, 10, "normal", "🟢"),
        (91, 10, "normal", "🔴"),
        (10, 76, "normal", "🟡"),
        (10, 10, "critical", "🔴"),
        (10, 10, "warning", "🟡"),
    ],
)
def test_format_emoji(five, seven, severity, emoji):
    out = usage.format_usage(
        [acct("a", five=five, seven=seven, severity=severity)], None, NOW
    )
    assert f"<b>a</b> {emoji}" in out


def test_format_expired_omite_janelas_e_mostra_estado():
    out = usage.format_usage([acct("a", state="expired")], None, NOW)
    assert "5h" not in out and "7d" not in out
    assert "⚠️ relogar" in out
    assert "<b>a</b> ⚪" in out
    assert "⚠️ desatualizado" in usage.format_usage(
        [acct("a", state="stale")], None, NOW
    )


def test_format_janela_nula_e_omitida_em_conta_ok():
    out = usage.format_usage([acct("a", five=None, seven=50)], None, NOW)
    assert "5h" not in out and "7d ▰▰▰▰▰▱▱▱▱▱ 50%" in out
    assert "sem dados de uso" in usage.format_usage([acct("b")], None, NOW)


def test_format_marca_ativa_e_trata_vazio():
    out = usage.format_usage(
        [acct("a", five=1, seven=1), acct("b", five=1, seven=1)], "b", NOW
    )
    assert "✅ <b>b</b>" in out and "✅ <b>a</b>" not in out
    assert "\n\n" in out.split("\n\n", 1)[1]  # blocos separados por linha em branco
    assert usage.format_usage([], None, NOW)


# 7. pick_best -------------------------------------------------------------


def test_pick_best_lowest_five_hour_among_ok():
    items = [acct("a", 50, 10), acct("b", 20, 90), acct("c", 5, 5, state="expired")]
    assert usage.pick_best(items) == "b"


def test_pick_best_tie_breaks_on_seven_day():
    assert usage.pick_best([acct("a", 20, 60), acct("b", 20, 30)]) == "b"


def test_pick_best_null_five_hour_counts_as_zero():
    assert usage.pick_best([acct("a", 10, 10), acct("b", None, 20)]) == "b"


def test_pick_best_excludes_exhausted_window():
    assert usage.pick_best([acct("a", None, 100), acct("b", 40, 40)]) == "b"
    assert usage.pick_best([acct("a", 100, 10)]) is None


def test_pick_best_rounded_hundred_is_spent():
    assert usage.pick_best([acct("a", 99.6, 10), acct("b", 40, 40)]) == "b"
    assert usage.pick_best([acct("a", 10, 99.5)]) is None
    assert usage.pick_best([acct("a", 99.4, 10)]) == "a"


# --- polimento pós-review T2 -------------------------------------------------


async def test_fetch_account_falls_back_to_config_dir_json(tmp_path):
    cdir = tmp_path / ".claude"
    write_creds(cdir)
    (tmp_path / ".claude.json").write_text(json.dumps({"other": 1}))
    write_account(cdir / ".claude.json", "InDir", "indir@example.com")
    async with make_client(ok_handler()) as client:
        r = await usage.fetch_usage(Profile("claude", cdir), client, NOW, home=tmp_path)
    assert (r.display_name, r.email) == ("InDir", "indir@example.com")


async def test_fetch_token_never_in_logs(tmp_path, caplog):
    caplog.set_level("DEBUG")
    cdir = tmp_path / ".c"
    write_creds(cdir)

    def leaky(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"falhou com {request.headers['authorization']}")

    async with make_client(leaky) as client:
        r = await usage.fetch_usage(Profile("x", cdir), client, NOW, home=tmp_path)
    assert r.state == "stale"
    for status in (500, 401):
        async with make_client(ok_handler(status=status)) as client:
            await usage.fetch_usage(Profile("x", cdir), client, NOW, home=tmp_path)
    assert caplog.records
    assert TOKEN not in caplog.text
    assert "ConnectError" in caplog.text


async def test_fetch_token_with_bad_chars_is_stale_not_raise(tmp_path):
    cdir = tmp_path / ".c"
    write_creds(cdir, token="toké\ud800en")
    async with make_client(ok_handler()) as client:
        r = await usage.fetch_usage(Profile("x", cdir), client, NOW, home=tmp_path)
    assert r.state == "stale"


async def test_fetch_hostile_body_is_stale_or_ok_never_raises(tmp_path):
    cdir = tmp_path / ".c"
    write_creds(cdir)
    raw = (
        b'{"five_hour": {"utilization": 1e999, "resets_at": "0001-01-01T00:00:00+05:00"},'
        b' "seven_day": {"utilization": 10, "resets_at": ["x"]},'
        b' "limits": [{"group": ["x"], "kind": 5, "severity": ["boom"], "is_active": true}]}'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=raw)

    async with make_client(handler) as client:
        r = await usage.fetch_usage(Profile("x", cdir), client, NOW, home=tmp_path)
    assert r.state == "ok"
    assert r.five_hour == Window(None, None)
    assert r.seven_day.percent == 10.0
    assert usage.format_usage([r], None, NOW)


async def test_fetch_json_list_body_is_stale(tmp_path):
    cdir = tmp_path / ".c"
    write_creds(cdir)
    async with make_client(ok_handler(body=[1, 2])) as client:
        r = await usage.fetch_usage(Profile("x", cdir), client, NOW, home=tmp_path)
    assert r.state == "stale"


def test_cache_hostile_values_never_raise(tmp_path):
    p = tmp_path / "c.json"
    bad = cache_entry("a")
    bad.update(state=["x"], severity={"y": 1}, display_name=5, email=7)
    huge = cache_entry("b", ms=10**30)
    p.write_text(json.dumps([bad, huge, cache_entry("c")]))
    items = usage.read_horizon_cache(p, NOW)
    assert items is not None
    by = {i.name: i for i in items}
    assert by["a"].state == "stale" and by["a"].severity == "normal"
    assert by["a"].display_name == "a" and by["a"].email is None


def test_cache_deeply_nested_is_none(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("[" * 100_000 + "]" * 100_000)
    assert usage.read_horizon_cache(p, NOW) is None


async def test_get_all_gather_exception_maps_to_stale(tmp_path, monkeypatch, caplog):
    profiles = two_profiles(tmp_path)
    real = usage.fetch_usage

    async def flaky(profile, client, now, home=None):
        if profile.name == "work":
            raise RuntimeError(f"explodiu {TOKEN}")
        return await real(profile, client, now, home)

    monkeypatch.setattr(usage, "fetch_usage", flaky)
    async with make_client(ok_handler()) as client:
        items = await usage.get_all_usage(
            profiles, tmp_path / "nope.json", client, NOW, home=tmp_path
        )
    assert [(i.name, i.state) for i in items] == [
        ("claude", "ok"),
        ("work", "stale"),
    ]
    assert TOKEN not in caplog.text


async def test_get_all_memo_returns_copy(tmp_path):
    profiles = two_profiles(tmp_path)
    async with make_client(ok_handler()) as client:
        a = await usage.get_all_usage(
            profiles, tmp_path / "nope.json", client, NOW, home=tmp_path
        )
        a.clear()
        b = await usage.get_all_usage(
            profiles, tmp_path / "nope.json", client, NOW, home=tmp_path
        )
    assert [i.name for i in b] == ["claude", "work"]


def test_load_profiles_logs_when_corrupt_not_when_absent(tmp_path, caplog):
    usage.load_profiles(tmp_path / "ausente.yaml", home=tmp_path)
    assert not caplog.records
    cfg = tmp_path / "config.yaml"
    cfg.write_text(": : not yaml [")
    usage.load_profiles(cfg, home=tmp_path)
    assert any(r.levelname == "WARNING" for r in caplog.records)


def test_pick_best_none_without_ok():
    assert usage.pick_best([]) is None
    assert (
        usage.pick_best(
            [acct("a", 1, 1, state="expired"), acct("b", 1, 1, state="stale")]
        )
        is None
    )


# 8. idle (token de acesso vencido, login vivo) vs expired (login vencido) ----

DAY_MS = 86_400_000
LOGIN_FUTURE_MS = NOW_MS + 20 * DAY_MS
LOGIN_PAST_MS = NOW_MS - 3 * DAY_MS
REFRESH = "sk-ant-ort01-FAKE-REFRESH-FOR-TESTS"


def _dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def write_login(
    config_dir: Path,
    access_exp_ms: int = NOW_MS - 1000,
    refresh_exp_ms: int | None = LOGIN_FUTURE_MS,
    refresh_token: str = REFRESH,
) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    oauth: dict = {
        "accessToken": TOKEN,
        "expiresAt": access_exp_ms,
        "refreshToken": refresh_token,
    }
    if refresh_exp_ms is not None:
        oauth["refreshTokenExpiresAt"] = refresh_exp_ms
    (config_dir / ".credentials.json").write_text(json.dumps({"claudeAiOauth": oauth}))


async def fetch_one(cdir: Path, tmp_path: Path, handler=no_network) -> AccountUsage:
    async with make_client(handler) as client:
        return await usage.fetch_usage(Profile("x", cdir), client, NOW, home=tmp_path)


async def test_fetch_access_expired_refresh_alive_is_idle_without_network(tmp_path):
    cdir = tmp_path / ".c"
    write_login(cdir)
    r = await fetch_one(cdir, tmp_path)
    assert r.state == "idle"
    assert r.login_expires_at == _dt(LOGIN_FUTURE_MS)


async def test_fetch_refresh_expiry_absent_but_refresh_token_is_idle(tmp_path):
    cdir = tmp_path / ".c"
    write_login(cdir, refresh_exp_ms=None)
    r = await fetch_one(cdir, tmp_path)
    assert r.state == "idle" and r.login_expires_at is None


@pytest.mark.parametrize("refresh_exp", [LOGIN_PAST_MS, NOW_MS])
async def test_fetch_refresh_expired_is_expired(tmp_path, refresh_exp):
    cdir = tmp_path / ".c"
    write_login(cdir, refresh_exp_ms=refresh_exp)
    r = await fetch_one(cdir, tmp_path)
    assert r.state == "expired"
    assert r.login_expires_at == _dt(refresh_exp)


async def test_fetch_expired_without_any_refresh_info(tmp_path):
    cdir = tmp_path / ".c"
    write_login(cdir, refresh_exp_ms=None, refresh_token="")
    assert (await fetch_one(cdir, tmp_path)).state == "expired"


@pytest.mark.parametrize("status", [401, 403])
async def test_fetch_401_with_live_login_is_idle(tmp_path, status):
    cdir = tmp_path / ".c"
    write_login(cdir, access_exp_ms=FAR_MS)
    r = await fetch_one(cdir, tmp_path, ok_handler(status=status, body={}))
    assert r.state == "idle"


async def test_fetch_401_with_dead_login_is_expired(tmp_path):
    cdir = tmp_path / ".c"
    write_login(cdir, access_exp_ms=FAR_MS, refresh_exp_ms=LOGIN_PAST_MS)
    r = await fetch_one(cdir, tmp_path, ok_handler(status=401, body={}))
    assert r.state == "expired"


async def test_fetch_idle_never_logs_secrets(tmp_path, caplog):
    cdir = tmp_path / ".c"
    write_login(cdir)
    with caplog.at_level("DEBUG"):
        await fetch_one(cdir, tmp_path)
    assert TOKEN not in caplog.text and REFRESH not in caplog.text


def one_profile(tmp_path) -> list[Profile]:
    return [Profile("claude", tmp_path / ".claude")]


async def run_all(tmp_path, now=NOW, state_dir=None, handler=no_network, cache=None):
    async with make_client(handler) as client:
        return await usage.get_all_usage(
            one_profile(tmp_path),
            cache or tmp_path / "nope.json",
            client,
            now,
            home=tmp_path,
            state_dir=state_dir,
        )


async def test_idle_keeps_last_known_from_memo(tmp_path):
    write_creds(tmp_path / ".claude")
    first = (await run_all(tmp_path, handler=ok_handler()))[0]
    assert first.state == "ok"
    write_login(tmp_path / ".claude")
    r = (await run_all(tmp_path, now=NOW + timedelta(minutes=10)))[0]
    assert r.state == "idle"
    assert r.five_hour == first.five_hour and r.seven_day == first.seven_day
    assert r.stale_since == NOW


async def test_idle_without_any_last_known_has_empty_windows(tmp_path):
    write_login(tmp_path / ".claude")
    r = (await run_all(tmp_path))[0]
    assert r.state == "idle"
    assert r.five_hour == Window(None, None) and r.stale_since is None


async def test_last_known_persisted_atomic_0600_without_secrets(tmp_path):
    sd = tmp_path / "state"
    write_creds(tmp_path / ".claude")
    await run_all(tmp_path, state_dir=sd, handler=ok_handler())
    f = sd / "usage-last.json"
    assert f.stat().st_mode & 0o777 == 0o600
    raw = f.read_text()
    assert TOKEN not in raw and "ada@example.com" not in raw
    assert json.loads(raw)["claude"]["seven_day"]["percent"] == 12
    assert [p.name for p in sd.iterdir()] == ["usage-last.json"]


async def test_idle_reads_persisted_after_restart(tmp_path):
    sd = tmp_path / "state"
    write_creds(tmp_path / ".claude")
    await run_all(tmp_path, state_dir=sd, handler=ok_handler())
    usage.clear_memo()  # "reinício" do bot
    write_login(tmp_path / ".claude")
    r = (await run_all(tmp_path, now=NOW + timedelta(hours=1), state_dir=sd))[0]
    assert r.state == "idle"
    assert r.five_hour.percent == 37.0 and r.seven_day.percent == 12.0
    assert r.stale_since == NOW


async def test_idle_corrupt_persisted_cache_is_ignored(tmp_path):
    sd = tmp_path / "state"
    sd.mkdir()
    (sd / "usage-last.json").write_text("{not json")
    write_login(tmp_path / ".claude")
    r = (await run_all(tmp_path, state_dir=sd))[0]
    assert r.state == "idle" and r.five_hour.percent is None


async def test_idle_uses_horizon_cache_when_fresher_than_persisted(tmp_path):
    sd = tmp_path / "state"
    write_creds(tmp_path / ".claude")
    await run_all(
        tmp_path, now=NOW - timedelta(hours=5), state_dir=sd, handler=ok_handler()
    )
    usage.clear_memo()
    write_login(tmp_path / ".claude")
    cache = tmp_path / "cache.json"
    entry = cache_entry("claude", NOW_MS - 3_600_000)  # velho p/ uso direto (>15min)
    entry["five_hour"] = {"percent": 61.0, "resets_at": "2026-11-19T18:00:00Z"}
    cache.write_text(json.dumps([entry]))
    r = (await run_all(tmp_path, state_dir=sd, cache=cache))[0]
    assert r.state == "idle" and r.five_hour.percent == 61.0
    assert r.stale_since == _dt(NOW_MS - 3_600_000)


async def test_idle_prefers_persisted_when_fresher_than_horizon(tmp_path):
    sd = tmp_path / "state"
    write_creds(tmp_path / ".claude")
    await run_all(
        tmp_path, now=NOW - timedelta(minutes=5), state_dir=sd, handler=ok_handler()
    )
    usage.clear_memo()
    write_login(tmp_path / ".claude")
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps([cache_entry("claude", NOW_MS - 3 * 3_600_000)]))
    r = (await run_all(tmp_path, state_dir=sd, cache=cache))[0]
    assert r.five_hour.percent == 37.0


async def test_fresh_horizon_expired_is_reclassified_idle_by_local_credentials(
    tmp_path,
):
    write_login(tmp_path / ".claude")
    cache = tmp_path / "cache.json"
    entry = cache_entry("claude", NOW_MS - 60_000)
    entry["state"] = "expired"
    cache.write_text(json.dumps([entry]))
    r = (await run_all(tmp_path, cache=cache))[0]
    assert r.state == "idle"
    assert r.five_hour.percent == 10.0  # janelas do próprio cache
    assert r.login_expires_at == _dt(LOGIN_FUTURE_MS)


async def test_fresh_horizon_expired_stays_expired_when_login_dead(tmp_path):
    write_login(tmp_path / ".claude", refresh_exp_ms=LOGIN_PAST_MS)
    cache = tmp_path / "cache.json"
    entry = cache_entry("claude", NOW_MS - 60_000)
    entry["state"] = "expired"
    cache.write_text(json.dumps([entry]))
    assert (await run_all(tmp_path, cache=cache))[0].state == "expired"


def idle(name="i", five=None, seven=None):
    base = acct(name, five, seven, state="idle")
    return AccountUsage(**{**base.__dict__, "stale_since": NOW - timedelta(hours=1)})


def test_format_idle_with_last_known():
    base = acct("a", 20, 100, state="idle")
    i = AccountUsage(
        **{
            **base.__dict__,
            "seven_day": Window(100.0, datetime(2026, 11, 20, 21, 0, tzinfo=UTC)),
        }
    )
    out = usage.format_usage([i], None, NOW)
    assert (
        "💤 sem leitura agora (renova ao usar) · último: 7d 100% · reset 20/11 21:00"
        in out
    )
    assert "▰" not in out and "⚠️" not in out


def test_format_idle_without_last_known_omits_ultimo():
    out = usage.format_usage([idle("a")], None, NOW)
    assert "💤 sem leitura agora (renova ao usar)" in out and "último" not in out


def test_format_expired_shows_relogar_with_date():
    login = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
    base = acct("a", state="expired")
    i = AccountUsage(**{**base.__dict__, "login_expires_at": login})
    assert "⚠️ relogar (login venceu 07/11)" in usage.format_usage([i], None, NOW)
    assert "⚠️ relogar" in usage.format_usage([acct("b", state="expired")], None, NOW)


def test_sort_idle_after_ok_usable_expired_last():
    items = [
        acct("exp", state="expired"),
        idle("idle-alta", 30, 60),
        acct("esgotada", 10, 100),
        acct("ok", 80, 80),
        idle("idle-baixa", 5, 5),
        idle("idle-sem-dado"),
    ]
    order = [i.name for i in usage.sort_usage(items)]
    assert order == ["ok", "idle-baixa", "idle-alta", "esgotada", "idle-sem-dado", "exp"]


def test_button_label_idle():
    assert usage.button_label(idle("x", 5, 9), None) == "💤 x · 9%"
    assert usage.button_label(idle("x"), "x") == "✅ 💤 x · —"


def test_pick_best_ok_beats_idle_idle_is_fallback_expired_never():
    assert usage.pick_best([idle("i", 1, 1), acct("o", 50, 50)]) == "o"
    assert usage.pick_best([idle("i", 1, 1), acct("e", 1, 1, state="expired")]) == "i"
    assert usage.pick_best([idle("i", 100, 1), idle("j")]) is None
    assert usage.pick_best([acct("e", 1, 1, state="expired")]) is None
