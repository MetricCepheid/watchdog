#!/bin/sh
set -eu

if [ ! -f /config/config.json ]; then
  echo "[watchdog] ERROR: /config/config.json not found (bind-mount it)."
  exit 1
fi

cp /config/config.json /app/config.json
cd /app
exec python watchdog.py
