#!/usr/bin/env bash
# Registra o MCP telegram-bot (escopo user) em cada conta do Claude. Idempotente:
#   - já registrado com o mesmo python/script  -> não faz nada;
#   - registrado com outro caminho             -> remove e registra de novo;
#   - não registrado                           -> registra.
# Escreve nos arquivos de config do Claude via `claude mcp add` — só rodar com OK do dono.
#
# Conta padrão (~/.claude): NÃO se define CLAUDE_CONFIG_DIR. O `claude` sem a variável guarda a
# config de usuário em ~/.claude.json; com CLAUDE_CONFIG_DIR=~/.claude ele usaria ~/.claude/.claude.json
# (outro arquivo) e o registro não valeria para o `claude` puro. As demais contas usam CLAUDE_CONFIG_DIR.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${REPO}/.venv/bin/python"
SCRIPT="${REPO}/mcp_server/telegram_mcp.py"
CLAUDE_BIN="${CLAUDE_BIN:-$HOME/.local/bin/claude}"
NAME="telegram-bot"
DEFAULT_DIR="$HOME/.claude"
DIRS=("$DEFAULT_DIR" "$HOME/.claude-work" "$HOME/.claude-personal" "$HOME/.claude-client")

[ -x "$PYTHON" ] || { echo "python do venv ausente: $PYTHON" >&2; exit 1; }
[ -f "$SCRIPT" ] || { echo "servidor MCP ausente: $SCRIPT" >&2; exit 1; }
[ -x "$CLAUDE_BIN" ] || { echo "claude ausente: $CLAUDE_BIN" >&2; exit 1; }

# claude_in <dir> <args...>: roda o claude "dentro" da conta <dir>
claude_in() {
  local dir="$1"
  shift
  if [ "$dir" = "$DEFAULT_DIR" ]; then
    env -u CLAUDE_CONFIG_DIR "$CLAUDE_BIN" "$@"
  else
    CLAUDE_CONFIG_DIR="$dir" "$CLAUDE_BIN" "$@"
  fi
}

for dir in "${DIRS[@]}"; do
  if [ ! -d "$dir" ]; then
    echo "pulando (sem pasta): $dir"
    continue
  fi
  verb="registrado"
  if out="$(claude_in "$dir" mcp get "$NAME" 2>/dev/null)"; then
    if printf '%s' "$out" | grep -qF -- "$PYTHON" && printf '%s' "$out" | grep -qF -- "$SCRIPT"; then
      echo "já registrado: $dir"
      continue
    fi
    claude_in "$dir" mcp remove --scope user "$NAME"
    verb="atualizado"
  fi
  claude_in "$dir" mcp add --scope user "$NAME" -- "$PYTHON" "$SCRIPT"
  echo "$verb: $dir"
done
