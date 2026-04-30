#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "==> Creating virtualenv (.venv)"
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "==> Installing dependencies"
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

if [ ! -f ".env" ]; then
  echo "==> No .env found — copying .env.example to .env"
  cp .env.example .env
  echo "==> API keys are entered in the browser when the app opens"
fi

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8001}"
SWIM_FILE_PORT="${SWIM_FILE_PORT:-9000}"
SWIM_HTTP_PROFILE="${SWIM_HTTP_PROFILE:-balanced}"
SWIM_HTTP_CHUNK_BYTES="${SWIM_HTTP_CHUNK_BYTES:-512}"
SWIM_HTTP_CHUNK_DELAY_MS="${SWIM_HTTP_CHUNK_DELAY_MS:-1}"
SWIM_HTTP_ACCELERATE_AFTER_BYTES="${SWIM_HTTP_ACCELERATE_AFTER_BYTES:-65536}"
SWIM_HTTP_ACCELERATED_CHUNK_BYTES="${SWIM_HTTP_ACCELERATED_CHUNK_BYTES:-65536}"
SWIM_HTTP_ACCELERATED_CHUNK_DELAY_MS="${SWIM_HTTP_ACCELERATED_CHUNK_DELAY_MS:-0}"
SWIM_HTTP_INITIAL_DELAY_MS="${SWIM_HTTP_INITIAL_DELAY_MS:-50}"
SWIM_HTTP_TCP_MAXSEG="${SWIM_HTTP_TCP_MAXSEG:-1460}"
SWIM_HTTP_SNDBUF_BYTES="${SWIM_HTTP_SNDBUF_BYTES:-131072}"
SWIM_HTTP_TCP_NOTSENT_LOWAT="${SWIM_HTTP_TCP_NOTSENT_LOWAT:-32768}"
SWIM_FTP_PORT="${SWIM_FTP_PORT:-2121}"
SWIM_FTP_PASSIVE_PORTS="${SWIM_FTP_PASSIVE_PORTS:-30000-30009}"
SWIM_TFTP_PORT="${SWIM_TFTP_PORT:-69}"
KILL_PORT_LISTENER="${KILL_PORT_LISTENER:-1}"
export SWIM_FILE_PORT SWIM_HTTP_PROFILE SWIM_HTTP_CHUNK_BYTES SWIM_HTTP_CHUNK_DELAY_MS
export SWIM_HTTP_ACCELERATE_AFTER_BYTES SWIM_HTTP_ACCELERATED_CHUNK_BYTES SWIM_HTTP_ACCELERATED_CHUNK_DELAY_MS
export SWIM_HTTP_INITIAL_DELAY_MS SWIM_HTTP_TCP_MAXSEG SWIM_HTTP_SNDBUF_BYTES SWIM_HTTP_TCP_NOTSENT_LOWAT
export SWIM_FTP_PORT SWIM_FTP_PASSIVE_PORTS SWIM_TFTP_PORT

tcp_listen_pids() {
  local port="$1"
  {
    if command -v lsof >/dev/null 2>&1; then
      lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true
    fi
    if command -v ss >/dev/null 2>&1; then
      ss -H -ltnp "sport = :$port" 2>/dev/null | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' || true
    fi
    if command -v fuser >/dev/null 2>&1; then
      fuser -n tcp "$port" 2>/dev/null | tr ' ' '\n' || true
    fi
  } | awk 'NF && !seen[$1]++'
}

udp_listen_pids() {
  local port="$1"
  {
    if command -v lsof >/dev/null 2>&1; then
      lsof -tiUDP:"$port" 2>/dev/null || true
    fi
    if command -v ss >/dev/null 2>&1; then
      ss -H -lunp "sport = :$port" 2>/dev/null | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' || true
    fi
    if command -v fuser >/dev/null 2>&1; then
      fuser -n udp "$port" 2>/dev/null | tr ' ' '\n' || true
    fi
  } | awk 'NF && !seen[$1]++'
}

