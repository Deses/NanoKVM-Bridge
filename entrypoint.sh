#!/bin/bash
# Installs the frontend into the /data volume if needed, then runs the bridge.
set -e

www=/data/www
baked=/www-image
mkdir -p /data

# Clean up after an in-app update that was interrupted mid-swap.
rm -rf /data/.update-*
if [ ! -f "$www/index.html" ] && [ -f "$www.old/index.html" ]; then
    rm -rf "$www"
    mv "$www.old" "$www"
fi
rm -rf "$www.old"

# Install the image's build on first boot, or when the image ships a newer
# version than the volume has. A newer version installed from the app is kept.
baked_version=$(cat "$baked/.nanokvm-usb-version")
installed_version=$(cat "$www/.nanokvm-usb-version" 2>/dev/null || true)
newest=$(printf '%s\n%s\n' "$installed_version" "$baked_version" | sort -V | tail -n 1)
if [ ! -f "$www/index.html" ] || [ "$newest" != "$installed_version" ]; then
    echo "[entrypoint] installing NanoKVM-USB frontend v$baked_version"
    rm -rf "$www.new"
    cp -a "$baked" "$www.new"
    rm -rf "$www"
    mv "$www.new" "$www"
fi

exec python3 /app/bridge.py
