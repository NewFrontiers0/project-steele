#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/project-steele}"
APP_USER="${APP_USER:-steele}"
REPO_URL="${REPO_URL:-git@github.com:NewFrontiers0/project-steele.git}"

if [ "$(id -u)" != "0" ]; then
  echo "Run this installer with sudo."
  exit 1
fi

apt-get update
apt-get install -y git python3-venv python3-pip openssh-client libcap2-bin

if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --create-home --shell /bin/bash "$APP_USER"
fi

if [ ! -d "$APP_DIR/.git" ]; then
  mkdir -p "$APP_DIR"
  chown "$APP_USER:$APP_USER" "$APP_DIR"
  runuser -u "$APP_USER" -- git clone "$REPO_URL" "$APP_DIR"
fi

chown -R "$APP_USER:$APP_USER" "$APP_DIR"

if [ ! -f "$APP_DIR/.env" ]; then
  runuser -u "$APP_USER" -- cp "$APP_DIR/.env.example" "$APP_DIR/.env"
fi

if [ ! -d "$APP_DIR/.venv" ]; then
  runuser -u "$APP_USER" -- python3 -m venv "$APP_DIR/.venv"
fi

runuser -u "$APP_USER" -- "$APP_DIR/.venv/bin/python" -m pip install --quiet --upgrade pip
runuser -u "$APP_USER" -- "$APP_DIR/.venv/bin/python" -m pip install --quiet -r "$APP_DIR/requirements.txt"

install -m 0644 "$APP_DIR/deploy/systemd/project-steele.service" /etc/systemd/system/project-steele.service
install -m 0644 "$APP_DIR/deploy/systemd/project-steele-update.service" /etc/systemd/system/project-steele-update.service
install -m 0644 "$APP_DIR/deploy/systemd/project-steele-update.timer" /etc/systemd/system/project-steele-update.timer

systemctl daemon-reload
systemctl enable --now project-steele.service
systemctl enable --now project-steele-update.timer

echo "Project Steele service installed."
echo "App status:    systemctl status project-steele.service"
echo "Update timer:  systemctl list-timers project-steele-update.timer"
echo "App logs:      journalctl -u project-steele.service -f"
