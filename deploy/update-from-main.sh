#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/project-steele}"
APP_USER="${APP_USER:-steele}"
SERVICE_NAME="${SERVICE_NAME:-project-steele.service}"

run_as_app_user() {
  if [ "$(id -un)" = "$APP_USER" ]; then
    "$@"
  else
    runuser -u "$APP_USER" -- "$@"
  fi
}

cd "$APP_DIR"

if ! run_as_app_user git diff --quiet || ! run_as_app_user git diff --cached --quiet; then
  echo "Tracked local changes are present in $APP_DIR; refusing to auto-update."
  echo "Commit, stash, or reset those changes before the timer can deploy main."
  exit 1
fi

old_rev="$(run_as_app_user git rev-parse HEAD)"
run_as_app_user git fetch --prune origin main
new_rev="$(run_as_app_user git rev-parse origin/main)"

if [ "$old_rev" = "$new_rev" ]; then
  echo "Project Steele is already current at $old_rev."
  exit 0
fi

echo "Updating Project Steele from $old_rev to $new_rev."
run_as_app_user git reset --hard origin/main

if [ ! -d ".venv" ]; then
  run_as_app_user python3 -m venv .venv
fi

run_as_app_user .venv/bin/python -m pip install --quiet --upgrade pip
run_as_app_user .venv/bin/python -m pip install --quiet -r requirements.txt

systemctl restart "$SERVICE_NAME"
echo "Project Steele updated and $SERVICE_NAME restarted."
