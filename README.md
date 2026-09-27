# NanoKVM-Pi

Use a [Sipeed NanoKVM-USB](https://github.com/sipeed/NanoKVM-USB) over the
network: plug it into a Raspberry Pi next to the machine you want to control,
run one container, and open the official NanoKVM-USB web UI from any browser.

```
[target machine] --HDMI + USB--> [NanoKVM-USB] --USB--> [Raspberry Pi] --LAN--> [your browser]
```

## How it works

The NanoKVM-USB appears on Linux as a UVC capture device (MJPEG video) and a
USB serial port speaking a CH9329-style keyboard/mouse protocol at 57600 baud.
Sipeed's [browser app](https://github.com/sipeed/NanoKVM-USB/tree/main/browser)
reaches both through Chrome's Web Serial and `getUserMedia` APIs, which only
work when the dongle is plugged into the machine running the browser.

This container serves Sipeed's official browser build, unmodified, plus a
small JavaScript shim that swaps those APIs for network-backed versions:

- **Video**: [ustreamer](https://github.com/pikvm/ustreamer) forwards the
  dongle's MJPEG frames as-is; the shim turns them into a `MediaStream`.
- **Keyboard and mouse**: the app's HID packets go over a WebSocket to the
  dongle's serial port.
- **Audio** (optional): raw PCM from the dongle's USB audio input goes over a
  second WebSocket.

A single Python process (`server/bridge.py`, aiohttp) serves all of it on one
port.

## Setup

1. Connect the NanoKVM-USB's **HOST** port to a USB port on the Pi (3.0 if it
   has one), and its HDMI and target USB ports to the machine you want to
   control.
2. Check the Pi sees it:
   ```bash
   ls -l /dev/serial/by-id/ /dev/v4l/by-id/
   ```
3. `docker compose up -d --build`
4. Open `http://<pi-ip>:47812`. It connects to the dongle's video and
   keyboard/mouse by itself; if it can't (say, the dongle is unplugged), the
   app's device dialog stays up with an error, and you can retry from there.
   Add `?autoconnect=0` to the URL to always pick devices by hand.

The image is based on Alpine Linux (about 90MB). ustreamer isn't packaged for
Alpine, so the build compiles a pinned release from source; that takes a
minute or two on a Pi. Everything else comes from Alpine's packages.

## Configuration

Environment variables in `docker-compose.yml`:

| Variable | Default | |
| --- | --- | --- |
| `VIDEO_DEVICE` | auto | Capture device, e.g. `/dev/video0`. `none` disables video. |
| `SERIAL_DEVICE` | auto | Serial device, e.g. `/dev/ttyACM0`. |
| `VIDEO_RESOLUTION` | `1920x1080` | Starting resolution. The app's resolution menu changes it live. |
| `VIDEO_FPS` | `30` | Capture frame rate. |
| `SERIAL_BAUD` | `57600` | Serial baud rate. |
| `USTREAMER_EXTRA_ARGS` | | Extra ustreamer flags, e.g. `--brightness 60`. |
| `AUDIO` | `off` | `on` to stream audio (see below). |
| `AUDIO_DEVICE` | auto | ALSA device, e.g. `plughw:1`. |
| `AUTH_USER`, `AUTH_PASSWORD` | | Require HTTP Basic auth for everything when both are set. |
| `UPDATE_CHECK` | `on` | `off` to stop checking for new NanoKVM-USB releases. |
| `UPDATE_CHECK_INTERVAL` | `21600` | Seconds between update checks (minimum 60). |

Build args, under `build.args`:

| Arg | Default | |
| --- | --- | --- |
| `NANOKVM_USB_VERSION` | `1.1.4` | NanoKVM-USB browser release baked into the image. |
| `USTREAMER_VERSION`, `USTREAMER_COMMIT` | `6.67`, its commit | ustreamer release to build. The build checks the tag still points at that commit, so change both together. |
| `WITH_AUDIO` | `false` | Install `alsa-utils`, needed for audio. |

Auto-detection uses the first entry in `/dev/serial/by-id/` and the first
`*-video-index0` entry in `/dev/v4l/by-id/`, falling back to `/dev/ttyACM0`,
`/dev/ttyUSB0` and `/dev/video0`. Set the device variables explicitly if the
Pi has other USB serial or video devices attached. A dongle plugged in after
the container starts is picked up automatically.

`GET /api/status` shows what the bridge found:

```bash
curl http://<pi-ip>:47812/api/status
```

`v4l2-ctl` isn't in the image. To inspect a capture device's formats, install
it temporarily (it's gone after the next container restart):

```bash
docker exec nanokvm-pi apk add --no-cache v4l-utils
docker exec nanokvm-pi v4l2-ctl -d /dev/video0 --list-formats-ext
```

### Audio

Off by default. To enable it, in `docker-compose.yml`:

1. Set `WITH_AUDIO: "true"` under `build.args` and `AUDIO: "on"` under
   `environment`.
2. Uncomment the `c 116:* rmw` device cgroup rule.
3. Run `docker compose up -d --build`.

The app then offers the dongle's audio alongside its video. If the wrong
sound card is picked, set `AUDIO_DEVICE` (see `cat /proc/asound/cards`).

## Updating the NanoKVM-USB frontend

The container checks GitHub for new NanoKVM-USB releases. When one is out,
the app shows a banner with an **Update now** button, which downloads the
release, installs it, and reloads the page. The same from the command line:

```bash
curl -X POST http://<pi-ip>:47812/api/update
curl -X POST http://<pi-ip>:47812/api/update -H 'Content-Type: application/json' -d '{"version": "1.1.5"}'
```

`GET /api/version` shows the installed and latest versions.

The installed frontend lives in the `nanokvm_data` volume, so it survives
container recreation. On start, the container installs the build baked into
the image if the volume has none or an older one; a newer one installed from
the app is kept. So pinning a version in `docker-compose.yml`
(`NANOKVM_USB_VERSION`) and rebuilding also works.

## Reverse proxy

Behind a reverse proxy:

- Forward WebSocket upgrades (`Upgrade` and `Connection` headers) for `/ws/`.
- Turn off response buffering for `/stream`, or video will lag.
- Serve it at the root of a (sub)domain. The app loads `/assets/...` by
  absolute path, so a path prefix like `example.com/kvm/` breaks it.

For example, with nginx:

```nginx
location / {
    proxy_pass http://<pi-ip>:47812;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_buffering off;
}
```

Over HTTPS, the app's fullscreen mode can also capture shortcuts like Ctrl+W
and Alt+Tab (`navigator.keyboard.lock()`) and send them to the target.

## Security

Anyone who can reach the port has full keyboard and mouse control of the
target machine. Keep it on a trusted network or VPN, set `AUTH_USER` and
`AUTH_PASSWORD`, or put it behind a reverse proxy with authentication. There's
no built-in TLS; terminate HTTPS at the proxy.

## Testing without hardware

`dev/fake_mjpeg.py` stands in for ustreamer with a synthetic test pattern.
Stop the main container first (the test one uses the same port), then:

```bash
docker compose run -d --rm --name nanokvm-pi-test -p 47812:80 -e VIDEO_DEVICE=none nanokvm-pi
docker exec nanokvm-pi-test apk add --no-cache py3-pillow
docker exec -d nanokvm-pi-test python3 /app/fake_mjpeg.py
```

Open `http://localhost:47812`; the test pattern appears. `docker stop
nanokvm-pi-test` when done. Keyboard and mouse need the real dongle; audio can
be faked with `AUDIO=on` and `AUDIO_DEVICE=test` (a 440 Hz tone).

For a full automated run with a fake serial port and a fake GitHub as well,
see `dev/run_e2e.sh`.

## Alternatives

[One-KVM](https://github.com/mofeng-git/One-KVM) is a more featureful
PiKVM-style project that also supports CH9329-based devices like this one. It
runs privileged with host networking and uses its own UI rather than
NanoKVM-USB's.

## Status

Tested with a NanoKVM-USB on a Raspberry Pi running DietPi (64-bit): video,
keyboard and mouse work end to end from a browser on another machine.
