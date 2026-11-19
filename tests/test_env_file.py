"""`python -m bot.main` manual: chaves do <state_dir>/env entram no ambiente (processo vence)."""

from __future__ import annotations

import logging
import os

import pytest

from bot import config, main as main_mod

SEGREDO_TOTP = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"


def write_env(tmp_path, text):
    d = tmp_path / "estado"
    d.mkdir(exist_ok=True)
    (d / "env").write_text(text)
    return d


def env_for(d, **extra):
    return {"CLAUDE_TELEGRAM_STATE_DIR": str(d), **extra}


def test_aplica_as_chaves_conhecidas_que_faltam_no_processo(tmp_path):
    d = write_env(
        tmp_path,
        "TELEGRAM_BOT_TOKEN=t\nOPENAI_API_KEY=k\nALLOWED_PHONE=5511\n"
        f"TOTP_SECRET={SEGREDO_TOTP}\nTOTP_UNLOCK_HOURS=6\n"
        "WEBAPP_URL=https://painel.example\nWEBAPP_PORT=29000\nBOT_LOCAL_PORT=29001\n",
    )
    environ = env_for(d)
    applied = config.apply_env_file(environ, home=tmp_path)
    assert set(applied) == set(config.KNOWN_KEYS)
    assert (
        environ["TOTP_SECRET"] == SEGREDO_TOTP
        and environ["WEBAPP_URL"] == "https://painel.example"
    )
    assert environ["WEBAPP_PORT"] == "29000" and environ["TOTP_UNLOCK_HOURS"] == "6"


def test_o_ambiente_do_processo_vence_o_arquivo(tmp_path):
    d = write_env(
        tmp_path,
        f"TOTP_SECRET={SEGREDO_TOTP}\nWEBAPP_URL=https://do-arquivo\nBOT_LOCAL_PORT=1111\n",
    )
    environ = env_for(d, WEBAPP_URL="https://do-processo", BOT_LOCAL_PORT="2222")
    applied = config.apply_env_file(environ, home=tmp_path)
    assert applied == ["TOTP_SECRET"]
    assert (
        environ["WEBAPP_URL"] == "https://do-processo"
        and environ["BOT_LOCAL_PORT"] == "2222"
    )


def test_chave_definida_vazia_no_processo_conta_como_presente(tmp_path):
    d = write_env(tmp_path, f"TOTP_SECRET={SEGREDO_TOTP}\n")
    environ = env_for(d, TOTP_SECRET="")  # quem esvazia de propósito não é sobrescrito
    assert config.apply_env_file(environ, home=tmp_path) == []
    assert environ["TOTP_SECRET"] == ""


def test_ignora_chaves_desconhecidas_comentarios_e_valores_vazios(tmp_path):
    d = write_env(
        tmp_path,
        "# comentário\nPATH=/evil\nLD_PRELOAD=/x.so\nCLAUDE_TELEGRAM_STATE_DIR=/outro\n"
        "TOTP_SECRET=\nexport WEBAPP_URL='https://aspas.example'\n",
    )
    environ = env_for(d)
    applied = config.apply_env_file(environ, home=tmp_path)
    assert (
        applied == ["WEBAPP_URL"] and environ["WEBAPP_URL"] == "https://aspas.example"
    )
    assert "PATH" not in environ and "LD_PRELOAD" not in environ
    assert environ["CLAUDE_TELEGRAM_STATE_DIR"] == str(
        d
    )  # nem o diretório de estado muda


def test_sem_arquivo_nao_aplica_nada(tmp_path):
    environ = env_for(tmp_path / "nao-existe")
    assert config.apply_env_file(environ, home=tmp_path) == []
    assert set(environ) == {"CLAUDE_TELEGRAM_STATE_DIR"}


