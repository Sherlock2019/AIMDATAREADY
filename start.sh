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
#   ./start.sh public          standalone app on a server: background API + password-protected public port 8461
#                              (Caddy container in front; the API itself stays on 127.0.0.1). Restarts after a reboot.
#   ./start.sh public-stop     close the public port (the API keeps running)
#   ./start.sh status | stop | restart | setup | help
#
# Claude agent (optional): put ANTHROPIC_API_KEY=... in .env (never commit it); this script exports .env.
set -euo pipefail
cd "$(dirname "$0")"
if [ -f .env ]; then set -a; source .env; set +a; fi
PORT="${TDR_PORT:-8460}"
URL="http://127.0.0.1:$PORT"
PY=.venv/bin/python
PUBLIC_PORT="${TDR_PUBLIC_PORT:-8461}"
PUBLIC_NAME=tdr-public
CADDY_IMAGE=caddy:2.11.7
CRON_TAG='# sparc-data-readiness'  # same tagged line SPARC's start-aws.sh writes, so there is only ever one
mkdir -p .data

setup() {
  if [ ! -x "$PY" ]; then
    echo "Creating .venv and installing dependencies..."
    python3 -m venv .venv
    .venv/bin/pip install -q --upgrade pip
  fi
  # every runtime import: an interrupted install (Ctrl+C) must be finished here, not crash the server later
  if ! "$PY" -c "import fastapi, uvicorn, yaml, jsonschema, openlineage, chromadb, anthropic" 2>/dev/null; then
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
  # up to 90 s: on a fresh server the first start loads (and downloads) the embedding model before it answers
  for _ in $(seq 1 450); do curl -fs -o /dev/null "$URL/health" && break; sleep 0.2; done
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

env_set() {  # env_set KEY VALUE: replace or append in .env (git-ignored, owner-only)
  touch .env; chmod 600 .env
  if grep -q "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env; else printf '%s=%s\n' "$1" "$2" >> .env; fi
}

public_ip() {  # EC2 instance metadata (IMDSv2), then a public echo service
  local token ip
  token=$(curl -fsS -m 2 -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' 2>/dev/null || true)
  ip=$(curl -fsS -m 2 -H "X-aws-ec2-metadata-token: ${token}" http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || true)
  [ -n "$ip" ] || ip=$(curl -fsS -m 5 https://checkip.amazonaws.com 2>/dev/null | tr -d '\n' || true)
  echo "${ip:-<public-ip>}"
}

on_ec2() {  # no network call: the DMI vendor (Nitro) or Xen hypervisor UUID says EC2
  grep -qs 'Amazon EC2' /sys/devices/virtual/dmi/id/sys_vendor || grep -qsi '^ec2' /sys/hypervisor/uuid
}

reboot_on() {  # start the API again after a reboot (the Caddy container has its own Docker restart policy)
  command -v crontab >/dev/null || { echo "  ! no crontab: after a reboot run ./start.sh --bg"; return 0; }
  local line; line="@reboot cd $(printf %q "$PWD") && bash ./start.sh --bg >> .data/launch.log 2>&1  $CRON_TAG"
  crontab -l 2>/dev/null | grep -qF -- "$line" && return 0
  { crontab -l 2>/dev/null | grep -vF -- "$CRON_TAG" || true; echo "$line"; } | crontab -
}

public_running() { [ "$(docker inspect -f '{{.State.Running}}' "$PUBLIC_NAME" 2>/dev/null)" = true ]; }

public_start() {
  command -v docker >/dev/null || { echo "Docker is needed for the public port (Caddy with a password)." >&2; return 1; }
  running || start_bg
  local user="${TDR_PUBLIC_USER:-admin}" pw="${TDR_PUBLIC_PASSWORD:-}" hash code=""
  if [ -z "$pw" ]; then  # first time: a random password, kept in .env
    pw=$("$PY" -c 'import secrets; print(secrets.token_urlsafe(18))')
    env_set TDR_PUBLIC_USER "$user"; env_set TDR_PUBLIC_PASSWORD "$pw"
  fi
  hash=$(docker run --rm "$CADDY_IMAGE" caddy hash-password --plaintext "$pw") || return 1
  mkdir -p .data/public
  cat > .data/public/Caddyfile <<EOF
{
	admin off
}
:$PUBLIC_PORT {
	basic_auth {
		$user $hash
	}
	reverse_proxy 127.0.0.1:$PORT
}
EOF
  docker rm -f "$PUBLIC_NAME" >/dev/null 2>&1 || true
  docker run -d --name "$PUBLIC_NAME" --restart unless-stopped --network host \
    -v "$PWD/.data/public/Caddyfile:/etc/caddy/Caddyfile:ro" "$CADDY_IMAGE" >/dev/null
  reboot_on
  for _ in $(seq 1 50); do  # 401 without the password = Caddy is up and asking for it
    code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PUBLIC_PORT/health" || true)
    [ "$code" = 401 ] && break; sleep 0.2
  done
  if [ "$code" != 401 ] || ! curl -fs -o /dev/null -u "$user:$pw" "http://127.0.0.1:$PUBLIC_PORT/health"; then
    echo "The public port did not come up; see: docker logs $PUBLIC_NAME" >&2; return 1
  fi
  cat <<EOF

  Data Readiness (standalone, public)
    URL       : http://$(public_ip):$PUBLIC_PORT/dashboard
    Login     : $user / $pw      (in .env: TDR_PUBLIC_USER / TDR_PUBLIC_PASSWORD; change it, then ./start.sh public)
    Security  : open TCP $PUBLIC_PORT in the security group to YOUR IP only. Plain HTTP: the password is not encrypted.
    Reboot    : API via crontab @reboot, public port via Docker (restart unless-stopped)
    Close     : ./start.sh public-stop
EOF
}

public_stop() {
  docker rm -f "$PUBLIC_NAME" >/dev/null 2>&1 && echo "Public port $PUBLIC_PORT closed (the API keeps running on $URL)." \
    || echo "Public port was not open."
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
  if on_ec2; then
    local ip; ip="$(public_ip)"
    if command -v docker >/dev/null && public_running; then
      echo "  Public    : http://$ip:$PUBLIC_PORT/dashboard   (login: TDR_PUBLIC_USER / TDR_PUBLIC_PASSWORD in .env)"
    else
      echo "  Public    : not open. Run ./start.sh public for http://$ip:$PUBLIC_PORT/dashboard (password-protected)"
    fi
  fi
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
  public)       setup; public_start ;;
  public-stop)  public_stop ;;
  status)       if running; then echo "Running on $URL (PID $(port_pid))"; curl -fs "$URL/health"; echo; else echo "Not running."; fi
                if command -v docker >/dev/null && public_running; then echo "Public: http://$(public_ip):$PUBLIC_PORT/dashboard (login in .env)"; fi ;;
  stop)         stop ;;
  restart)      stop; setup; start_bg; banner ;;
  setup)        setup; echo "Setup complete." ;;
  help|-h|--help) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//' ;;
  *)            echo "Unknown command '$1'"; sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
