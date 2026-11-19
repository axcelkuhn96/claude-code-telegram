from pathlib import Path

import pytest

from bot import config

ENV = {
    "TELEGRAM_BOT_TOKEN": "tok-SECRET-1",
    "OPENAI_API_KEY": "key-SECRET-2",
    "ALLOWED_PHONE": "5511999999999",
}


def test_load_from_env_with_defaults(tmp_path):
    cfg = config.load(env=dict(ENV), home=tmp_path)
    assert cfg.bot_token == "tok-SECRET-1"
    assert cfg.openai_key == "key-SECRET-2"
    assert cfg.allowed_phone == "5511999999999"
    assert cfg.home == tmp_path
    assert cfg.state_dir == tmp_path / ".config" / "claude-telegram"
    assert cfg.claude_bin == tmp_path / ".local" / "bin" / "claude"
    assert cfg.local_port == 28765


def _write_env_file(home: Path, text: str) -> None:
    d = home / ".config" / "claude-telegram"
    d.mkdir(parents=True)
    (d / "env").write_text(text)


def test_falls_back_to_env_file(tmp_path):
    _write_env_file(
        tmp_path,
        "# comentario\n\nTELEGRAM_BOT_TOKEN=tok-file\n"
        "export OPENAI_API_KEY='key-file'\n"
        'ALLOWED_PHONE="5511999999999"\n',
    )
    cfg = config.load(env={}, home=tmp_path)
    assert cfg.bot_token == "tok-file"
    assert cfg.openai_key == "key-file"
    assert cfg.allowed_phone == "5511999999999"


def test_env_has_precedence_over_file(tmp_path):
    _write_env_file(
        tmp_path, "TELEGRAM_BOT_TOKEN=tok-file\nOPENAI_API_KEY=k\nALLOWED_PHONE=1\n"
    )
    cfg = config.load(env={"TELEGRAM_BOT_TOKEN": "tok-env"}, home=tmp_path)
    assert cfg.bot_token == "tok-env"
    assert cfg.openai_key == "k"


def test_missing_required_names_var_not_values(tmp_path):
    env = {"TELEGRAM_BOT_TOKEN": "tok-SECRET-1", "ALLOWED_PHONE": "5511999999999"}
    with pytest.raises(ValueError) as exc:
        config.load(env=env, home=tmp_path)
    msg = str(exc.value)
    assert "OPENAI_API_KEY" in msg
    assert "tok-SECRET-1" not in msg
    assert "5511999999999" not in msg


def test_empty_value_counts_as_missing(tmp_path):
    env = dict(ENV, TELEGRAM_BOT_TOKEN="")
    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
        config.load(env=env, home=tmp_path)


def test_file_garbage_lines_ignored(tmp_path):
    _write_env_file(tmp_path, "sem igual\n=semchave\nTELEGRAM_BOT_TOKEN=a\n")
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        config.load(env={"ALLOWED_PHONE": "1"}, home=tmp_path)


def test_custom_port(tmp_path):
    cfg = config.load(env=dict(ENV, BOT_LOCAL_PORT="9000"), home=tmp_path)
    assert cfg.local_port == 9000


@pytest.mark.parametrize("bad", ["abc", "0", "70000", "-1", ""])
def test_invalid_port(tmp_path, bad):
    if bad == "":
        # vazio = não definido -> default
        assert (
            config.load(env=dict(ENV, BOT_LOCAL_PORT=bad), home=tmp_path).local_port
            == 28765
        )
        return
    with pytest.raises(ValueError, match="BOT_LOCAL_PORT"):
        config.load(env=dict(ENV, BOT_LOCAL_PORT=bad), home=tmp_path)


def test_state_dir_override_env(tmp_path):
    other = tmp_path / "outro"
    cfg = config.load(
        env=dict(ENV, CLAUDE_TELEGRAM_STATE_DIR=str(other)), home=tmp_path
    )
    assert cfg.state_dir == other


def test_repr_does_not_leak_secrets(tmp_path):
    cfg = config.load(env=dict(ENV), home=tmp_path)
    text = repr(cfg) + str(cfg)
    assert "tok-SECRET-1" not in text
    assert "key-SECRET-2" not in text
