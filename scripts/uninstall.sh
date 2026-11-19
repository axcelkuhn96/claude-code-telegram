#!/usr/bin/env bash
# Remove o serviço systemd --user do claude-telegram. Não apaga o repo, o .venv nem os segredos/estado
# em ~/.config/claude-telegram. Não mexe em linger. Idempotente.
set -euo pipefail

UNIT_NAME="claude-telegram.service"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

systemctl --user disable --now "$UNIT_NAME" 2>/dev/null || true
rm -f "${UNIT_DIR}/${UNIT_NAME}"
systemctl --user daemon-reload
systemctl --user reset-failed "$UNIT_NAME" 2>/dev/null || true
echo "serviço removido. Segredos e estado ficam em ~/.config/claude-telegram (apague à mão se quiser)."
echo "O MCP registrado nas contas (se foi): rode 'claude mcp remove --scope user telegram-bot' em cada conta."
