#!/usr/bin/env bash
# Reinício seguro do bot: pede ao próprio bot (POST /restart-when-idle) para parar de aceitar
# execuções novas, esperar as em andamento terminarem (até 30 min) e sair; o systemd
# (Restart=always) sobe o bot de novo. NUNCA use `systemctl restart` com execução ativa: o
# `claude` filho morre com SIGTERM.
#
# Uso: scripts/safe-restart.sh [--no-wait]
#   (sem flag) espera o bot voltar (MainPID novo + active), até ~32 min.
# O segredo local nunca é impresso nem vai para a linha de comando (arquivo temporário 0600).
set -euo pipefail

STATE_DIR="${CLAUDE_TELEGRAM_STATE_DIR:-$HOME/.config/claude-telegram}"
SECRET_FILE="$STATE_DIR/local.secret"
PORT_FILE="$STATE_DIR/local.port"
UNIT="${CLAUDE_TELEGRAM_UNIT:-claude-telegram}"
WAIT=1
[ "${1:-}" = "--no-wait" ] && WAIT=0

DOWN_MSG="O bot não está rodando (ou não respondeu): nada foi reiniciado.
Para subir: systemctl --user start $UNIT   (confira: systemctl --user status $UNIT)"

PORT=28765
if [ -r "$PORT_FILE" ]; then
  P="$(tr -d '[:space:]' < "$PORT_FILE")"
  case "$P" in ''|*[!0-9]*) ;; *) PORT="$P" ;; esac
fi

if [ ! -r "$SECRET_FILE" ]; then
  echo "$DOWN_MSG" >&2
  exit 2
fi

HDR="$(mktemp)"
trap 'rm -f "$HDR"' EXIT
chmod 600 "$HDR"
printf 'X-Bot-Secret: %s\n' "$(cat "$SECRET_FILE")" > "$HDR"

PID_BEFORE=""
if [ "$WAIT" = 1 ]; then
  PID_BEFORE="$(systemctl --user show -p MainPID --value "$UNIT" 2>/dev/null || true)"
fi

if ! RESP="$(curl -sS -m 10 -X POST -H @"$HDR" -H 'Content-Type: application/json' -d '{}' \
    -w '\n%{http_code}' "http://127.0.0.1:$PORT/restart-when-idle" 2>/dev/null)"; then
  echo "$DOWN_MSG" >&2
  exit 2
fi
CODE="${RESP##*$'\n'}"
BODY="${RESP%$'\n'*}"

case "$CODE" in
  200) ;;
  401) echo "O bot recusou o segredo local (arquivo desatualizado?). Nada foi reiniciado." >&2; exit 2 ;;
  404) echo "Este bot ainda não tem o reinício seguro (versão antiga). Nada foi reiniciado." >&2; exit 2 ;;
  *)   echo "Resposta inesperada do bot (HTTP $CODE). Nada foi reiniciado." >&2; exit 2 ;;
esac

RUNNING="$(printf '%s' "$BODY" | grep -o '"running": *[0-9]*' | grep -o '[0-9]*$' || echo '?')"
echo "Reinício seguro pedido. Execuções em andamento: $RUNNING."
echo "O bot responde \"reiniciando em instantes\" a mensagens novas até terminar e voltar."

[ "$WAIT" = 1 ] || exit 0

for _ in $(seq 1 960); do   # 960 × 2 s ≈ 32 min (30 min de espera + folga do boot)
  sleep 2
  PID_NOW="$(systemctl --user show -p MainPID --value "$UNIT" 2>/dev/null || true)"
  ACTIVE="$(systemctl --user show -p ActiveState --value "$UNIT" 2>/dev/null || true)"
  if [ -n "$PID_NOW" ] && [ "$PID_NOW" != "0" ] && [ "$PID_NOW" != "$PID_BEFORE" ] && [ "$ACTIVE" = "active" ]; then
    echo "Reiniciado: pid $PID_BEFORE → $PID_NOW ($ACTIVE)."
    exit 0
  fi
done
echo "O bot não voltou no prazo. Confira: systemctl --user status $UNIT" >&2
exit 3
