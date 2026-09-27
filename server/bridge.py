#!/usr/bin/env python3
"""Bridges the NanoKVM-USB app to the dongle, behind nginx."""

import asyncio
import glob
import logging
import math
import os
import shlex
import struct
import subprocess
import threading
import time

from aiohttp import web, WSMsgType

import updater

logging.basicConfig(level=logging.INFO, format="[bridge] %(message)s")
LOG = logging.getLogger("nanokvm-pi-bridge")

BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 8090
STREAM_PORT = 8081

SERIAL_BAUD = int(os.environ.get("SERIAL_BAUD", "57600"))
VIDEO_FPS = os.environ.get("VIDEO_FPS", "30")
USTREAMER_EXTRA_ARGS = shlex.split(os.environ.get("USTREAMER_EXTRA_ARGS", ""))
AUDIO_ENABLED = os.environ.get("AUDIO", "off").strip().lower() in ("1", "true", "on", "yes")
AUDIO_RATE = 48000
AUDIO_CHANNELS = 2


# Re-resolved on every (re)connect, so a replugged dongle is found again.
def resolve_serial_device():
    env = os.environ.get("SERIAL_DEVICE")
    if env:
        return env
    matches = sorted(glob.glob("/dev/serial/by-id/*"))
    if matches:
        return matches[0]
    for candidate in ("/dev/ttyACM0", "/dev/ttyUSB0"):
        if os.path.exists(candidate):
            return candidate
    return None


def resolve_video_device():
    env = os.environ.get("VIDEO_DEVICE")
    if env:
        return None if env.strip().lower() == "none" else env
    matches = sorted(glob.glob("/dev/v4l/by-id/*-video-index0"))
    if matches:
        return matches[0]
    if os.path.exists("/dev/video0"):
        return "/dev/video0"
    return None


def resolve_audio_device():
    env = os.environ.get("AUDIO_DEVICE")
    if env:
        return env  # "test": synthetic tone
    try:
        with open("/proc/asound/cards") as f:
            content = f.read()
    except OSError:
        return None
    for line in content.splitlines():
        line = line.strip()
        if line and line[0].isdigit() and "USB-Audio" in line:
            return f"hw:{line.split()[0]}"
    return None


class SerialBridge:
    def __init__(self):
        self.loop = None
        self.clients = set()
        self.port = None
        self.device_path = None
        self.last_error = None
        self._stop_flag = threading.Event()
        self._reader_thread = None

    def status(self):
        return {
            "device": self.device_path,
            "connected": self.port is not None and self.port.is_open,
            "clients": len(self.clients),
            "baud_rate": SERIAL_BAUD,
            "last_error": self.last_error,
        }

    async def add_client(self, ws):
        self.loop = asyncio.get_running_loop()
        self.clients.add(ws)
        if self.port is None:
            self._open()

    async def remove_client(self, ws):
        self.clients.discard(ws)
        if not self.clients:
            self._close()

    def write(self, data: bytes):
        if not self.port:
            return
        try:
            self.port.write(data)
        except Exception as exc:  # noqa: BLE001 - report and keep serving
            self.last_error = f"write failed: {exc}"
            LOG.warning(self.last_error)

    def _open(self):
        import serial  # local import: keeps module importable without pyserial for tests

        device = resolve_serial_device()
        self.device_path = device
        if not device:
            self.last_error = "no serial device found"
            LOG.warning(self.last_error)
            return
        try:
            self.port = serial.Serial(device, SERIAL_BAUD, timeout=0.2)
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"failed to open {device}: {exc}"
            LOG.warning(self.last_error)
            self.port = None
            return

        self.last_error = None
        self._stop_flag.clear()
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()
        LOG.info("serial opened: %s @ %d baud", device, SERIAL_BAUD)

    def _close(self):
        self._stop_flag.set()
        if self.port:
            try:
                self.port.close()
            except Exception:  # noqa: BLE001
                pass
        self.port = None
        LOG.info("serial closed (no clients)")

    def _read_loop(self):
        # Own thread: pyserial's blocking read() would stall the event loop.
        while not self._stop_flag.is_set() and self.port:
            try:
                data = self.port.read(256)
            except Exception as exc:  # noqa: BLE001 - device unplugged etc.
                if self._stop_flag.is_set():
                    # Closed by _close(), not a device error.
                    return
                self.last_error = f"read failed: {exc}"
                LOG.warning(self.last_error)
                if self.loop:
                    asyncio.run_coroutine_threadsafe(self._disconnect_all(), self.loop)
                return
            if data and self.loop:
                asyncio.run_coroutine_threadsafe(self._broadcast(data), self.loop)

    async def _broadcast(self, data: bytes):
        for ws in list(self.clients):
            try:
                await ws.send_bytes(data)
            except Exception:  # noqa: BLE001
                self.clients.discard(ws)

    async def _disconnect_all(self):
        for ws in list(self.clients):
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
        self.clients.clear()
        self._close()


