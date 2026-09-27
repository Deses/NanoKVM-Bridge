#!/bin/bash
# Renders the nginx config, then runs the bridge and nginx; either exiting stops the container.
set -e

# Seed only an empty volume, so in-app updates survive rebuilds.
if [ ! -f /data/www/index.html ]; then
    echo "[entrypoint] seeding /data/www from the image's baked-in build"
    mkdir -p /data/www
    cp -a /www-image/. /data/www/
fi

cp /etc/nginx/nginx.conf.template /etc/nginx/nginx.conf

if [ -n "${AUTH_USER:-}" ] && [ -n "${AUTH_PASSWORD:-}" ]; then
    echo "[entrypoint] HTTP basic auth enabled for user '${AUTH_USER}'"
    htpasswd -Bbc /etc/nginx/.htpasswd "$AUTH_USER" "$AUTH_PASSWORD" >/dev/null
    cat > /etc/nginx/auth.conf <<EOF
auth_basic "NanoKVM-Pi";
auth_basic_user_file /etc/nginx/.htpasswd;
EOF
else
    echo "[entrypoint] AUTH_USER/AUTH_PASSWORD not set - no auth, LAN/VPN use only"
    : > /etc/nginx/auth.conf
fi

nginx -t

python3 /app/bridge.py &
BRIDGE_PID=$!

nginx -g "daemon off;" &
NGINX_PID=$!

terminate() {
    kill "$BRIDGE_PID" "$NGINX_PID" 2>/dev/null || true
}
trap terminate TERM INT

wait -n "$BRIDGE_PID" "$NGINX_PID"
EXIT_CODE=$?
terminate
exit "$EXIT_CODE"
