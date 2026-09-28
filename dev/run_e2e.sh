#!/usr/bin/env bash
# Builds the image and runs dev/e2e_test.py with fake serial, video and GitHub. Linux only.
# Needs docker, socat, and python3 with playwright (+ Chromium), pyserial, aiohttp.
set -euo pipefail
cd "$(dirname "$0")/.."

IMAGE=${IMAGE:-nanokvm-bridge:e2e}
PORT=${PORT:-47899}
GITHUB_PORT=9998
NAME=nanokvm-bridge-e2e
WORK=$(mktemp -d)

cleanup() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker volume rm "$NAME" >/dev/null 2>&1 || true
    kill $(jobs -p) 2>/dev/null || true
    rm -rf "$WORK"
}
trap cleanup EXIT

[ -n "${SKIP_BUILD:-}" ] || docker build -t "$IMAGE" .

# The "dongle": whatever the container writes to one end comes out the other.
socat pty,raw,echo=0,link="$WORK/pty-host" pty,raw,echo=0,link="$WORK/pty-container" &
for _ in $(seq 20); do [ -e "$WORK/pty-container" ] && break; sleep 0.2; done

# Fake GitHub, serving the image's own frontend build as "the latest release".
cid=$(docker create "$IMAGE")
docker cp "$cid:/www-image" "$WORK/www" >/dev/null
docker rm "$cid" >/dev/null
python3 dev/fake_github.py --port "$GITHUB_PORT" --site-dir "$WORK/www" --latest 9.9.9 &

github="http://host.docker.internal:$GITHUB_PORT"
docker run -d --name "$NAME" --privileged -p "$PORT:80" \
    -v /dev:/dev -v "$NAME:/data" --add-host host.docker.internal:host-gateway \
    -e VIDEO_DEVICE=none \
    -e SERIAL_DEVICE="$(readlink -f "$WORK/pty-container")" \
    -e UPDATE_RELEASES_API_URL="$github/releases/latest" \
    -e UPDATE_DOWNLOAD_URL_TEMPLATE="$github/download/v{version}/nanokvm-usb-browser-v{version}.zip" \
    "$IMAGE" >/dev/null
for _ in $(seq 50); do curl -fs -o /dev/null "http://127.0.0.1:$PORT/api/status" && break; sleep 0.2; done

# Video: the synthetic test pattern, in place of ustreamer (VIDEO_DEVICE=none).
docker exec "$NAME" python3 -c "import PIL" 2>/dev/null || docker exec "$NAME" apk add -q --no-cache py3-pillow
docker exec -d "$NAME" python3 /app/fake_mjpeg.py
sleep 2

python3 dev/e2e_test.py --url "http://127.0.0.1:$PORT" --pty "$WORK/pty-host" --update || {
    echo "--- container log"; docker logs "$NAME" 2>&1 | tail -40; exit 1
}
