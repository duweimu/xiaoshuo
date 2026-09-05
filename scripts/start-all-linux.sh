#!/usr/bin/env bash
# One-click start: backend + React frontend, both backgrounded and logged
# under .codex-run/. Safe to re-run — each leg stops its own previous
# instance first (see start-backend-linux.sh / start-frontend-linux.sh).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# shellcheck source=lib/dev-lifecycle.sh
source "$SCRIPT_DIR/lib/dev-lifecycle.sh"

RUN_DIR="$REPO_ROOT/.codex-run"
LOG_DIR="$RUN_DIR/logs"
mkdir -p "$LOG_DIR"

BACKEND_PORT="${NOVEL_SYSTEM_BACKEND_PORT:-8000}"
FRONTEND_PORT="${NOVEL_SYSTEM_FRONTEND_PORT:-5174}"
BACKEND_URL="http://127.0.0.1:${BACKEND_PORT}"
FRONTEND_URL="http://127.0.0.1:${FRONTEND_PORT}"

# Report a leg that never became healthy: say whether it exited or just timed
# out, and surface the tail of its log so the cause is visible right here
# instead of one more file away.
leg_failed() {
  local leg="$1" log="$2" rc="$3"
  if [ "$rc" = "2" ]; then
    echo "!! $leg process exited during startup; see $log" >&2
  else
    echo "!! $leg did not become healthy in time; see $log" >&2
  fi
  echo "---- tail of $log ----" >&2
  tail -n 20 "$log" >&2 || true
  exit 1
}

# Each leg also tears its previous instance down, but doing it here first
# guarantees the readiness probes below can only ever hit the *new* process —
# never a still-running old one that the leg has not killed yet.
dev_stop_pidfile "$RUN_DIR/backend.pid"
dev_stop_port "$BACKEND_PORT"

echo "==> starting backend (log: $LOG_DIR/backend.log)"
# setsid makes the leg script the leader of a fresh process group, so
# stop-all-linux.sh (or the next start-all-linux.sh run) can cleanly kill the
# whole subtree — uvicorn --reload's worker, npm's vite child, etc.
setsid "$SCRIPT_DIR/start-backend-linux.sh" >"$LOG_DIR/backend.log" 2>&1 &
BACKEND_LEG_PID=$!
disown

echo "==> waiting for backend readiness (${BACKEND_URL}/ready)"
rc=0
dev_wait_http_ok "${BACKEND_URL}/ready" 90 "$BACKEND_LEG_PID" || rc=$?
[ "$rc" = 0 ] || leg_failed backend "$LOG_DIR/backend.log" "$rc"

dev_stop_pidfile "$RUN_DIR/frontend-react.pid"
dev_stop_port "$FRONTEND_PORT"

echo "==> starting frontend (log: $LOG_DIR/frontend-react.log)"
setsid "$SCRIPT_DIR/start-frontend-linux.sh" >"$LOG_DIR/frontend-react.log" 2>&1 &
FRONTEND_LEG_PID=$!
disown

echo "==> waiting for frontend (${FRONTEND_URL})"
rc=0
dev_wait_http_ok "$FRONTEND_URL" 60 "$FRONTEND_LEG_PID" || rc=$?
[ "$rc" = 0 ] || leg_failed frontend "$LOG_DIR/frontend-react.log" "$rc"

echo "==> backend:  $BACKEND_URL"
echo "==> frontend: $FRONTEND_URL"
echo "==> logs:     $LOG_DIR/"
echo "==> stop with: scripts/stop-all-linux.sh"
