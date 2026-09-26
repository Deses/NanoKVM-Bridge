# NanoKVM-Pi

Run [Sipeed's NanoKVM-USB](https://github.com/sipeed/NanoKVM-USB) dongle
plugged into a Raspberry Pi that lives next to the machine you're managing,
instead of into whatever laptop you happen to have with you. One container
on the Pi, one port to open from anywhere on your network.

```
[target machine] --HDMI+USB--> [NanoKVM-USB] --USB--> [Raspberry Pi] --LAN--> [your browser]
```

## How it works

The NanoKVM-USB shows up on Linux as two ordinary devices: a UVC (webcam-like)
video capture device and a USB-serial port speaking a CH9329-style HID
protocol at 57600 baud. Sipeed's own [browser app](https://github.com/sipeed/NanoKVM-USB/tree/main/browser)
talks to both directly from Chrome via WebSerial and `getUserMedia` - which
only works when the browser and the dongle are on the same machine.

This project serves that *exact, unmodified* browser build, plus a small
JavaScript shim that replaces those two browser APIs with versions backed by
a WebSocket/HTTP bridge running on the Pi:

- **Video**: [ustreamer](https://github.com/pikvm/ustreamer) reads the
  dongle's MJPEG frames and passes them straight through; the shim turns
  that into a `MediaStream` the app can display.
- **Keyboard/mouse**: the app's HID packets travel over a WebSocket straight
  to the dongle's serial port.
- **Audio** (optional, off by default): raw PCM from the dongle's USB audio
  input travels over a second WebSocket and plays back on your side.

Everything is served through nginx on a single port, so you can point a
reverse proxy at it like any other web app.

## Setup

1. Wire it up: NanoKVM-USB's **HOST** port → a USB 3.0 port on the Pi.
   HDMI-in and the target's USB port go to the target machine as usual.
2. Copy this `NanoKVM-Pi/` folder to the Pi.
3. `docker compose up -d --build`
4. Open `http://<pi-ip>:47812` from any computer on your network, pick the
   video device, click "select serial port" - same flow as the official app,
   just now reachable from anywhere.

(Port 47812 is arbitrary and deliberately not `8080`/`8000`/etc., since this
container hands out full keyboard and mouse control of the target machine -
see **Security** below.)

Building on the Pi itself works fine: every package this image needs
(`ustreamer`, `nginx-light`, `python3-aiohttp`, ...) is available for both
`arm64` and `armhf` in Debian, so there's no cross-compilation involved.

## Configuration

All via environment variables in `docker-compose.yml`:

| Variable | Default | Meaning |
| --- | --- | --- |
| `VIDEO_DEVICE` | auto-detect | `/dev/videoN`, or `none` to disable video (for testing) |
| `SERIAL_DEVICE` | auto-detect | `/dev/ttyACM0`, `/dev/ttyUSB0`, etc. |
| `VIDEO_RESOLUTION` | `1920x1080` | Initial capture resolution; the app's own resolution menu changes this live |
| `VIDEO_FPS` | `30` | ustreamer's `--desired-fps` |
| `SERIAL_BAUD` | `57600` | The dongle's CH9329-style serial baud rate |
| `USTREAMER_EXTRA_ARGS` | *(empty)* | Extra flags appended to the ustreamer command, e.g. a hardware encoder |
| `AUDIO` | `off` | Set to `on` to enable audio (see below) |
| `AUDIO_DEVICE` | auto-detect | ALSA device, e.g. `hw:1` |
| `AUTH_USER` / `AUTH_PASSWORD` | unset | HTTP basic auth in front of everything, if set |

Auto-detection looks at `/dev/serial/by-id/*` and `/dev/v4l/by-id/*` first
(stable names tied to the USB device), falling back to `/dev/ttyACM0` /
`/dev/video0`. If you have other USB serial/video devices on the same Pi,
set `SERIAL_DEVICE`/`VIDEO_DEVICE` explicitly to avoid picking the wrong one.

Find the right values with:

```bash
ls -l /dev/serial/by-id/ /dev/v4l/by-id/
v4l2-ctl -d /dev/video0 --list-formats-ext   # confirm it offers MJPEG
```

`GET /api/status` (behind auth, if enabled) reports what the bridge
currently sees for troubleshooting:

```bash
curl http://<pi-ip>:47812/api/status
```

### Audio

Off by default, since most people diagnosing a headless server don't need
it, and it needs `/dev/snd` access. To enable it:

1. In `docker-compose.yml`, set `AUDIO: "on"` and uncomment the `c 116:* rmw`
   device cgroup rule (and optionally `AUDIO_DEVICE` if auto-detection picks
   the wrong ALSA card - check with `cat /proc/asound/cards`).
2. Rebuild/restart. The app's video device dropdown will then also offer a
   matching audio input, exactly like it would with a directly-attached
   dongle.

## Reverse proxy

This works fine behind a reverse proxy, with three things to get right:

- **WebSocket upgrade** must be forwarded for `/ws/` (both `Upgrade` and
  `Connection: upgrade` headers).
- **Buffering off** for `/stream`, or your proxy will happily sit on MJPEG
  frames instead of forwarding them as they arrive, adding latency.
- **Serve at the domain/subdomain root.** The official app's `index.html`
  references `/assets/...` with absolute paths, and the shim itself talks to
  `/api/`, `/ws/`, `/stream` at the root. A path prefix (`example.com/kvm/`)
  will break asset loading; a dedicated (sub)domain works fine.

Example nginx reverse-proxy snippet (adjust for your setup):

```nginx
location / {
    proxy_pass http://<pi-ip>:47812;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_buffering off;
}
```

Serving this over HTTPS also lets the app's fullscreen mode use
`navigator.keyboard.lock()`, which captures browser/OS shortcuts like
Ctrl+W or Alt+Tab so they reach the target machine instead of your own.

## Security

**This container gives full keyboard and mouse control of whatever the
dongle is plugged into, to anyone who can reach the port.** At minimum:

- Keep it on a trusted LAN or behind a VPN/Tailscale, and/or
- Set `AUTH_USER`/`AUTH_PASSWORD` for HTTP basic auth, and/or
- Put it behind a reverse proxy that adds real authentication.

There's no built-in TLS - terminate HTTPS at your reverse proxy if you
expose this beyond your LAN.

## Testing without hardware

`dev/fake_mjpeg.py` stands in for both the dongle's video and ustreamer, so
you can sanity-check the container, the bridge, and the shim without the
dongle plugged in:

```bash
docker compose run --rm -e VIDEO_DEVICE=none -p 47812:80 nanokvm-pi &
docker exec -it nanokvm-pi python3 /app/fake_mjpeg.py &
```

Open `http://localhost:47812`, select the "NanoKVM-USB via Pi" video device
- you should see a synthetic test pattern. Serial and audio need real
hardware (or a fake serial pair / `AUDIO_DEVICE=test`) to exercise fully.

## Alternatives

If this doesn't work well for your setup, [One-KVM](https://github.com/mofeng-git/One-KVM)
is a more full-featured, actively developed PiKVM-style project that also
supports CH9329-based dongles like this one (at 57600 baud) - at the cost of
running `--privileged`/host networking and using its own UI rather than the
official NanoKVM-USB one.

## Status

This is a first pass, built and tested against a fake serial/video device
(no physical NanoKVM-USB available). It hasn't yet been run against
real hardware - expect to find and fix a few rough edges on first boot with
the actual dongle.
