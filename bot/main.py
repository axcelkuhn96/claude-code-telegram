"""Entrada do bot: `python -m bot.main`. Long polling + API local + watchdog systemd."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import importlib.util
import logging
import os
import re
import sys

import httpx
from telegram import BotCommand
from telegram.error import TelegramError
from telegram.ext import Application

from bot import config, state, totp
from bot.handlers import COMMANDS, DEFAULT_UNLOCK_HOURS, Bot
from bot.localapi import (
    PORT_FILE,
    SECRET_FILE,
    LocalApi,
    new_secret,
    write_port,
    write_secret,
)
from bot.notify import sd_notify

log = logging.getLogger("bot.main")

WATCHDOG_INTERVAL_S = 20.0
POLLING_WAIT_S = 60.0  # intervalo entre avisos de "aguardando rede"
POLL_STEP_S = 0.5
CONNECT_TIMEOUT_S = 15.0
EXIT_PORT_BUSY = 3
POOL_SIZE = 8
POOL_TIMEOUT_S = 10.0


async def watchdog(
    app: Application, bot: Bot, interval: float = WATCHDOG_INTERVAL_S
) -> None:
    """READY=1 quando o polling sobe; WATCHDOG=1 a cada `interval` só enquanto ele vive."""

    def alive() -> bool:
        return bool(app.running and app.updater and app.updater.running)

    # Nunca desiste: a unit usa TimeoutStartSec=infinity, então sem READY o serviço ficaria em
    # `activating` e sem watchdog. Rede caída no start = espera (e avisa no log) até o polling subir.
    waited = 0.0
    while not alive():
        if waited and waited % POLLING_WAIT_S < POLL_STEP_S:
            log.warning("polling ainda não subiu (%ss); aguardando rede", int(waited))
            sd_notify("STATUS=aguardando rede")
        await asyncio.sleep(POLL_STEP_S)
        waited += POLL_STEP_S
    sd_notify("READY=1\nSTATUS=polling ativo")
    log.info("polling ativo")
    owner = bot.state.owner_id
    if owner is not None:
        await bot.send(owner, "🟢 no ar")
    while True:
        if alive():
            sd_notify("WATCHDOG=1")
        await asyncio.sleep(interval)


async def start_api(api: LocalApi) -> None:
    """Sobe a API local; porta ocupada = erro claro e saída não-zero (o systemd tenta de novo)."""
    try:
        await api.start()
    except OSError as e:
        log.error(
            "porta %s indisponível para a API local (%s); ajuste BOT_LOCAL_PORT",
            api.port,
            e.strerror or type(e).__name__,
        )
        raise SystemExit(EXIT_PORT_BUSY) from None


def _load_webapp():
    """bot.webapp (Mini App, C4); None se o módulo ainda não existe."""
    if importlib.util.find_spec("bot.webapp") is None:
        return None
    return importlib.import_module("bot.webapp")


async def start_webapp(bot: Bot, cfg: config.Config):
    """Sobe o backend do Mini App; falha aqui (porta, módulo ausente) não derruba o bot."""
    try:
        webapp = _load_webapp()
        if webapp is None:
            log.info("Mini App indisponível (bot.webapp ausente)")
            return None
        server = await webapp.start(bot, cfg)
    except Exception as e:  # noqa: BLE001 - o bot funciona sem o painel
        log.error("Mini App não subiu (%s)", type(e).__name__)
        return None
    log.info("Mini App ativo")
    return server


async def stop_webapp(server) -> None:
    webapp = _load_webapp()
    if server is None or webapp is None:
        return
    try:
        await webapp.stop(server)
    except Exception as e:  # noqa: BLE001 - no shutdown, segue limpando
        log.warning("Mini App: erro ao parar (%s)", type(e).__name__)


def totp_settings(env: dict[str, str] | None = None) -> tuple[str | None, float]:
    """(TOTP_SECRET normalizado ou None, TOTP_UNLOCK_HOURS). Segredo definido mas inválido
    = ValueError (iniciar destrancado seria inseguro; trancado para sempre, inútil)."""
    env = os.environ if env is None else env
    raw = (env.get("TOTP_SECRET") or "").strip()
    secret = None
    if raw:
        if not totp.valid_secret(raw):
            raise ValueError("TOTP_SECRET inválido (esperado base32 de ≥ 80 bits)")
        secret = re.sub(r"[\s-]", "", raw).upper()
    try:
        hours = float(env.get("TOTP_UNLOCK_HOURS") or DEFAULT_UNLOCK_HOURS)
    except ValueError:
        hours = DEFAULT_UNLOCK_HOURS
    if not 0 < hours <= 24 * 30:
        hours = DEFAULT_UNLOCK_HOURS
    return secret, hours


def build(
    cfg: config.Config,
    st: state.State,
    totp_secret: str | None = None,
    unlock_hours: float = DEFAULT_UNLOCK_HOURS,
) -> Application:
    bot = Bot(cfg, st, totp_secret=totp_secret, unlock_hours=unlock_hours)
    secret = new_secret()
    api = LocalApi(
        secret,
        bot.notify,
        bot.ask,
        cfg.local_port,
        on_send_file=bot.send_file,
        on_restart_when_idle=bot.restart_when_idle,
    )
    tasks: list[asyncio.Task] = []
    webapp_server: list = []

    async def post_init(app: Application) -> None:
        bot.tg = app.bot
        bot.client = httpx.AsyncClient()
        try:
            await start_api(api)
        except SystemExit as e:
            app.bot_data["exit_code"] = (
                e.code
            )  # o PTB engole SystemExit: main() relê daqui
            raise
        write_secret(cfg.state_dir, secret)  # só depois do bind: sem segredo órfão
        write_port(cfg.state_dir, api.port)  # o MCP de sessões de terminal lê daqui
        log.info("API local em 127.0.0.1:%s", api.port)
        with contextlib.suppress(TelegramError):
            await app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS])
        await (
            bot.apply_menu_button()
        )  # "Painel" (Mini App), se WEBAPP_URL e dono vinculado
        webapp_server.append(await start_webapp(bot, cfg))
        await (
            bot.adopt_hosts()
        )  # sessões hospedadas (tmux) que sobreviveram ao reinício
        await bot.post_interrupted()  # "⏸ interrompido pelo reinício" + 🔁 Reenviar
        bot.pinned_enabled = (
            True  # barra de status fixada + placeholder do campo de texto
        )
        tasks.append(asyncio.create_task(bot.status_loop()))
        tasks.append(asyncio.create_task(watchdog(app, bot)))

    async def post_shutdown(app: Application) -> None:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await bot.shutdown()
        if bot._pinned is not None:
            await bot._pinned.close()
        for server in webapp_server:
            await stop_webapp(server)
        await api.stop()
        for name in (SECRET_FILE, PORT_FILE):
            (cfg.state_dir / name).unlink(missing_ok=True)
        if bot.client is not None:
            await bot.client.aclose()

    app = (
        Application.builder()
        .token(cfg.bot_token)
        .connect_timeout(
            CONNECT_TIMEOUT_S
        )  # rede instável: o default (5s) estoura no TLS
        .get_updates_connect_timeout(CONNECT_TIMEOUT_S)
        .connection_pool_size(POOL_SIZE)  # concurrent_updates: envios simultâneos
        .pool_timeout(POOL_TIMEOUT_S)
        .concurrent_updates(True)  # /parar, callbacks e replies não esperam voz/rede
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    bot.register(app)
    return app


class RedactToken(logging.Filter):
    """Troca o token do bot por *** na mensagem, no traceback e no stack_info (a URL do Telegram
    leva o token e aparece em exceções de rede)."""

    def __init__(self, token: str) -> None:
        super().__init__()
        self.token = token

    def _clean(self, text: str) -> str:
        return text.replace(self.token, "***") if self.token else text

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._clean(record.getMessage())
        record.args = None
        if record.exc_info:
            record.exc_text = self._clean(
                logging.Formatter().formatException(record.exc_info)
            )
        elif record.exc_text:
            record.exc_text = self._clean(record.exc_text)
        if record.stack_info:
            record.stack_info = self._clean(record.stack_info)
        return True


def log_totp_state(totp_secret: str | None, unlock_hours: float) -> None:
    if totp_secret:
        log.info("trava TOTP ativa (destrave de %g h)", unlock_hours)
    else:
        log.warning("TOTP desligado: TOTP_SECRET ausente; o bot atende sem trava")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    # httpx loga a URL completa (com o token do bot) em INFO
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # `python -m bot.main` manual: traz do <state_dir>/env o que o systemd já injetaria
    # (TOTP_SECRET, WEBAPP_URL…); o ambiente do processo vence. Só nomes vão para o log.
    applied = config.apply_env_file()
    if applied:
        log.info("chaves lidas do arquivo de env: %s", ", ".join(applied))
    try:
        cfg = config.load()
    except ValueError as e:
        log.error("%s", e)  # só o nome da variável
        return 2
    for handler in logging.getLogger().handlers:
        handler.addFilter(RedactToken(cfg.bot_token))
    try:
        totp_secret, unlock_hours = totp_settings()
    except ValueError as e:
        log.error("%s", e)
        return 2
    log_totp_state(totp_secret, unlock_hours)
    st = state.load(cfg.state_dir / "state.json")
    app = build(cfg, st, totp_secret, unlock_hours)
    # bootstrap_retries=-1: sem rede no start, tenta de novo em vez de morrer
    app.run_polling(allowed_updates=["message", "callback_query"], bootstrap_retries=-1)
    return int(app.bot_data.get("exit_code", 0))


if __name__ == "__main__":
    sys.exit(main())
