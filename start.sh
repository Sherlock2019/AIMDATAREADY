#!/usr/bin/env bash
# taskDataReadiness launcher. Local demonstration only: the API binds 127.0.0.1.
#
#   ./start.sh                 set up if needed, then run the API in the foreground (Ctrl+C to stop)
#   ./start.sh --bg | bg       run the API in the background (log .data/server.log, PID .data/server.pid)
#   ./start.sh dashboard       background API + print the dashboard URL
#   ./start.sh demo [NN]       faulty-dataset demo in the terminal: all 11 cases, or one (e.g. 07)
#   ./start.sh scenarios       the 36 labelled readiness scenarios
#   ./start.sh test            pytest + ruff
#   ./start.sh flowise         API + dedicated Flowise (127.0.0.1:3460) with the Data-Readiness Agentflow
#   ./start.sh flowise-stop    stop that Flowise container (its data volume is kept)
#   ./start.sh status | stop | restart | setup | help
#
# Claude agent (optional): put ANTHROPIC_API_KEY=... in .env (never commit it); this script exports .env.
set -euo pipefail
cd "$(dirname "$0")"
if [ -f .env ]; then set -a; source .env; set +a; fi
PORT="${TDR_PORT:-8460}"
URL="http://127.0.0.1:$PORT"
PY=.venv/bin/python
mkdir -p .data

setup() {
  if [ ! -x "$PY" ]; then
    echo "Creating .venv and installing dependencies..."
    python3 -m venv .venv
    .venv/bin/pip install -q --upgrade pip
  fi
  if ! "$PY" -c "import fastapi, yaml, anthropic" 2>/dev/null; then
    .venv/bin/pip install -q -e '.[dev,claude]'
  fi
  [ -f .env ] || cp .env.example .env
  [ -d datasets/cases ] || "$PY" datasets/build_datasets.py
}

port_pid() { ss -ltnpH "sport = :$PORT" 2>/dev/null | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2; }

running() { [ -n "$(port_pid)" ]; }

start_bg() {
  if running; then echo "Port $PORT is already in use (PID $(port_pid)); see ./start.sh status"; return 0; fi
  setsid nohup .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$PORT" > .data/server.log 2>&1 < /dev/null &
  echo $! > .data/server.pid
  for _ in $(seq 1 50); do curl -fs -o /dev/null "$URL/health" && break; sleep 0.2; done
  if curl -fs -o /dev/null "$URL/health"; then
    echo "taskDataReadiness running on $URL (PID $(cat .data/server.pid))"
  else
    echo "Server did not start; see .data/server.log" >&2; return 1
  fi
}

stop() {
  # Stop by port (and the PID we recorded), never by process name.
  local pid; pid="$(port_pid)"
  if [ -z "$pid" ]; then echo "Nothing listening on port $PORT."; rm -f .data/server.pid; return 0; fi
  if [ -f .data/server.pid ] && [ "$(cat .data/server.pid)" != "$pid" ]; then
    echo "Port $PORT is held by PID $pid, which this launcher did not start. Not stopping it." >&2; return 1
  fi
  kill "$pid" && echo "Stopped PID $pid (port $PORT)."
  rm -f .data/server.pid
}

banner() {
  cat <<EOF

  App (table UI) : $URL/dashboard   (current metric | missing checkpoints | recommended fix)
  Old lab        : $URL/lab
  API docs  : $URL/docs
  Health    : $URL/health
  Claude    : $( [ -n "${ANTHROPIC_API_KEY:-}" ] && echo "API key found (Ask Claude enabled)" || echo "not configured (set ANTHROPIC_API_KEY in .env)")
  Stop      : ./start.sh stop
EOF
}

case "${1:-}" in
  ""|fg)        setup
                if running; then
                  echo "taskDataReadiness is already running on $URL (PID $(port_pid))."
                  echo "Use it as is, or run ./start.sh restart (background) / ./start.sh stop then ./start.sh."
                  banner; exit 0
                fi
                banner; exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$PORT" ;;
  --bg|bg)      setup; start_bg; banner ;;
  dashboard)    setup; start_bg; banner ;;
  demo)         setup; "$PY" scripts/run_dataset_demo.py ${2:+--case "$2"} ;;
  scenarios)    setup; "$PY" scripts/run_demo.py ;;
  test)         setup; "$PY" -m pytest && .venv/bin/ruff check . && .venv/bin/ruff format --check . ;;
  flowise)      setup; running || start_bg; "$PY" flowise/provision.py ;;
  flowise-stop) docker compose --env-file .env -f flowise/docker-compose.yml stop ;;
  status)       if running; then echo "Running on $URL (PID $(port_pid))"; curl -fs "$URL/health"; echo; else echo "Not running."; fi ;;
  stop)         stop ;;
  restart)      stop; setup; start_bg; banner ;;
  setup)        setup; echo "Setup complete." ;;
  help|-h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//' ;;
  *)            echo "Unknown command '$1'"; sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