show_tcp_listener() {
  local port="$1"
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null || true
  fi
  if command -v ss >/dev/null 2>&1; then
    ss -H -ltnp "sport = :$port" 2>/dev/null || true
  fi
}

show_udp_listener() {
  local port="$1"
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iUDP:"$port" 2>/dev/null || true
  fi
  if command -v ss >/dev/null 2>&1; then
    ss -H -lunp "sport = :$port" 2>/dev/null || true
  fi
}

can_bind_tcp() {
  local port="$1"
  python -c 'import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("0.0.0.0", int(sys.argv[1])))
s.close()' "$port" >/dev/null 2>&1
}

force_clear_tcp_port() {
  local port="$1"
  local label="$2"

  if can_bind_tcp "$port"; then
    return
  fi

  echo "==> $label port $port is occupied, but the listener PID was not visible."
  echo "==> Existing listener details:"
  show_tcp_listener "$port"

  if [ "$KILL_PORT_LISTENER" != "1" ]; then
    echo "ERROR: automatic port cleanup is disabled."
    echo "==> Stop the process, run with KILL_PORT_LISTENER=1, or use another port."
    exit 1
  fi

  if command -v fuser >/dev/null 2>&1; then
    echo "==> Trying fuser cleanup for TCP/$port."
    fuser -k -n tcp "$port" 2>/dev/null || true
    sleep 0.5
  fi

  if ! can_bind_tcp "$port" && command -v sudo >/dev/null 2>&1 && command -v fuser >/dev/null 2>&1; then
    echo "==> Trying sudo fuser cleanup for TCP/$port."
    sudo fuser -k -n tcp "$port" 2>/dev/null || true
    sleep 0.5
  fi

  if ! can_bind_tcp "$port"; then
    echo "ERROR: port $port is still in use."
    show_tcp_listener "$port"
    echo "==> As a manual fallback, run: sudo fuser -k -n tcp $port"
    echo "==> Or use another port with: PORT=8002 SWIM_FILE_PORT=9001 SWIM_FTP_PORT=2122 ./run.sh"
    exit 1
  fi

  echo "==> $label port $port is clear."
}

clear_listen_port() {
  local port="$1"
  local label="$2"
  local pids

  pids="$(tcp_listen_pids "$port" | tr '\n' ' ')"
  if [ -z "${pids// /}" ]; then
    force_clear_tcp_port "$port" "$label"
    return
  fi

  echo "==> $label port $port is already in use."
  echo "==> Existing listener:"
  show_tcp_listener "$port"
  if [ "$KILL_PORT_LISTENER" != "1" ]; then
    echo "ERROR: automatic port cleanup is disabled."
    echo "==> Stop that process, run with KILL_PORT_LISTENER=1, or use another port."
    exit 1
  fi

  echo "==> Stopping listener PID(s): $pids"
  kill $pids 2>/dev/null || true

  for _ in 1 2 3 4 5 6 7 8 9 10; do
    pids="$(tcp_listen_pids "$port" | tr '\n' ' ')"
    if [ -z "${pids// /}" ]; then
      break
    fi
    sleep 0.5
  done

  pids="$(tcp_listen_pids "$port" | tr '\n' ' ')"
  if [ -n "${pids// /}" ]; then
    echo "==> Listener did not stop cleanly; forcing it down."
    kill -9 $pids 2>/dev/null || true
    sleep 0.5
  fi

  pids="$(tcp_listen_pids "$port" | tr '\n' ' ')"
  if [ -n "${pids// /}" ]; then
    echo "ERROR: port $port is still in use."
    show_tcp_listener "$port"
    echo "==> Run on another port with: PORT=8002 SWIM_FILE_PORT=9001 SWIM_FTP_PORT=2122 ./run.sh"
    exit 1
  fi
  force_clear_tcp_port "$port" "$label"
}