class Streamer:
    def __init__(self):
        self.device = resolve_video_device()
        self.enabled = self.device is not None
        self.width, self.height = self._parse_resolution(
            os.environ.get("VIDEO_RESOLUTION", "1920x1080")
        )
        self.fps = VIDEO_FPS
        self.proc = None
        self._last_spawn = 0.0

    @staticmethod
    def _parse_resolution(res: str):
        try:
            w, h = res.lower().split("x")
            return int(w), int(h)
        except Exception:  # noqa: BLE001
            return 1920, 1080

    def status(self):
        return {
            "enabled": self.enabled,
            "device": self.device,
            "running": self.proc is not None and self.proc.poll() is None,
            "resolution": f"{self.width}x{self.height}",
            "fps": self.fps,
        }

    def start(self):
        if not self.enabled:
            LOG.warning("no video device found; /stream will be unavailable")
            return
        self._spawn()

    def stop(self):
        if not self.proc:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc = None

    def set_resolution(self, width: int, height: int):
        if (width, height) == (self.width, self.height):
            return
        self.width, self.height = width, height
        if self.enabled:
            LOG.info("resolution changed, restarting ustreamer at %dx%d", width, height)
            self.stop()
            self._spawn()

    def _spawn(self):
        cmd = [
            "ustreamer",
            "--device", self.device,
            "--format", "MJPEG",
            "--resolution", f"{self.width}x{self.height}",
            "--desired-fps", str(self.fps),
            "--drop-same-frames", "30",
            "--host", "127.0.0.1",
            "--port", str(STREAM_PORT),
            *USTREAMER_EXTRA_ARGS,
        ]
        LOG.info("starting ustreamer: %s", " ".join(cmd))
        self._last_spawn = time.time()
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)

    async def watchdog(self):
        backoff = 1.0
        while True:
            await asyncio.sleep(2)
            if not self.enabled:
                continue
            if self.proc is not None and self.proc.poll() is None:
                backoff = 1.0
                continue
            if time.time() - self._last_spawn < backoff:
                continue
            backoff = min(backoff * 2, 30.0)
            LOG.warning("ustreamer is not running, restarting")
            self._spawn()


class AudioBridge:
    def __init__(self):
        self.loop = None
        self.clients = set()
        self.enabled = AUDIO_ENABLED
        self.device = resolve_audio_device() if self.enabled else None
        self.proc = None
        self._thread = None
        self._stop_flag = threading.Event()

    def status(self):
        running = False
        if self._thread is not None:
            running = self._thread.is_alive()
        return {
            "enabled": self.enabled,
            "device": self.device,
            "running": running,
            "clients": len(self.clients),
        }

    async def add_client(self, ws):
        if not self.enabled:
            return
        self.loop = asyncio.get_running_loop()
        self.clients.add(ws)
        if self._thread is None or not self._thread.is_alive():
            self._start()

    async def remove_client(self, ws):
        self.clients.discard(ws)
        if not self.clients:
            self._stop()

    def _start(self):
        if not self.device:
            LOG.warning("audio enabled but no capture device found")
            return
        self._stop_flag.clear()
        target = self._tone_loop if self.device == "test" else self._arecord_loop
        self._thread = threading.Thread(target=target, daemon=True)
        self._thread.start()

    def _stop(self):
        self._stop_flag.set()
        if self.proc:
            self.proc.terminate()
            self.proc = None

    def _arecord_loop(self):
        cmd = [
            "arecord", "-D", self.device,
            "-f", "S16_LE", "-r", str(AUDIO_RATE), "-c", str(AUDIO_CHANNELS),
            "-t", "raw", "-q",
        ]
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("failed to start arecord: %s", exc)
            return
        chunk_size = int(AUDIO_RATE * AUDIO_CHANNELS * 2 * 0.02)  # ~20ms of audio
        while not self._stop_flag.is_set():
            data = self.proc.stdout.read(chunk_size)
            if not data:
                break
            if self.loop:
                asyncio.run_coroutine_threadsafe(self._broadcast(data), self.loop)
        if self.proc:
            self.proc.terminate()
            self.proc = None

    def _tone_loop(self):
        # Synthetic 440Hz tone so the audio path can be exercised without hardware.
        freq = 440.0
        chunk_ms = 20
        samples_per_chunk = int(AUDIO_RATE * chunk_ms / 1000)
        sample_index = 0
        while not self._stop_flag.is_set():
            buf = bytearray()
            for _ in range(samples_per_chunk):
                value = int(3000 * math.sin(2 * math.pi * freq * sample_index / AUDIO_RATE))
                buf += struct.pack("<hh", value, value)
                sample_index += 1
            if self.loop:
                asyncio.run_coroutine_threadsafe(self._broadcast(bytes(buf)), self.loop)
            time.sleep(chunk_ms / 1000)

    async def _broadcast(self, data: bytes):
        for ws in list(self.clients):
            try:
                await ws.send_bytes(data)
            except Exception:  # noqa: BLE001
                self.clients.discard(ws)


