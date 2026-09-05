#!/usr/bin/env bash
# Linux dev equivalent of start-dev.cmd's React-frontend leg.
# Safe to re-run: stops any previous instance (by pidfile, falling back to
# whatever is bound to the port) before starting a fresh one.
#
# Node resolution — first hit wins:
#   1. $NOVEL_SYSTEM_NODE_BIN   directory holding node/npm (explicit override)
#   2. nvm ($NVM_DIR, ~/.nvm)   sourced; `nvm use 16` only if the default alias
#                               puts no node on PATH (the old CentOS 7 / glibc
#                               2.17 host tops out at Node 16)
#   3. ~/.local/node/bin        a plain tarball install (the Ubuntu dev host)
#   4. whatever `node` is already on PATH
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=lib/dev-lifecycle.sh
source "$SCRIPT_DIR/lib/dev-lifecycle.sh"

RUN_DIR="$REPO_ROOT/.codex-run"
mkdir -p "$RUN_DIR"
PID_FILE="$RUN_DIR/frontend-react.pid"
URL_FILE="$RUN_DIR/frontend-react.url"
PORT="${NOVEL_SYSTEM_FRONTEND_PORT:-5174}"

dev_stop_pidfile "$PID_FILE"
dev_stop_port "$PORT"

cd "$REPO_ROOT/frontend-react"

if [ -n "${NOVEL_SYSTEM_NODE_BIN:-}" ]; then
  export PATH="$NOVEL_SYSTEM_NODE_BIN:$PATH"
elif [ -s "${NVM_DIR:-$HOME/.nvm}/nvm.sh" ]; then
  export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
  # shellcheck disable=SC1091
  . "$NVM_DIR/nvm.sh"
  command -v node >/dev/null 2>&1 || nvm use 16 >/dev/null 2>&1 || true
elif [ -x "$HOME/.local/node/bin/node" ]; then
  export PATH="$HOME/.local/node/bin:$PATH"
fi

if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
  cat >&2 <<'MSG'
!! node/npm not found. Provide Node >= 16 via one of:
   - nvm at ~/.nvm (with a default alias, or Node 16 installed), or
   - a tarball unpacked at ~/.local/node, or
   - NOVEL_SYSTEM_NODE_BIN=/path/to/node/bin
MSG
  exit 1
fi

if [ ! -d node_modules ]; then
  echo "!! frontend-react/node_modules is missing. Run: cd frontend-react && npm ci" >&2
  exit 1
fi

echo "==> node $(node --version) at $(command -v node)"

# Node 16 lacks the global WebCrypto Vite 6 needs (plus two ES2023 array
# methods); the polyfill only patches what is missing, so it is a no-op on
# newer runtimes and is preloaded unconditionally.
export NODE_OPTIONS="--require ./crypto-polyfill.cjs"

echo "http://127.0.0.1:${PORT}" > "$URL_FILE"
echo $$ > "$PID_FILE"
exec npm run dev -- --host 127.0.0.1 --port "$PORT"