clear_udp_port() {
  local port="$1"
  local label="$2"
  local pids

  pids="$(udp_listen_pids "$port" | tr '\n' ' ')"
  if [ -z "${pids// /}" ]; then
    return
  fi

  echo "==> $label UDP port $port is already in use."
  echo "==> Existing listener:"
  show_udp_listener "$port"
  if [ "$KILL_PORT_LISTENER" != "1" ]; then
    echo "ERROR: automatic port cleanup is disabled."
    echo "==> Stop that process, run with KILL_PORT_LISTENER=1, or use another port."
    exit 1
  fi

  echo "==> Stopping UDP listener PID(s): $pids"
  kill $pids 2>/dev/null || true
  sleep 0.5
  pids="$(udp_listen_pids "$port" | tr '\n' ' ')"
  if [ -n "${pids// /}" ]; then
    kill -9 $pids 2>/dev/null || true
    sleep 0.5
  fi
}

ensure_low_port_permission() {
  local port="$1"

  if [ "$port" -ge 1024 ] || [ "$(id -u)" = "0" ]; then
    return
  fi

  if [ "$(uname -s)" != "Linux" ]; then
    echo "ERROR: UDP/$port is privileged on this OS."
    echo "==> Run with sudo, use Docker, or set SWIM_TFTP_PORT to a non-standard port if your switch supports it."
    exit 1
  fi

  local python_bin
  python_bin="$(python -c 'import os,sys; print(os.path.realpath(sys.executable))')"

  if command -v getcap >/dev/null 2>&1; then
    if getcap "$python_bin" 2>/dev/null | grep -q "cap_net_bind_service"; then
      echo "==> Python can bind privileged ports for TFTP."
      return
    fi
  fi

  if ! command -v setcap >/dev/null 2>&1; then
    if command -v sudo >/dev/null 2>&1 && command -v apt-get >/dev/null 2>&1; then
      echo "==> Installing libcap2-bin so Python can bind UDP/$port for TFTP."
      sudo apt-get update
      sudo apt-get install -y libcap2-bin
    else
      echo "ERROR: setcap is required so ./run.sh can bind UDP/$port for TFTP."
      echo "==> Install libcap2-bin, run with sudo, or use Docker."
      exit 1
    fi
  fi

  if ! command -v sudo >/dev/null 2>&1; then
    echo "ERROR: sudo is required to grant Python permission to bind UDP/$port."
    echo "==> Run with sudo or use Docker."
    exit 1
  fi

  echo "==> Granting Python permission to bind UDP/$port for TFTP."
  echo "==> sudo may prompt for your password once."
  sudo setcap 'cap_net_bind_service=+ep' "$python_bin"

  if command -v getcap >/dev/null 2>&1 && getcap "$python_bin" | grep -q "cap_net_bind_service"; then
    echo "==> Low-port bind permission applied to $python_bin."
  else
    echo "ERROR: could not confirm low-port bind permission on $python_bin."
    echo "==> Use Docker or run ./run.sh with sudo."
    exit 1
  fi
}

clear_listen_port "$PORT" "App"
clear_listen_port "$SWIM_FILE_PORT" "SWIM firmware file server"
clear_listen_port "$SWIM_FTP_PORT" "SWIM FTP server"
clear_udp_port "$SWIM_TFTP_PORT" "SWIM TFTP server"
ensure_low_port_permission "$SWIM_TFTP_PORT"

echo "==> Firmware downloads use the web UI source prompt unless FIRMWARE_URLS is set"
echo "==> Starting server on http://${HOST}:${PORT}"
echo "==> SWIM firmware file server will listen on http://${HOST}:${SWIM_FILE_PORT}"
echo "==> SWIM HTTP stream profile: ${SWIM_HTTP_PROFILE} (transfer logs show exact chunk/delay values)"
echo "==> SWIM FTP server will listen on ftp://${HOST}:${SWIM_FTP_PORT} with passive ports ${SWIM_FTP_PASSIVE_PORTS}"
echo "==> SWIM TFTP server will listen on UDP/${SWIM_TFTP_PORT}"
echo "==> Cisco IOS-XE TFTP expects standard UDP/69; non-standard TFTP ports are usually rejected"
echo "==> Open it from this machine at http://127.0.0.1:${PORT} or from the LAN at http://<this-machine-ip>:${PORT}"
exec uvicorn main:app --reload --host "$HOST" --port "$PORT"
