#!/usr/bin/env bash
# Share the app through a Cloudflare quick tunnel with one command.
#   ./scripts/share.sh          respect backend/.env
#   ./scripts/share.sh --demo   BOLNA_MODE=fake for this session only (.env is not touched)
# Stop with Ctrl+C: the tunnel, the servers this script started and caffeinate are all stopped.
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HELPERS="$ROOT/scripts/share_helpers.py"
SHARE_DIR="$ROOT/.share"
BACKEND_PORT=8000      # the Vite proxy in frontend/vite.config.ts points here
PREVIEW_PORT=4173      # `vite preview` (see frontend/vite.config.ts)
BACKEND_URL="http://127.0.0.1:$BACKEND_PORT"
PREVIEW_URL="http://127.0.0.1:$PREVIEW_PORT"
PY="$ROOT/backend/.venv/bin/python"
ENV_FILE="$ROOT/backend/.env"

DEMO=0
for arg in "$@"; do
  case "$arg" in
    --demo) DEMO=1 ;;
    -h|--help) sed -n '2,5p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "Unknown option: $arg  (use --demo, or no option)" >&2; exit 2 ;;
  esac
done

die() { echo "" >&2; echo "ERROR: $1" >&2; [ -n "${2:-}" ] && echo "  Fix: $2" >&2; exit 1; }

# ---- 1. pre-checks -----------------------------------------------------------------------------
command -v cloudflared >/dev/null 2>&1 || die "cloudflared is not installed." "brew install cloudflared"
command -v node        >/dev/null 2>&1 || die "Node.js is not installed." "brew install node"
command -v npm         >/dev/null 2>&1 || die "npm is not installed." "brew install node"
command -v python3     >/dev/null 2>&1 || die "python3 is not installed." "brew install python"
command -v curl        >/dev/null 2>&1 || die "curl is not installed."
[ -x "$PY" ] || die "The backend virtual environment is missing ($PY)." \
  "cd backend && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
[ -d "$ROOT/frontend/node_modules" ] || die "Front end packages are not installed." "cd frontend && npm install"

mkdir -p "$SHARE_DIR"
PIDS=()          # process groups this script started (and must stop)
STARTED_BACKEND=0

# ---- 8. cleanup, on Ctrl+C or any exit ------------------------------------------------------------
cleanup() {
  trap - EXIT INT TERM
  echo ""
  local pid
  for pid in "${PIDS[@]:-}"; do
    [ -n "$pid" ] && kill -TERM -- "-$pid" 2>/dev/null
  done
  sleep 1
  for pid in "${PIDS[@]:-}"; do
    [ -n "$pid" ] && kill -KILL -- "-$pid" 2>/dev/null
  done
  echo "Sharing stopped. The old link no longer works."
}
trap cleanup EXIT
trap 'exit 130' INT TERM

wait_for() {  # wait_for <url> <seconds>
  local url="$1" tries=$(( $2 * 2 ))
  while [ "$tries" -gt 0 ]; do
    curl -s -o /dev/null -m 2 "$url" && return 0
    sleep 0.5; tries=$((tries - 1))
  done
  return 1
}

start_group() {  # start_group <logfile> <command...>: run in its own process group, remember it
  local log="$1"; shift
  set -m
  "$@" >"$log" 2>&1 &
  PIDS+=("$!")
  set +m
}

# ---- 2. backend -----------------------------------------------------------------------------------
if curl -s -o /dev/null -m 2 "$BACKEND_URL/api/auth/status"; then
  RUNNING_MODE="$("$PY" "$HELPERS" mode "$BACKEND_URL")"
  if [ "$DEMO" = 1 ] && [ "$RUNNING_MODE" = "real" ]; then
    die "A backend is already running on port $BACKEND_PORT in LIVE mode, so --demo cannot make it fake." \
        "Stop that backend first, or run ./scripts/share.sh without --demo."
  fi
  echo "Backend: already running on port $BACKEND_PORT, reusing it."
else
  echo "Backend: starting on 127.0.0.1:$BACKEND_PORT ..."
  if [ "$DEMO" = 1 ]; then
    start_group "$SHARE_DIR/backend.log" bash -c "cd '$ROOT/backend' && BOLNA_MODE=fake exec '$PY' run.py"
  else
    start_group "$SHARE_DIR/backend.log" bash -c "cd '$ROOT/backend' && exec '$PY' run.py"
  fi
  STARTED_BACKEND=1
fi

# ---- 3. front end: production build + preview -----------------------------------------------------
if curl -s -o /dev/null -m 2 "$PREVIEW_URL"; then
  die "Something is already listening on port $PREVIEW_PORT." "Stop it (lsof -nP -iTCP:$PREVIEW_PORT -sTCP:LISTEN) and run again."
fi
echo "Front end: building ..."
( cd "$ROOT/frontend" && npm run build >"$SHARE_DIR/build.log" 2>&1 ) \
  || die "The front end build failed. See $SHARE_DIR/build.log"
echo "Front end: starting preview on 127.0.0.1:$PREVIEW_PORT ..."
start_group "$SHARE_DIR/preview.log" bash -c "cd '$ROOT/frontend' && exec npx vite preview"

# ---- 4. wait until both answer, then open the tunnel ----------------------------------------------
wait_for "$BACKEND_URL/api/auth/status" 40 || die "The backend did not answer in time. See $SHARE_DIR/backend.log"
wait_for "$PREVIEW_URL/api/auth/status" 40 || die "The preview server did not answer in time. See $SHARE_DIR/preview.log"

: >"$SHARE_DIR/cloudflared.log"
start_group "$SHARE_DIR/cloudflared.log" cloudflared tunnel --no-autoupdate --url "http://localhost:$PREVIEW_PORT"

# ---- 5. find the public link --------------------------------------------------------------------------
LINK=""
for _ in $(seq 1 120); do
  LINK="$("$PY" "$HELPERS" url <"$SHARE_DIR/cloudflared.log" 2>/dev/null || true)"
  [ -n "$LINK" ] && break
  sleep 0.5
done
[ -n "$LINK" ] || die "cloudflared did not print a trycloudflare.com link within 60 seconds. See $SHARE_DIR/cloudflared.log" \
  "Check your internet connection and try again."

# ---- 6. keep the Mac awake ---------------------------------------------------------------------------------
if command -v caffeinate >/dev/null 2>&1; then
  start_group /dev/null caffeinate -dims
fi

# ---- 7. banner, then the link ------------------------------------------------------------------------------
# The banner uses what the running backend itself reports (so it is true even for a reused backend).
eval "$("$PY" "$HELPERS" flags "$BACKEND_URL" "$ENV_FILE" | sed 's/^/FLAG_/')"
echo ""
"$PY" "$HELPERS" banner "$FLAG_password_set" "$FLAG_mode" "$FLAG_delete"
echo ""
echo "    SHARE THIS LINK:"
echo ""
echo "        $LINK"
echo ""
echo "  Record of changes made through the link: $SHARE_DIR/actions.log"
echo "  (Keep this laptop awake and online. Press Ctrl+C to stop.)"
echo ""

# stay here until Ctrl+C
while true; do sleep 3600 & wait $!; done
