#!/usr/bin/env bash
# Instala o claude-telegram como serviço systemd --user. Idempotente: pode rodar N vezes.
# Nunca usa sudo, nunca liga linger (só avisa), nunca imprime o conteúdo do env.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_NAME="claude-telegram.service"
UNIT_SRC="${REPO}/deploy/${UNIT_NAME}"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
ENV_FILE="$HOME/.config/claude-telegram/env"
VENV="${REPO}/.venv"

die() { echo "erro: $*" >&2; exit 1; }

[ "$(id -u)" -ne 0 ] || die "não rode como root (é um serviço systemd --user)"
command -v systemctl >/dev/null || die "systemctl não encontrado"
[ -f "$UNIT_SRC" ] || die "unit ausente: $UNIT_SRC"
[ -f "${REPO}/requirements.txt" ] || die "requirements.txt ausente"

# 1) venv + dependências (pip é no-op quando já está tudo instalado)
if [ ! -x "${VENV}/bin/python" ]; then
  echo "criando venv: ${VENV}"
  python3 -m venv "$VENV"
fi
"${VENV}/bin/python" -m pip install -q -r "${REPO}/requirements.txt"
echo "venv ok"

# 2) segredos: existe, 0600, e tem as 3 variáveis (só os NOMES são checados; valores nunca saem)
[ -f "$ENV_FILE" ] || die "env ausente: $ENV_FILE (TELEGRAM_BOT_TOKEN, OPENAI_API_KEY, ALLOWED_PHONE)"
perm="$(stat -c '%a' "$ENV_FILE")"
[ "$perm" = "600" ] || die "$ENV_FILE tem permissão $perm; rode: chmod 600 $ENV_FILE"
for var in TELEGRAM_BOT_TOKEN OPENAI_API_KEY ALLOWED_PHONE; do
  grep -Eq "^(export[[:space:]]+)?${var}=." "$ENV_FILE" || die "variável ausente no env: $var"
done
echo "env ok (0600)"

# 2b) deploy/mcp.json: config MCP das sessoes lancadas pelo bot (caminhos absolutos desta maquina)
printf '{"mcpServers":{"telegram-bot":{"command":"%s/.venv/bin/python","args":["%s/mcp_server/telegram_mcp.py"]}}}\n' "$REPO" "$REPO" > "${REPO}/deploy/mcp.json"
echo "deploy/mcp.json gerado"

# 3) unit: symlink pro arquivo versionado (editar o repo vale após daemon-reload)
mkdir -p "$UNIT_DIR"
ln -sfn "$UNIT_SRC" "${UNIT_DIR}/${UNIT_NAME}"
was_active=no
systemctl --user is-active --quiet "$UNIT_NAME" && was_active=yes
systemctl --user daemon-reload
systemctl --user enable --now "$UNIT_NAME"
# já rodava: reinício explícito pega mudança de código/unit (derruba claude filhos em execução)
[ "$was_active" = "yes" ] && systemctl --user restart "$UNIT_NAME"

# 4) confere que subiu
sleep 3
state="$(systemctl --user is-active "$UNIT_NAME" || true)"
systemctl --user status "$UNIT_NAME" --no-pager -n 10 || true
[ "$state" = "active" ] || die "serviço não está active (estado: $state); veja: journalctl --user -u claude-telegram -n 50"

# 5) dicas (não aplicadas aqui)
linger="$(loginctl show-user "$USER" -p Linger --value 2>/dev/null || echo desconhecido)"
echo "ativo. Linger=${linger}"
if [ "$linger" != "yes" ]; then
  echo "dica: sem linger o bot para ao encerrar a sessão gráfica. Para manter após logout: loginctl enable-linger \"$USER\" (decisão sua; este script não aplica)."
fi
