"""Uso 5h/7d por conta Claude (espelha horizon-core/src/claude_usage).

Só lê credenciais: nunca refresh, nunca escreve, nunca loga token/conteúdo.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import math
import os
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml

log = logging.getLogger(__name__)

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"  # único host que recebe o token
BETA_HEADER = "oauth-2025-04-20"
REQUEST_TIMEOUT_S = 10.0
EXPIRY_MARGIN_MS = 60_000
CACHE_MAX_AGE = timedelta(minutes=15)
MAX_BODY_BYTES = 1024 * 1024
MAX_ACCOUNT_BYTES = 8 * 1024 * 1024  # .claude.json é grande
LAST_FILE = "usage-last.json"
DASH = "—"

_DEFAULT_PROFILES = (
    ("claude", ".claude"),
    ("work", ".claude-work"),
    ("personal", ".claude-personal"),
    ("client", ".claude-client"),
)
_SEVERITY_RANK = {"normal": 0, "warning": 1, "critical": 2}
_SEVERITY_BY_RANK = {v: k for k, v in _SEVERITY_RANK.items()}
_STATES = {"ok", "idle", "expired", "missing", "stale"}


@dataclass(frozen=True)
class Profile:
    name: str
    config_dir: Path


@dataclass(frozen=True)
class Window:
    percent: float | None
    resets_at: datetime | None


@dataclass(frozen=True)
class AccountUsage:
    name: str
    display_name: str
    email: str | None
    five_hour: Window
    seven_day: Window
    severity: str
    state: str  # ok | idle | expired | missing | stale
    fetched_at: datetime
    stale_since: datetime | None = None  # idle: de quando é a última leitura mostrada
    login_expires_at: datetime | None = None  # refreshTokenExpiresAt (login vence)


_EMPTY = Window(None, None)


# --- perfis ------------------------------------------------------------


def _expand(raw: str, home: Path) -> Path:
    if raw == "~":
        return home
    if raw.startswith("~/"):
        return home / raw[2:]
    return Path(raw)


def load_profiles(horizon_config: Path, home: Path | None = None) -> list[Profile]:
    home = home or Path.home()
    profiles: list[Profile] = []
    try:
        data = yaml.safe_load(Path(horizon_config).read_text())
    except FileNotFoundError:
        data = None
    except (OSError, yaml.YAMLError, UnicodeDecodeError, RecursionError) as exc:
        log.warning(
            "config do Horizon ilegível (%s); usando perfis padrão", type(exc).__name__
        )
        data = None
    entries = data.get("claude_profiles") if isinstance(data, dict) else None
    for e in entries if isinstance(entries, list) else []:
        if (
            isinstance(e, dict)
            and isinstance(e.get("name"), str)
            and isinstance(e.get("config_dir"), str)
        ):
            profiles.append(Profile(e["name"], _expand(e["config_dir"], home)))
    return profiles or [Profile(n, home / d) for n, d in _DEFAULT_PROFILES]


# --- parse da resposta -------------------------------------------------


def _percent(obj: Any) -> float | None:
    if not isinstance(obj, dict):
        return None
    v = obj.get("utilization", obj.get("percent"))
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return float(v)


def _datetime(v: Any) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        dt = datetime.fromisoformat(v)
        dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        dt.astimezone(
            timezone.utc
        )  # extremos (ano 1/9999 + offset) estouram aqui, não na formatação
    except (ValueError, OverflowError):
        return None
    return dt


def _window(obj: Any) -> Window:
    if not isinstance(obj, dict):
        return _EMPTY
    return Window(_percent(obj), _datetime(obj.get("resets_at")))


def _is_five_hour_limit(l: dict) -> bool:
    kind = l.get("kind")
    return l.get("group") in ("five_hour", "session") or (
        isinstance(kind, str) and kind.startswith(("five_hour", "session"))
    )


def _is_weekly_limit(l: dict) -> bool:
    return l.get("kind") == "weekly_all" or l.get("group") == "weekly"


def _severity_for(*percents: float | None) -> str:
    top = max((p for p in percents if p is not None), default=0.0)
    return "critical" if top >= 90 else "warning" if top >= 75 else "normal"


def parse_usage_response(data: dict) -> tuple[Window, Window, str]:
    raw = data.get("limits") if isinstance(data, dict) else None
    limits = [l for l in raw if isinstance(l, dict)] if isinstance(raw, list) else []

    def pick(key: str, match) -> Window:
        direct = _window(data.get(key))
        if direct != _EMPTY:
            return direct
        # ordem de `limits[]` importa: weekly_all antes de group=weekly (como no Horizon)
        found = [l for l in limits if match(l)]
        if key == "seven_day":
            found.sort(key=lambda l: l.get("kind") != "weekly_all")
        return _window(found[0]) if found else _EMPTY

    five = pick("five_hour", _is_five_hour_limit)
    seven = pick("seven_day", _is_weekly_limit)
    api_rank = max(
        (
            _SEVERITY_RANK.get(sev, 0)
            for l in limits
            if l.get("is_active") is True and isinstance(sev := l.get("severity"), str)
        ),
        default=0,
    )
    computed_rank = _SEVERITY_RANK[_severity_for(five.percent, seven.percent)]
    return five, seven, _SEVERITY_BY_RANK[max(api_rank, computed_rank)]


# --- cache do Horizon ---------------------------------------------------


_EPOCH = datetime.fromtimestamp(0, tz=timezone.utc)


def _from_ms(ms: Any) -> datetime | None:
    if isinstance(ms, bool) or not isinstance(ms, int):
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def read_horizon_cache(
    path: Path, now: datetime, max_age: timedelta | None = CACHE_MAX_AGE
) -> list[AccountUsage] | None:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(data, list):
        return None
    items: list[AccountUsage] = []
    newest: datetime | None = None
    for e in data:
        if not isinstance(e, dict) or not isinstance(e.get("name"), str):
            continue
        fetched = _from_ms(e.get("fetched_at_ms"))
        if fetched is not None and (newest is None or fetched > newest):
            newest = fetched
        state, sev = e.get("state"), e.get("severity")
        state = state if isinstance(state, str) and state in _STATES else "stale"
        sev = sev if isinstance(sev, str) and sev in _SEVERITY_RANK else "normal"
        email = e.get("email")
        display = e.get("display_name")
        items.append(
            AccountUsage(
                name=e["name"],
                display_name=display
                if isinstance(display, str) and display
                else e["name"],
                email=email if isinstance(email, str) else None,
                five_hour=_window(e.get("five_hour")),
                seven_day=_window(e.get("seven_day")),
                severity=sev,
                state=state,
                fetched_at=fetched or _EPOCH,
            )
        )
    if not items or newest is None:
        return None
    if max_age is not None and now - newest > max_age:
        return None
    return items


# --- API ----------------------------------------------------------------


def _read_json(path: Path, max_bytes: int) -> Any:
    try:
        with open(path, "rb") as f:
            return json.loads(f.read(max_bytes))
    except (OSError, ValueError, RecursionError):
        return None


def _read_account(config_dir: Path, home: Path) -> tuple[str | None, str | None]:
    # perfil padrão (~/.claude): ~/.claude.json primeiro, depois o da pasta (como credentials.rs:129)
    paths = [config_dir / ".claude.json"]
    if config_dir == home / ".claude":
        paths.insert(0, home / ".claude.json")
    for path in paths:
        data = _read_json(path, MAX_ACCOUNT_BYTES)
        acc = data.get("oauthAccount") if isinstance(data, dict) else None
        if isinstance(acc, dict):
            name, email = acc.get("displayName"), acc.get("emailAddress")
            return (
                name if isinstance(name, str) and name else None,
                email if isinstance(email, str) and email else None,
            )
    return None, None


def _read_credentials(config_dir: Path) -> tuple[str, int, int | None, bool] | None:
    """(token, expiresAt ms, refreshTokenExpiresAt ms, tem refreshToken) ou None se
    ausente/malformada. Do refresh só timestamp e "vazio ou não"; nunca loga conteúdo."""
    data = _read_json(config_dir / ".credentials.json", 1024 * 1024)
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict):
        return None
    token, exp = oauth.get("accessToken"), oauth.get("expiresAt")
    token = token if isinstance(token, str) else ""
    exp = exp if isinstance(exp, int) and not isinstance(exp, bool) and token else 0
    refresh_exp = oauth.get("refreshTokenExpiresAt")
    if isinstance(refresh_exp, bool) or not isinstance(refresh_exp, int):
        refresh_exp = None
    refresh = oauth.get("refreshToken")
    return token, exp, refresh_exp, isinstance(refresh, str) and bool(refresh)


def _login_state(
    refresh_exp_ms: int | None, has_refresh: bool, now: datetime
) -> tuple[str, datetime | None]:
    """Access token inutilizável: `idle` (Claude Code renova ao usar) ou `expired` (relogar)."""
    if refresh_exp_ms is not None:
        alive = refresh_exp_ms > int(now.timestamp() * 1000)
    else:
        alive = has_refresh
    return ("idle" if alive else "expired"), _from_ms(refresh_exp_ms)


async def fetch_usage(
    profile: Profile, client: httpx.AsyncClient, now: datetime, home: Path | None = None
) -> AccountUsage:
    home = home or Path.home()
    acc_name, email = _read_account(profile.config_dir, home)

    def result(
        state: str, five: Window = _EMPTY, seven: Window = _EMPTY, sev: str = "normal"
    ) -> AccountUsage:
        return AccountUsage(
            profile.name, acc_name or profile.name, email, five, seven, sev, state, now
        )

    creds = _read_credentials(profile.config_dir)
    if creds is None:
        return result("missing")
    token, expires_ms, refresh_exp_ms, has_refresh = creds
    login_state, login_expires = _login_state(refresh_exp_ms, has_refresh, now)

    def unusable() -> AccountUsage:
        return replace(result(login_state), login_expires_at=login_expires)

    if not token or expires_ms <= int(now.timestamp() * 1000) + EXPIRY_MARGIN_MS:
        return unusable()
    try:
        resp = await client.get(
            USAGE_URL,
            headers={"Authorization": f"Bearer {token}", "anthropic-beta": BETA_HEADER},
            timeout=REQUEST_TIMEOUT_S,
            follow_redirects=False,
        )
    except (httpx.HTTPError, ValueError, OverflowError, OSError) as exc:
        # ValueError cobre UnicodeEncodeError (token com chars inválidos); só o tipo vai pro log
        log.warning("uso %s: falha de rede (%s)", profile.name, type(exc).__name__)
        return result("stale")
    if resp.status_code in (401, 403):
        return unusable()
    if resp.status_code != 200 or len(resp.content) > MAX_BODY_BYTES:
        log.warning("uso %s: HTTP %s", profile.name, resp.status_code)
        return result("stale")
    try:
        body = resp.json()
        if not isinstance(body, dict):
            return result("stale")
        five, seven, sev = parse_usage_response(body)
    except (ValueError, OverflowError, TypeError, RecursionError) as exc:
        log.warning("uso %s: resposta inválida (%s)", profile.name, type(exc).__name__)
        return result("stale")
    return replace(result("ok", five, seven, sev), login_expires_at=login_expires)


_MEMO: dict[tuple, tuple[datetime, list[AccountUsage]]] = {}
_LAST: dict[str, tuple[datetime, Window, Window]] = {}  # última leitura boa por conta


def clear_memo() -> None:
    _MEMO.clear()
    _LAST.clear()


def _has_reading(w5: Window, w7: Window) -> bool:
    return w5.percent is not None or w7.percent is not None


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _dump_window(w: Window) -> dict:
    return {"percent": w.percent, "resets_at": _iso(w.resets_at)}


def _read_last_file(state_dir: Path | None) -> dict[str, tuple[datetime, Window, Window]]:
    out: dict[str, tuple[datetime, Window, Window]] = {}
    if state_dir is None:
        return out
    data = _read_json(Path(state_dir) / LAST_FILE, MAX_BODY_BYTES)
    for name, e in data.items() if isinstance(data, dict) else []:
        if not isinstance(e, dict):
            continue
        fetched = _datetime(e.get("fetched_at"))
        five, seven = _window(e.get("five_hour")), _window(e.get("seven_day"))
        if fetched is not None and _has_reading(five, seven):
            out[name] = (fetched, five, seven)
    return out


def _write_last_file(
    state_dir: Path, known: dict[str, tuple[datetime, Window, Window]]
) -> None:
    payload = {
        name: {
            "five_hour": _dump_window(five),
            "seven_day": _dump_window(seven),
            "fetched_at": fetched.isoformat(),
        }
        for name, (fetched, five, seven) in known.items()
    }
    state_dir = Path(state_dir)
    tmp = state_dir / f"{LAST_FILE}.{os.getpid()}.tmp"
    try:
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, state_dir / LAST_FILE)
    except (OSError, ValueError) as exc:
        log.warning("uso: não gravou %s (%s)", LAST_FILE, type(exc).__name__)
        try:
            tmp.unlink()
        except OSError:
            pass


def _remember(items: list[AccountUsage], state_dir: Path | None) -> None:
    """Guarda a última leitura boa (memória + arquivo) das contas `ok`."""
    fresh = {
        i.name: (i.fetched_at, i.five_hour, i.seven_day)
        for i in items
        if i.state == "ok" and _has_reading(i.five_hour, i.seven_day)
    }
    if not fresh:
        return
    _LAST.update(fresh)
    if state_dir is None:
        return
    known = _read_last_file(state_dir)
    if all(known.get(n) == v for n, v in fresh.items()):
        return
    known.update(fresh)
    _write_last_file(state_dir, known)


def _fill_idle(
    items: list[AccountUsage],
    horizon_cache: Path,
    now: datetime,
    state_dir: Path | None,
) -> list[AccountUsage]:
    """Conta ociosa mantém a última leitura conhecida (memória, arquivo ou cache do Horizon: a mais nova)."""
    if not any(i.state == "idle" for i in items):
        return items
    persisted = _read_last_file(state_dir)
    horizon = {
        i.name: (i.fetched_at, i.five_hour, i.seven_day)
        for i in read_horizon_cache(horizon_cache, now, max_age=None) or []
        if _has_reading(i.five_hour, i.seven_day)
    }
    out = []
    for i in items:
        if i.state != "idle":
            out.append(i)
            continue
        cands = [c for c in (_LAST.get(i.name), persisted.get(i.name), horizon.get(i.name)) if c]
        if _has_reading(i.five_hour, i.seven_day):
            cands.append((i.fetched_at, i.five_hour, i.seven_day))
        if not cands:
            out.append(i)
            continue
        fetched, five, seven = max(cands, key=lambda c: c[0])
        out.append(replace(i, five_hour=five, seven_day=seven, stale_since=fetched))
    return out


def _reclassify(item: AccountUsage, profile: Profile, now: datetime) -> AccountUsage:
    """Cache do Horizon diz `expired`: confere o login local (só timestamps) antes de pedir relogar."""
    if item.state != "expired":
        return item
    creds = _read_credentials(profile.config_dir)
    if creds is None:
        return item
    state, login_expires = _login_state(creds[2], creds[3], now)
    return replace(item, state=state, login_expires_at=login_expires)


async def get_all_usage(
    profiles: list[Profile],
    horizon_cache: Path,
    client: httpx.AsyncClient,
    now: datetime,
    memo_ttl_s: int = 300,
    home: Path | None = None,
    state_dir: Path | None = None,
) -> list[AccountUsage]:
    cached = read_horizon_cache(horizon_cache, now)
    if cached is not None:
        by_name = {i.name: i for i in cached}
        if all(p.name in by_name for p in profiles):
            items = [_reclassify(by_name[p.name], p, now) for p in profiles]
            _remember(items, state_dir)
            return _fill_idle(items, horizon_cache, now, state_dir)
    key = tuple((p.name, str(p.config_dir)) for p in profiles)
    hit = _MEMO.get(key)
    if hit and timedelta(0) <= now - hit[0] < timedelta(seconds=memo_ttl_s):
        return list(hit[1])
    results = await asyncio.gather(
        *(fetch_usage(p, client, now, home) for p in profiles), return_exceptions=True
    )
    items: list[AccountUsage] = []
    for p, r in zip(profiles, results):
        if isinstance(r, AccountUsage):
            items.append(r)
            continue
        if not isinstance(r, Exception):  # CancelledError etc.: não engolir
            raise r
        log.warning("uso %s: erro inesperado (%s)", p.name, type(r).__name__)
        items.append(
            AccountUsage(p.name, p.name, None, _EMPTY, _EMPTY, "normal", "stale", now)
        )
    _remember(items, state_dir)
    items = _fill_idle(items, horizon_cache, now, state_dir)
    _MEMO[key] = (now, items)
    return list(items)


# --- apresentação --------------------------------------------------------


def mask_email(email: str | None) -> str:
    if not email or "@" not in email:
        return DASH
    local, _, domain = email.partition("@")
    return f"{local[:1]}…@{domain}" if local else DASH


BAR_CELLS = 10
_STATE_WARN = {
    "stale": "⚠️ desatualizado",
    "missing": "⚠️ sem credencial",
}


def _pct(w: Window) -> str:
    return DASH if w.percent is None else f"{int(w.percent + 0.5)}%"


def _bar(percent: float) -> str:
    """10 células; floor(p/10), mínimo 1 se há uso (9% -> 1, 97% -> 9, 100% -> 10)."""
    filled = min(BAR_CELLS, int(percent // 10))
    if percent > 0:
        filled = max(1, filled)
    return "▰" * filled + "▱" * (BAR_CELLS - filled)


def _usable(i: AccountUsage) -> bool:
    return (
        i.state == "ok"
        and round(i.five_hour.percent or 0) < 100
        and round(i.seven_day.percent or 0) < 100
    )


def _idle_usable(i: AccountUsage) -> bool:
    """Ociosa com última leitura conhecida e nenhuma janela esgotada (renova ao usar)."""
    return (
        i.state == "idle"
        and _has_reading(i.five_hour, i.seven_day)
        and round(i.five_hour.percent or 0) < 100
        and round(i.seven_day.percent or 0) < 100
    )


def sort_usage(items: list[AccountUsage]) -> list[AccountUsage]:
    """Ordem do pick_best primeiro (menor 5h, depois 7d); ociosas após as ok; esgotadas e inválidas por último."""

    def key(i: AccountUsage):
        if _usable(i):
            rank = 0
        elif _idle_usable(i):
            rank = 1
        elif i.state == "ok":
            rank = 2
        else:
            rank = 3 if i.state == "idle" else 4
        return (rank, i.five_hour.percent or 0.0, i.seven_day.percent or 0.0)

    return sorted(items, key=key)


def _line(label: str, w: Window, fmt: str, tz) -> str | None:
    if w.percent is None:
        return None
    reset = (
        f" · reset {w.resets_at.astimezone(tz).strftime(fmt)}" if w.resets_at else ""
    )
    return f"   {label} {_bar(w.percent)} {_pct(w)}{reset}"


def button_label(i: AccountUsage, active: str | None) -> str:
    """`🟢 work · 9%` (maior das janelas); `✅ ` na ativa."""
    pcts = [p for p in (i.five_hour.percent, i.seven_day.percent) if p is not None]
    pct = f"{int(max(pcts) + 0.5)}%" if pcts else DASH
    emoji = "💤" if i.state == "idle" else _emoji(i)
    return f"{'✅ ' if i.name == active else ''}{emoji} {i.name} · {pct}"


def _emoji(i: AccountUsage) -> str:
    if i.state != "ok" and i.five_hour.percent is None and i.seven_day.percent is None:
        return "⚪"
    rank = max(
        _SEVERITY_RANK.get(i.severity, 0),
        _SEVERITY_RANK[_severity_for(i.five_hour.percent, i.seven_day.percent)],
    )
    return ("🟢", "🟡", "🔴")[rank]


def _idle_text(i: AccountUsage, tz) -> str:
    text = "💤 sem leitura agora (renova ao usar)"
    known = [
        (w.percent, label, w, fmt)
        for label, w, fmt in (
            ("5h", i.five_hour, "%H:%M"),
            ("7d", i.seven_day, "%d/%m %H:%M"),
        )
        if w.percent is not None
    ]
    if not known:
        return text
    _, label, w, fmt = max(known, key=lambda k: k[0])  # empate: 7d (último da lista)
    reset = f" · reset {w.resets_at.astimezone(tz).strftime(fmt)}" if w.resets_at else ""
    return f"{text} · último: {label} {_pct(w)}{reset}"


def _expired_text(i: AccountUsage, tz) -> str:
    if i.login_expires_at is None:
        return "⚠️ relogar"
    return f"⚠️ relogar (login venceu {i.login_expires_at.astimezone(tz).strftime('%d/%m')})"


def format_usage(items: list[AccountUsage], active: str | None, now: datetime) -> str:
    """Mensagem em HTML do Telegram (parse_mode=HTML): um bloco por conta, melhor primeiro."""
    if not items:
        return "Nenhuma conta configurada."
    tz = now.tzinfo
    blocks = []
    for i in sort_usage(items):
        mark = "✅ " if i.name == active else ""
        lines = [f"{mark}<b>{html.escape(i.name)}</b> {_emoji(i)}"]
        windows = (
            _line("5h", i.five_hour, "%H:%M", tz),
            _line("7d", i.seven_day, "%d/%m %H:%M", tz),
        )
        if i.state == "idle":
            lines.append(f"   {_idle_text(i, tz)}")
            blocks.append("\n".join(lines))
            continue
        lines += [w for w in windows if w]
        if i.state == "expired":
            lines.append(f"   {_expired_text(i, tz)}")
        elif i.state != "ok":
            lines.append(f"   {_STATE_WARN.get(i.state, html.escape(i.state))}")
        elif not any(windows):
            lines.append("   sem dados de uso")
        blocks.append("\n".join(lines))
    return "<b>Contas Claude</b>\n\n" + "\n\n".join(blocks)


def pick_best(items: list[AccountUsage]) -> str | None:
    """Menor 5h entre `ok` (null conta como 0; empate -> 7d). Janela esgotada (>=100) fica de fora."""
    usable = [i for i in items if _usable(i)] or [i for i in items if _idle_usable(i)]
    if not usable:
        return None
    return min(
        usable, key=lambda i: (i.five_hour.percent or 0.0, i.seven_day.percent or 0.0)
    ).name
