# NanoKVM-Bridge

Use a [Sipeed NanoKVM-USB](https://github.com/sipeed/NanoKVM-USB) over the network. Plug it into a Linux computer that sits next to the machine you want to control, run one container, and open the official NanoKVM-USB web UI from any browser. A Raspberry Pi is a good fit, but a mini PC or an old laptop works just as well.

```
[target machine] --HDMI + USB--> [NanoKVM-USB] --USB--> [Linux host] --LAN--> [your browser]
```

This is only for the NanoKVM-USB. Sipeed's network NanoKVM models already have their own web UI, and other USB KVMs aren't supported.

## How it works

On Linux, the NanoKVM-USB shows up as a UVC capture device (MJPEG video) and an USB serial port. Sipeed's [browser app](https://github.com/sipeed/NanoKVM-USB/tree/main/browser) talks to both through Chrome's Web Serial and `getUserMedia` APIs, and those only work when the dongle is plugged into the machine running the browser.

This container serves Sipeed's browser build unmodified, plus a small JavaScript shim that swaps those APIs for versions that go over the network. 
Video comes from [ustreamer](https://github.com/pikvm/ustreamer), which forwards the dongle's MJPEG frames as they are, and the shim turns them into a `MediaStream`. The app's keyboard and mouse packets go over a WebSocket to the dongle's serial port.
Audio is optional and uses a second WebSocket carrying raw PCM from the dongle's USB audio input.

One Python process (`server/bridge.py`, aiohttp) serves all of it on a single port.

## Setup

1. Connect the NanoKVM-USB's HOST port to a USB port on the host (3.0 if it has one), and its HDMI and target USB ports to the machine you want to control.
2. Check that the host sees it:
   ```bash
   ls -l /dev/serial/by-id/ /dev/v4l/by-id/
   ```
3. `docker compose pull && docker compose up -d` fetches the image (x86-64, 32-bit x86, arm64 and 32-bit arm) from GHCR.
   `docker compose up -d --build` builds it locally instead.
4. Open `http://<host>:47812`. It connects to the dongle's video and keyboard/mouse by itself.
   If it can't (say, the dongle is unplugged), the app's device dialog stays up with an error and you can retry from there.
   Add `?autoconnect=0` to the URL if you'd rather pick devices by hand.

The image is based on Alpine Linux and weighs about 90MB.

`latest` is built from `main`. To try the development branch, put `IMAGE_TAG=dev` in a `.env` file next to `docker-compose.yml` and pull again. Remove it to go back to `latest`.

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
| `KEYBOARD_LAYOUT` | `en-US` | Keyboard layout of the target machine, used by the phone keyboard (see below). One of the [supported layouts](web/layouts/README.md), as a [language-COUNTRY code](https://en.wikipedia.org/wiki/IETF_language_tag). |

Build args, under `build.args`:

| Arg | Default | |
| --- | --- | --- |
| `NANOKVM_USB_VERSION` | `1.1.4` | NanoKVM-USB browser release baked into the image. |
| `USTREAMER_VERSION`, `USTREAMER_COMMIT` | `6.67`, its commit | ustreamer release to build. The build checks the tag still points at that commit, so change both together. |
| `WITH_AUDIO` | `false` | Install `alsa-utils`, needed for audio. |

Auto-detection picks the first entry in `/dev/serial/by-id/` and the first `*-video-index0` entry in `/dev/v4l/by-id/`, and falls back to `/dev/ttyACM0`, `/dev/ttyUSB0` and `/dev/video0`. If the host has other USB serial or video devices attached, set the device variables explicitly.
A dongle plugged in after the container starts is picked up automatically.

To see what the bridge found:

```bash
curl http://<host>:47812/api/status
```

`v4l2-ctl` isn't in the image. To check a capture device's formats, install it
for the moment (it's gone after the next container restart):

```bash
docker exec nanokvm-bridge apk add --no-cache v4l-utils
docker exec nanokvm-bridge v4l2-ctl -d /dev/video0 --list-formats-ext
```

### Audio

Audio is off by default. To turn it on, edit `docker-compose.yml`:

1. Set `WITH_AUDIO: "true"` under `build.args` and `AUDIO: "on"` under `environment`.
2. Uncomment the `c 116:* rmw` device cgroup rule.
3. Run `docker compose up -d --build`. The published image is built without audio, so this needs a local build.

The app then offers the dongle's audio next to its video. If it picks the wrong sound card, set `AUDIO_DEVICE` (see `cat /proc/asound/cards`).

### Phones and tablets

On a touch screen there's no way to open the phone's keyboard over a video, so a keyboard button appears in the bottom right corner. It opens the phone's own keyboard plus a bar with the keys phones lack: Esc, Tab, Ctrl, Alt, Win, arrows, Del and Ctrl+Alt+Del.
Ctrl, Alt and Win stay pressed until the next key, so Ctrl then C sends Ctrl+C.

A USB keyboard sends key positions, not characters, and the target turns them into characters with its own layout. So set `KEYBOARD_LAYOUT` to the target's layout, or characters like `ñ` and `@` will come out wrong. Supported layouts are English (US and UK), Spanish (Spain and Latin America), Portuguese (Brazil and Portugal), French, German, Italian and Russian; see [web/layouts](web/layouts/README.md) for their codes and for how to add one. Characters the layout doesn't have are skipped.

## Updating the NanoKVM-USB frontend

The container checks GitHub for new NanoKVM-USB releases. When there is one, the app shows a banner with an "Update now" button that downloads the release, installs it and reloads the page. You can do the same from the command line:

```bash
curl -X POST http://<host>:47812/api/update
curl -X POST http://<host>:47812/api/update -H 'Content-Type: application/json' -d '{"version": "1.1.5"}'
```

`GET /api/version` shows the installed and latest versions.

The installed frontend lives in the `nanokvm_data` volume, so it survives the container being recreated. On start, the container installs the build baked into the image if the volume has none or an older one, and keeps a newer one installed from the app. Pinning `NANOKVM_USB_VERSION` in `docker-compose.yml` and rebuilding works too.

## Reverse proxy

Behind a reverse proxy:

- Forward WebSocket upgrades (`Upgrade` and `Connection` headers) for `/ws/`.
- Turn off response buffering for `/stream`, or video will lag.
- Serve it at the root of a (sub)domain. The app loads `/assets/...` by
  absolute path, so a path prefix like `example.com/kvm/` breaks it.

For example, with nginx:

```nginx
location / {
    proxy_pass http://<host>:47812;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_buffering off;
}
```

Or Caddy:

```caddy
kvm.example.com {
	reverse_proxy <host>:47812 {
		flush_interval -1
	}
}
```

Over HTTPS, the app's fullscreen mode can also capture shortcuts like Ctrl+W and Alt+Tab (`navigator.keyboard.lock()`) and pass them to the target.

## Security

Anyone who can reach the port gets full keyboard and mouse control of the target machine. Keep it on a trusted network or VPN, set `AUTH_USER` and `AUTH_PASSWORD`, or put it behind a reverse proxy with authentication.
There's no built-in TLS, so terminate HTTPS at the proxy.

## Testing without hardware

`dev/fake_mjpeg.py` stands in for ustreamer and serves a test pattern. The test container uses the same port, so stop the main one first, then:

```bash
docker compose run -d --rm --name nanokvm-bridge-test -p 47812:80 -e VIDEO_DEVICE=none nanokvm-bridge
docker exec nanokvm-bridge-test apk add --no-cache py3-pillow
docker exec -d nanokvm-bridge-test python3 /app/fake_mjpeg.py
```

Open `http://localhost:47812` and the test pattern appears. Run `docker stop nanokvm-bridge-test` when you're done.
Keyboard and mouse need the real dongle, but you can fake audio with `AUDIO=on` and `AUDIO_DEVICE=test` (you'll hear a tone).

`dev/run_e2e.sh` does a full automated run that also fakes the serial port and GitHub.

## Future/Ideas

I might grow this into a control center: one page for several NanoKVM-USBs, and for other networked NanoKVMs (I own a NanoKVM-PCI), so you can switch between machines from one place.
