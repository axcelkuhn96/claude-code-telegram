#!/usr/bin/env bash
# Instala/ativa o tunel cloudflared do Mini App (tg-bot). Idempotente.
# Pre-requisito: tunel `tg-bot` criado e ~/.cloudflared/tg-bot.yml existente.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_SRC="$REPO/deploy/cloudflared-tg-bot.service"
UNIT_DIR="$HOME/.config/systemd/user"
ENV_FILE="$HOME/.config/claude-telegram/env"
WEBAPP_URL_VALUE="https://bot.example.com"

[ -f "$HOME/.cloudflared/tg-bot.yml" ] || { echo "falta ~/.cloudflared/tg-bot.yml" >&2; exit 1; }

mkdir -p "$UNIT_DIR"
ln -sf "$UNIT_SRC" "$UNIT_DIR/cloudflared-tg-bot.service"
systemctl --user daemon-reload
systemctl --user enable --now cloudflared-tg-bot.service

mkdir -p "$(dirname "$ENV_FILE")"
touch "$ENV_FILE"
chmod 600 "$ENV_FILE"
if ! grep -q '^WEBAPP_URL=' "$ENV_FILE"; then
  # garante quebra de linha final para nao colar na ultima variavel
  if [ -s "$ENV_FILE" ] && [ -n "$(tail -c1 "$ENV_FILE")" ]; then
    printf '\n' >> "$ENV_FILE"
  fi
  printf 'WEBAPP_URL=%s\n' "$WEBAPP_URL_VALUE" >> "$ENV_FILE"
  echo "WEBAPP_URL adicionado ao env (reinicie o bot para aplicar)"
else
  echo "WEBAPP_URL ja definido no env"
fi

echo "tunel ativo: $(systemctl --user is-active cloudflared-tg-bot.service)"