async def ws_serial_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    bridge = request.app["serial_bridge"]
    await bridge.add_client(ws)
    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                bridge.write(msg.data)
            elif msg.type == WSMsgType.ERROR:
                break
    finally:
        await bridge.remove_client(ws)
    return ws


async def ws_audio_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    bridge = request.app["audio_bridge"]
    if not bridge.enabled:
        await ws.close(code=1000, message=b"audio disabled")
        return ws
    await bridge.add_client(ws)
    try:
        async for _msg in ws:
            pass  # audio only flows Pi -> browser; ignore anything inbound
    finally:
        await bridge.remove_client(ws)
    return ws


async def api_status(request):
    app = request.app
    return web.json_response({
        "serial": app["serial_bridge"].status(),
        "video": app["streamer"].status(),
        "audio": app["audio_bridge"].status(),
    })


async def api_video(request):
    try:
        body = await request.json()
        width = int(body["width"])
        height = int(body["height"])
    except Exception:  # noqa: BLE001
        return web.json_response({"error": "expected JSON {width, height}"}, status=400)
    request.app["streamer"].set_resolution(width, height)
    return web.json_response({"ok": True})


async def api_version(request):
    upd = request.app["updater"]
    upd.maybe_refresh()
    return web.json_response(upd.status())


async def api_update(request):
    upd = request.app["updater"]
    version = None
    if request.can_read_body:
        try:
            body = await request.json()
            version = body.get("version") if body else None
        except Exception:  # noqa: BLE001 - empty/non-JSON body means "use latest"
            version = None
    try:
        installed = await upd.perform_update(version)
    except Exception as exc:  # noqa: BLE001 - reported to the caller, not fatal
        return web.json_response({"error": str(exc)}, status=409)
    return web.json_response({"ok": True, "version": installed})


async def on_startup(app):
    app["streamer"].start()
    app["streamer_task"] = asyncio.create_task(app["streamer"].watchdog())
    app["updater_task"] = asyncio.create_task(app["updater"].watchdog())


async def on_cleanup(app):
    for key in ("streamer_task", "updater_task"):
        task = app.get(key)
        if task:
            task.cancel()
    app["streamer"].stop()
    app["serial_bridge"]._close()  # noqa: SLF001 - internal, shutdown path only
    app["audio_bridge"]._stop()  # noqa: SLF001


def create_app():
    app = web.Application()
    app["serial_bridge"] = SerialBridge()
    app["audio_bridge"] = AudioBridge()
    app["streamer"] = Streamer()
    app["updater"] = updater.Updater()

    app.router.add_get("/ws/serial", ws_serial_handler)
    app.router.add_get("/ws/audio", ws_audio_handler)
    app.router.add_get("/api/status", api_status)
    app.router.add_post("/api/video", api_video)
    app.router.add_get("/api/version", api_version)
    app.router.add_post("/api/update", api_update)

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main():
    LOG.info("audio %s", "enabled" if AUDIO_ENABLED else "disabled")
    web.run_app(create_app(), host=BRIDGE_HOST, port=BRIDGE_PORT, print=None)


if __name__ == "__main__":
    main()
