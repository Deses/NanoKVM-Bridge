#!/usr/bin/env python3
"""Serves the NanoKVM-USB app and bridges it to the dongle."""

import asyncio
import base64
import glob
import hmac
import logging
import math
import os
import shlex
import struct
import subprocess
import threading
import time

import aiohttp
import serial
from aiohttp import WSMsgType, web

import updater

logging.basicConfig(level=logging.INFO, format="[bridge] %(message)s")
LOG = logging.getLogger("nanokvm-bridge")

HTTP_PORT = 80
STREAM_PORT = 8081
SHIM_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nanokvm-bridge-shim.js")
NO_CACHE_PATHS = {"/", "/index.html", "/nanokvm-bridge-shim.js"}

SERIAL_BAUD = int(os.environ.get("SERIAL_BAUD", "57600"))
VIDEO_RESOLUTION = os.environ.get("VIDEO_RESOLUTION", "1920x1080")
VIDEO_FPS = int(os.environ.get("VIDEO_FPS", "30"))
USTREAMER_EXTRA_ARGS = shlex.split(os.environ.get("USTREAMER_EXTRA_ARGS", ""))
AUDIO_ENABLED = os.environ.get("AUDIO", "off").strip().lower() in ("1", "true", "on", "yes")
AUDIO_RATE = 48000
AUDIO_CHANNELS = 2
AUTH_USER = os.environ.get("AUTH_USER", "")
AUTH_PASSWORD = os.environ.get("AUTH_PASSWORD", "")
AUTH_ENABLED = bool(AUTH_USER and AUTH_PASSWORD)
KEYBOARD_LAYOUT = os.environ.get("KEYBOARD_LAYOUT", "en-US").strip()


# Re-resolved on every (re)connect, so a replugged dongle is found again.
def resolve_serial_device():
    if os.environ.get("SERIAL_DEVICE"):
        return os.environ["SERIAL_DEVICE"]
    matches = sorted(glob.glob("/dev/serial/by-id/*"))
    if matches:
        return matches[0]
    for candidate in ("/dev/ttyACM0", "/dev/ttyUSB0"):
        if os.path.exists(candidate):
            return candidate
    return None


def resolve_video_device():
    env = os.environ.get("VIDEO_DEVICE", "").strip()
    if env:
        return None if env.lower() == "none" else env
    # index1 is the capture card's metadata-only node.
    matches = sorted(glob.glob("/dev/v4l/by-id/*-video-index0"))
    if matches:
        return matches[0]
    return "/dev/video0" if os.path.exists("/dev/video0") else None


def resolve_audio_device():
    if os.environ.get("AUDIO_DEVICE"):
        return os.environ["AUDIO_DEVICE"]  # "test": synthetic tone
    try:
        with open("/proc/asound/cards") as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if line[:1].isdigit() and "USB-Audio" in line:
            # plughw converts to the rate/channels we ask for.
            return f"plughw:{line.split()[0]}"
    return None


def valid_resolution(width, height):
    return 1 <= width <= 7680 and 1 <= height <= 4320


def parse_resolution(value):
    try:
        width, height = (int(part) for part in value.lower().split("x"))
    except ValueError:
        width = height = 0
    if not valid_resolution(width, height):
        LOG.warning("invalid VIDEO_RESOLUTION %r, using 1920x1080", value)
        return 1920, 1080
    return width, height


async def broadcast(clients, data):
    for ws in list(clients):
        try:
            await ws.send_bytes(data)
        except (ConnectionResetError, RuntimeError):
            clients.discard(ws)


