#!/usr/bin/env bash
# Container entrypoint. Firmware URL downloads are handled inside the FastAPI
# app so the browser can show progress and errors.
set -euo pipefail

FIRMWARE_DIR="${FIRMWARE_DIR:-/app/firmware}"
mkdir -p "$FIRMWARE_DIR"

case "${FIRMWARE_URLS:-}" in
  "")
    echo "==> No FIRMWARE_URLS override set - using built-in firmware URLs"
    ;;
  none|off|false|disabled|skip)
    echo "==> Automatic firmware downloads disabled"
    ;;
  *)
    echo "==> Firmware downloads will be tracked in the web UI"
    ;;
esac

if [ -z "${FIRMWARE_URLS:-}" ]; then
    existing=$(find "$FIRMWARE_DIR" -maxdepth 1 -name '*.bin' 2>/dev/null | wc -l)
    if [ "$existing" -gt 0 ]; then
        echo "    ($existing image(s) already in volume)"
    fi
fi

echo "==> Starting: $*"
exec "$@"
