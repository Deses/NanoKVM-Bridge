#!/bin/bash
# Seeds the frontend volume on first boot, then execs the bridge.
set -e

# Seed only an empty volume, so in-app updates survive rebuilds.
if [ ! -f /data/www/index.html ]; then
    echo "[entrypoint] seeding /data/www from the image's baked-in build"
    mkdir -p /data/www
    cp -a /www-image/. /data/www/
fi

# exec, so the bridge is PID 1 and gets SIGTERM directly.
exec python3 /app/bridge.py