class SerialBridge:
    def __init__(self):
        self.clients = set()
        self.port = None
        self.last_error = None
        self._stop = None
        self._loop = None

    def status(self):
        return {
            "device": self.port.port if self.port else resolve_serial_device(),
            "connected": self.port is not None,
            "clients": len(self.clients),
            "baud_rate": SERIAL_BAUD,
            "last_error": self.last_error,
        }

    def open_if_needed(self):
        self._loop = asyncio.get_running_loop()
        if self.port is None:
            self._open()
        return self.port is not None

    def add_client(self, ws):
        self.clients.add(ws)

    def remove_client(self, ws):
        self.clients.discard(ws)
        if not self.clients:
            self.close()

    def write(self, data):
        if self.port is None:
            return
        try:
            self.port.write(data)
        except (serial.SerialException, OSError) as exc:
            self.last_error = f"write failed: {exc}"
            LOG.warning(self.last_error)

    def _open(self):
        device = resolve_serial_device()
        if not device:
            self.last_error = "no serial device found"
            LOG.warning(self.last_error)
            return
        try:
            port = serial.Serial(device, SERIAL_BAUD, timeout=0.2)
        except (serial.SerialException, OSError) as exc:
            self.last_error = f"failed to open {device}: {exc}"
            LOG.warning(self.last_error)
            return
        # Per-open stop event, so a lingering reader thread never reads the new port.
        stop = threading.Event()
        self.port, self._stop, self.last_error = port, stop, None
        threading.Thread(target=self._read_loop, args=(port, stop), daemon=True).start()
        LOG.info("serial opened: %s @ %d baud", device, SERIAL_BAUD)

    def close(self):
        if self.port is None:
            return
        self._stop.set()
        try:
            self.port.close()
        except (serial.SerialException, OSError):
            pass
        self.port = self._stop = None
        LOG.info("serial closed")

    def _read_loop(self, port, stop):
        while not stop.is_set():
            try:
                data = port.read(256)
            except Exception as exc:  # unplugged, or closed under us
                if not stop.is_set():
                    asyncio.run_coroutine_threadsafe(self._device_lost(port, exc), self._loop)
                return
            if data:
                asyncio.run_coroutine_threadsafe(broadcast(self.clients, data), self._loop)

    async def _device_lost(self, port, exc):
        if self.port is not port:
            return
        self.last_error = f"read failed: {exc}"
        LOG.warning(self.last_error)
        self.close()
        # Closing the sockets makes the app show its device picker again.
        stale = list(self.clients)
        self.clients.difference_update(stale)
        for ws in stale:
            await ws.close()


class Streamer:
    def __init__(self):
        self.device = None
        self.width, self.height = parse_resolution(VIDEO_RESOLUTION)
        self.proc = None
        self._lock = asyncio.Lock()
        self._spawned_at = 0.0
        self._next_spawn = 0.0

    def status(self):
        return {
            "device": self.device,
            "running": self._running(),
            "resolution": f"{self.width}x{self.height}",
            "fps": VIDEO_FPS,
        }

    def _running(self):
        return self.proc is not None and self.proc.poll() is None

    async def set_resolution(self, width, height):
        async with self._lock:
            if (width, height) == (self.width, self.height):
                return
            self.width, self.height = width, height
            if self.device:
                LOG.info("restarting ustreamer at %dx%d", width, height)
                await self._terminate()
                self._spawn()

    async def stop(self):
        async with self._lock:
            await self._terminate()

    async def supervise(self):
        delay = 1.0
        warned = False
        while True:
            async with self._lock:
                if self.device is None:
                    self.device = resolve_video_device()
                    if self.device:
                        LOG.info("video device: %s", self.device)
                    elif not warned:
                        LOG.warning("no video device found yet; /stream is unavailable")
                        warned = True
                if self.device and not self._running():
                    if self.proc is not None:
                        uptime = time.monotonic() - self._spawned_at
                        delay = 1.0 if uptime > 30 else min(delay * 2, 30.0)
                        LOG.warning(
                            "ustreamer exited (code %s), restarting in %.0fs",
                            self.proc.returncode, delay,
                        )
                        self.proc = None
                        self._next_spawn = time.monotonic() + delay
                    if time.monotonic() >= self._next_spawn:
                        self._spawn()
            await asyncio.sleep(1)

    def _spawn(self):
        cmd = [
            "ustreamer",
            "--device", self.device,
            "--format", "MJPEG",
            "--encoder", "HW",  # forward the dongle's JPEGs as-is
            "--resolution", f"{self.width}x{self.height}",
            "--desired-fps", str(VIDEO_FPS),
            "--drop-same-frames", "30",
            "--slowdown",
            "--exit-on-parent-death",
            "--host", "127.0.0.1",
            "--port", str(STREAM_PORT),
            *USTREAMER_EXTRA_ARGS,
        ]
        LOG.info("starting %s", " ".join(cmd))
        self._spawned_at = time.monotonic()
        self.proc = subprocess.Popen(cmd)

    async def _terminate(self):
        proc, self.proc = self.proc, None
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            await asyncio.to_thread(proc.wait, 3)
        except subprocess.TimeoutExpired:
            proc.kill()
            await asyncio.to_thread(proc.wait)


