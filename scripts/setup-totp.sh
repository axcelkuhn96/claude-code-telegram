#!/usr/bin/env bash
# Configura o TOTP (trava do bot): gera o segredo, grava TOTP_SECRET no env e mostra o QR.
# Rode num terminal FORA do Claude Code: o segredo aparece na tela (QR/URI).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${CLAUDE_TELEGRAM_STATE_DIR:-$HOME/.config/claude-telegram}/env"
PY="$REPO/.venv/bin/python"

if [ -n "${CLAUDECODE:-}" ] || [ -n "${CLAUDE_CODE_ENTRYPOINT:-}" ]; then
  echo "Rode este script num terminal FORA do Claude Code (o segredo TOTP é mostrado na tela)." >&2
  exit 1
fi
if [ ! -t 1 ]; then
  echo "Rode num terminal interativo (a saída é o QR do autenticador)." >&2
  exit 1
fi
[ -x "$PY" ] || { echo "venv ausente: rode scripts/install.sh primeiro." >&2; exit 1; }

if grep -qE '^[[:space:]]*(export[[:space:]]+)?TOTP_SECRET=[^[:space:]]' "$ENV_FILE" 2>/dev/null; then
  read -r -p "Já existe um TOTP configurado. Substituir (os códigos antigos param de valer)? [s/N] " ans
  case "$ans" in s|S|y|Y) ;; *) echo "Nada alterado."; exit 0 ;; esac
fi

URI="$("$PY" "$REPO/scripts/totp_setup.py" "$ENV_FILE")"

echo
if command -v qrencode >/dev/null 2>&1; then
  echo "Escaneie no app autenticador (Google Authenticator, Aegis, 2FAS...):"
  echo
  qrencode -t ANSIUTF8 "$URI"
else
  echo "qrencode não instalado. Adicione manualmente no autenticador (tipo 'baseado em tempo') esta URI/segredo:"
fi
echo
echo "$URI"
echo
echo "Segredo gravado em $ENV_FILE (modo 0600)."
echo "Para valer:  systemctl --user restart claude-telegram"
echo "No Telegram: /destravar <código de 6 dígitos>  (libera por TOTP_UNLOCK_HOURS, padrão 12 h);  /trancar tranca na hora."
echo "Limpe o terminal (clear) depois de escanear."
