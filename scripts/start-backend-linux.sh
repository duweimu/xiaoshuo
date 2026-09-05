#!/usr/bin/env bash
# Linux dev equivalent of start-dev.cmd's backend leg (this box has no PowerShell).
# Safe to re-run: stops any previous instance (by pidfile, falling back to
# whatever is bound to the port) before starting a fresh one.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=lib/dev-lifecycle.sh
source "$SCRIPT_DIR/lib/dev-lifecycle.sh"

RUN_DIR="$REPO_ROOT/.codex-run"
mkdir -p "$RUN_DIR"
PID_FILE="$RUN_DIR/backend.pid"
URL_FILE="$RUN_DIR/backend.url"
PORT="${NOVEL_SYSTEM_BACKEND_PORT:-8000}"

dev_stop_pidfile "$PID_FILE"
dev_stop_port "$PORT"

cd "$REPO_ROOT/backend"

export NOVEL_SYSTEM_VECTOR_BACKEND=memory
PYTHON="$REPO_ROOT/backend/.venv/bin/python"
CONFIG_SECRET_FILE="$RUN_DIR/config.secret"
ARTIFACT_RETENTION_DAYS="${NOVEL_SYSTEM_ARTIFACT_RETENTION_DAYS:-14}"

"$PYTHON" "$SCRIPT_DIR/cleanup_runtime_artifacts.py" \
  --run-dir "$RUN_DIR" \
  --retention-days "$ARTIFACT_RETENTION_DAYS" \
  --apply

# Match the Windows launcher: create one per-workspace random encryption key and
# reuse it across restarts. A public fallback key makes provider API keys in the
# SQLite database decryptable by anyone who obtains the file.
if [ -z "${NOVEL_SYSTEM_CONFIG_SECRET:-}" ]; then
  if [ -s "$CONFIG_SECRET_FILE" ]; then
    NOVEL_SYSTEM_CONFIG_SECRET="$(<"$CONFIG_SECRET_FILE")"
  else
    umask 077
    NOVEL_SYSTEM_CONFIG_SECRET="$($PYTHON -c 'import secrets; print(secrets.token_urlsafe(48))')"
    printf '%s' "$NOVEL_SYSTEM_CONFIG_SECRET" > "$CONFIG_SECRET_FILE"
  fi
fi
export NOVEL_SYSTEM_CONFIG_SECRET
chmod 600 "$CONFIG_SECRET_FILE" 2>/dev/null || true

$PYTHON -m alembic upgrade head

echo "http://127.0.0.1:${PORT}" > "$URL_FILE"
echo $$ > "$PID_FILE"
exec "$PYTHON" -m uvicorn novel_system.api.app:create_app --factory --reload \
  --host 127.0.0.1 --port "$PORT" --app-dir src