class AudioBridge:
    def __init__(self):
        self.clients = set()
        self.device = None
        self._stop = None
        self._thread = None
        self._loop = None

    def status(self):
        return {
            "enabled": AUDIO_ENABLED,
            "device": self.device,
            "running": self._thread is not None and self._thread.is_alive(),
            "clients": len(self.clients),
        }

    async def add_client(self, ws):
        self._loop = asyncio.get_running_loop()
        self.clients.add(ws)
        if self._stop is None or not self._thread.is_alive():
            self._start()

    def remove_client(self, ws):
        self.clients.discard(ws)
        if not self.clients:
            self.stop()

    def stop(self):
        if self._stop is not None:
            self._stop.set()
            self._stop = self._thread = None

    def _start(self):
        # Per session: the card number can change on replug.
        self.device = resolve_audio_device()
        if not self.device:
            LOG.warning("AUDIO=on but no USB audio capture device found")
            return
        stop = threading.Event()
        target = self._tone_loop if self.device == "test" else self._arecord_loop
        self._stop = stop
        self._thread = threading.Thread(target=target, args=(stop,), daemon=True)
        self._thread.start()

    def _send(self, data):
        asyncio.run_coroutine_threadsafe(broadcast(self.clients, data), self._loop)

    def _arecord_loop(self, stop):
        cmd = [
            "arecord", "-q", "-D", self.device, "-t", "raw",
            "-f", "S16_LE", "-r", str(AUDIO_RATE), "-c", str(AUDIO_CHANNELS),
        ]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError:
            LOG.warning("arecord not found - rebuild the image with WITH_AUDIO=true")
            return
        chunk_size = AUDIO_RATE * AUDIO_CHANNELS * 2 // 50  # 20ms
        try:
            while not stop.is_set():
                data = proc.stdout.read(chunk_size)
                if not data:
                    break
                self._send(data)
        finally:
            proc.terminate()
            _, err = proc.communicate()
            if not stop.is_set():
                LOG.warning("arecord exited: %s", err.decode(errors="replace").strip())

    def _tone_loop(self, stop):
        samples = AUDIO_RATE // 50  # 20ms
        index = 0
        deadline = time.monotonic()
        while not stop.is_set():
            buf = bytearray()
            for _ in range(samples):
                value = int(3000 * math.sin(2 * math.pi * 440 * index / AUDIO_RATE))
                buf += struct.pack("<hh", value, value)
                index += 1
            self._send(bytes(buf))
            deadline += 0.02
            time.sleep(max(0.0, deadline - time.monotonic()))


SERIAL = web.AppKey("serial", SerialBridge)
AUDIO = web.AppKey("audio", AudioBridge)
STREAMER = web.AppKey("streamer", Streamer)
UPDATER = web.AppKey("updater", updater.Updater)
HTTP_CLIENT = web.AppKey("http_client", aiohttp.ClientSession)
TASKS = web.AppKey("tasks", list)


def _credentials_match(header):
    if not header.startswith("Basic "):
        return False
    try:
        user, _, password = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except ValueError:
        return False
    # compare_digest rejects non-ASCII str.
    user_ok = hmac.compare_digest(user.encode(), AUTH_USER.encode())
    password_ok = hmac.compare_digest(password.encode(), AUTH_PASSWORD.encode())
    return user_ok and password_ok


@web.middleware
async def auth_middleware(request, handler):
    if not AUTH_ENABLED or _credentials_match(request.headers.get("Authorization", "")):
        return await handler(request)
    return web.Response(
        status=401,
        headers={"WWW-Authenticate": 'Basic realm="NanoKVM-Bridge"'},
        text="Unauthorized",
    )


@web.middleware
async def hide_dotfiles_middleware(request, handler):
    if any(part.startswith(".") for part in request.path.split("/")):
        raise web.HTTPNotFound()
    return await handler(request)


async def add_response_headers(request, response):
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-Content-Type-Options"] = "nosniff"
    if request.path in NO_CACHE_PATHS:
        # So an updated frontend never references stale cached asset hashes.
        response.headers["Cache-Control"] = "no-cache"


async def index_handler(request):
    index_path = os.path.join(updater.WWW_DIR, "index.html")
    if not os.path.isfile(index_path):
        raise web.HTTPServiceUnavailable(text="frontend not installed")
    return web.FileResponse(index_path)


async def shim_handler(request):
    return web.FileResponse(SHIM_PATH)


async def stream_handler(request):
    try:
        upstream = await request.app[HTTP_CLIENT].get(
            f"http://127.0.0.1:{STREAM_PORT}/stream",
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=5),
        )
    except aiohttp.ClientError as exc:
        raise web.HTTPServiceUnavailable(text=f"video stream unavailable: {exc}")

    try:
        response = web.StreamResponse(
            status=upstream.status,
            headers={"Content-Type": upstream.headers.get("Content-Type", "text/plain")},
        )
        await response.prepare(request)
        async for chunk in upstream.content.iter_any():
            await response.write(chunk)
    except (ConnectionResetError, aiohttp.ClientError):
        pass  # viewer left or ustreamer restarted; the shim reconnects
    finally:
        upstream.close()
    return response