def test_usa_o_estado_padrao_na_home_quando_nao_ha_override(tmp_path):
    d = tmp_path / ".config" / "claude-telegram"
    d.mkdir(parents=True)
    (d / "env").write_text(f"TOTP_SECRET={SEGREDO_TOTP}\n")
    environ: dict[str, str] = {}
    assert config.apply_env_file(environ, home=tmp_path) == ["TOTP_SECRET"]


def test_load_le_a_porta_local_do_arquivo(tmp_path):
    d = write_env(
        tmp_path,
        "TELEGRAM_BOT_TOKEN=t\nOPENAI_API_KEY=k\nALLOWED_PHONE=5511\nBOT_LOCAL_PORT=29001\n",
    )
    assert config.load(env_for(d), home=tmp_path).local_port == 29001
    # processo vence; porta inválida no arquivo continua sendo erro de configuração
    cfg = config.load(env_for(d, BOT_LOCAL_PORT="29555"), home=tmp_path)
    assert cfg.local_port == 29555
    d2 = (
        write_env(
            tmp_path / "x",
            "TELEGRAM_BOT_TOKEN=t\nOPENAI_API_KEY=k\nALLOWED_PHONE=5511\nBOT_LOCAL_PORT=abc\n",
        )
        if (tmp_path / "x").mkdir() is None
        else None
    )
    with pytest.raises(ValueError, match="BOT_LOCAL_PORT"):
        config.load(env_for(d2), home=tmp_path)


def test_totp_vindo_do_arquivo_liga_a_trava_no_main(tmp_path):
    """O caminho do `python -m bot.main` manual: apply_env_file -> totp_settings."""
    d = write_env(tmp_path, f"TOTP_SECRET={SEGREDO_TOTP}\nTOTP_UNLOCK_HOURS=3\n")
    environ = env_for(d)
    assert main_mod.totp_settings(environ) == (
        None,
        12.0,
    )  # antes: sem trava, em silêncio
    config.apply_env_file(environ, home=tmp_path)
    secret, hours = main_mod.totp_settings(environ)
    assert secret == SEGREDO_TOTP and hours == 3.0


def test_aviso_totp_desligado_e_info_quando_ligado(caplog):
    with caplog.at_level(logging.INFO, logger="bot.main"):
        main_mod.log_totp_state(None, 12.0)
    rec = [r for r in caplog.records if "TOTP desligado" in r.getMessage()]
    assert len(rec) == 1 and rec[0].levelno == logging.WARNING
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="bot.main"):
        main_mod.log_totp_state(SEGREDO_TOTP, 6.0)
    assert not any("desligado" in r.getMessage() for r in caplog.records)
    assert any(
        "ativa" in r.getMessage() and r.levelno == logging.INFO for r in caplog.records
    )
    assert SEGREDO_TOTP not in caplog.text  # o segredo nunca vai para o log


def test_log_das_chaves_aplicadas_so_tem_nomes(tmp_path, monkeypatch, caplog):
    d = write_env(
        tmp_path,
        "TELEGRAM_BOT_TOKEN=tok-secreto-xyz\nOPENAI_API_KEY=sk-secreto-abc\nALLOWED_PHONE=5511\n"
        f"TOTP_SECRET={SEGREDO_TOTP}\n",
    )
    # cópia do ambiente: o main() escreve nele e isso não pode vazar para os outros testes
    monkeypatch.setattr(
        os, "environ", {**os.environ, "CLAUDE_TELEGRAM_STATE_DIR": str(d)}
    )
    for k in config.KNOWN_KEYS:
        os.environ.pop(k, None)
    monkeypatch.setattr(
        main_mod, "build", lambda *a, **k: (_ for _ in ()).throw(SystemExit(0))
    )
    with caplog.at_level(logging.INFO), pytest.raises(SystemExit):
        main_mod.main()
    texto = caplog.text
    assert "chaves lidas do arquivo de env" in texto and "TOTP_SECRET" in texto
    for segredo in ("tok-secreto-xyz", "sk-secreto-abc", SEGREDO_TOTP):
        assert segredo not in texto