async def ws_serial_handler(request):
    bridge = request.app[SERIAL]
    # No device: refuse the handshake so the app shows a connect error.
    if not bridge.open_if_needed():
        raise web.HTTPServiceUnavailable(text=bridge.last_error)
    ws = web.WebSocketResponse()
    try:
        await ws.prepare(request)
    except (ConnectionResetError, aiohttp.ClientError):
        if not bridge.clients:
            bridge.close()
        raise
    bridge.add_client(ws)
    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                bridge.write(msg.data)
    finally:
        bridge.remove_client(ws)
    return ws


async def ws_audio_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    if not AUDIO_ENABLED:
        await ws.close(message=b"audio disabled")
        return ws
    bridge = request.app[AUDIO]
    await bridge.add_client(ws)
    try:
        async for _ in ws:
            pass
    finally:
        bridge.remove_client(ws)
    return ws


async def api_status(request):
    return web.json_response({
        "serial": request.app[SERIAL].status(),
        "video": request.app[STREAMER].status(),
        "audio": request.app[AUDIO].status(),
        "keyboard": {"layout": KEYBOARD_LAYOUT},
    })


async def api_video(request):
    try:
        body = await request.json()
        width, height = int(body["width"]), int(body["height"])
    except (ValueError, KeyError, TypeError):
        raise web.HTTPBadRequest(text='expected JSON {"width": int, "height": int}')
    if not valid_resolution(width, height):
        raise web.HTTPBadRequest(text="resolution out of range")
    await request.app[STREAMER].set_resolution(width, height)
    return web.json_response({"ok": True})


async def api_version(request):
    return web.json_response(request.app[UPDATER].status())


async def api_update(request):
    version = None
    if request.can_read_body:
        try:
            body = await request.json()
        except ValueError:
            raise web.HTTPBadRequest(text="expected a JSON body")
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text='expected JSON {"version": "x.y.z"}')
        version = body.get("version")
    try:
        installed = await request.app[UPDATER].perform_update(version)
    except updater.UpdateError as exc:
        return web.json_response({"error": str(exc)}, status=409)
    return web.json_response({"ok": True, "version": installed})


async def on_startup(app):
    app[HTTP_CLIENT] = aiohttp.ClientSession()
    app[TASKS] = [
        asyncio.create_task(app[STREAMER].supervise()),
        asyncio.create_task(app[UPDATER].run()),
    ]


async def on_shutdown(app):
    # WebSocket and /stream handlers never return on their own; end them so
    # `docker stop` doesn't SIGKILL. Closing the session ends every /stream proxy.
    for ws in list(app[SERIAL].clients) + list(app[AUDIO].clients):
        await ws.close(code=aiohttp.WSCloseCode.GOING_AWAY, message=b"shutting down")
    await app[HTTP_CLIENT].close()


async def on_cleanup(app):
    for task in app[TASKS]:
        task.cancel()
    await app[STREAMER].stop()
    app[SERIAL].close()
    app[AUDIO].stop()


def create_app():
    app = web.Application(middlewares=[auth_middleware, hide_dotfiles_middleware])
    app[SERIAL] = SerialBridge()
    app[AUDIO] = AudioBridge()
    app[STREAMER] = Streamer()
    app[UPDATER] = updater.Updater()

    # Static route last: it matches every path.
    app.router.add_get("/", index_handler)
    app.router.add_get("/nanokvm-bridge-shim.js", shim_handler)
    app.router.add_get("/stream", stream_handler)
    app.router.add_get("/ws/serial", ws_serial_handler)
    app.router.add_get("/ws/audio", ws_audio_handler)
    app.router.add_get("/api/status", api_status)
    app.router.add_post("/api/video", api_video)
    app.router.add_get("/api/version", api_version)
    app.router.add_post("/api/update", api_update)
    app.router.add_static("/", updater.WWW_DIR)

    app.on_response_prepare.append(add_response_headers)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    app.on_cleanup.append(on_cleanup)
    return app


def main():
    updater.install_baked()
    LOG.info("audio %s, auth %s",
             "on" if AUDIO_ENABLED else "off",
             f"on (user {AUTH_USER!r})" if AUTH_ENABLED else "off")
    web.run_app(create_app(), port=HTTP_PORT, shutdown_timeout=3, print=None)


if __name__ == "__main__":
    main()
