"""Checks for and installs NanoKVM-USB frontend releases into /data/www."""

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

LOG = logging.getLogger("nanokvm-pi-bridge.updater")

WWW_DIR = "/data/www"
VERSION_FILE = os.path.join(WWW_DIR, ".nanokvm-usb-version")
SHIM_SRC = "/app/nanokvm-pi-shim.js"

# Overridable to point at a mirror or a local stand-in for testing.
RELEASES_API_URL = os.environ.get(
    "UPDATE_RELEASES_API_URL",
    "https://api.github.com/repos/sipeed/NanoKVM-USB/releases/latest",
)
DOWNLOAD_URL_TEMPLATE = os.environ.get(
    "UPDATE_DOWNLOAD_URL_TEMPLATE",
    "https://github.com/sipeed/NanoKVM-USB/releases/download/"
    "v{version}/nanokvm-usb-browser-v{version}.zip",
)
REQUEST_HEADERS = {"User-Agent": "nanokvm-pi-bridge"}

CHECK_INTERVAL = int(os.environ.get("UPDATE_CHECK_INTERVAL", str(6 * 3600)))
CHECK_ENABLED = os.environ.get("UPDATE_CHECK", "on").strip().lower() not in (
    "0", "off", "false", "no",
)


def parse_version(version):
    """'1.1.10' -> (1, 1, 10), ignoring a leading 'v' and any suffix; None if not numeric."""
    if not version:
        return None
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", version)
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def read_installed_version():
    try:
        with open(VERSION_FILE) as f:
            return f.read().strip() or None
    except OSError:
        return None


class Updater:
    def __init__(self):
        self.latest = None
        self.latest_checked_at = None
        self.last_check_error = None
        self.updating = False
        self.last_update_error = None

    def status(self):
        installed = read_installed_version()
        installed_v = parse_version(installed)
        latest_v = parse_version(self.latest)
        update_available = bool(installed_v and latest_v and latest_v > installed_v)
        return {
            "installed": installed,
            "latest": self.latest,
            "latest_checked_at": self.latest_checked_at,
            "update_available": update_available,
            "updating": self.updating,
            "check_enabled": CHECK_ENABLED,
            "last_check_error": self.last_check_error,
            "last_update_error": self.last_update_error,
        }

    def maybe_refresh(self):
        """Starts a background check if the cache is stale; never blocks."""
        if not CHECK_ENABLED:
            return
        age = time.time() - self.latest_checked_at if self.latest_checked_at else None
        if age is None or age > CHECK_INTERVAL:
            asyncio.create_task(self.check_latest())

    async def check_latest(self):
        loop = asyncio.get_running_loop()
        try:
            latest = await loop.run_in_executor(None, self._fetch_latest_tag)
        except Exception as exc:  # noqa: BLE001 - network/parse errors, never fatal
            self.last_check_error = str(exc)
            LOG.warning("update check failed: %s", self.last_check_error)
            return
        self.latest = latest
        self.latest_checked_at = time.time()
        self.last_check_error = None

    def _fetch_latest_tag(self):
        req = urllib.request.Request(RELEASES_API_URL, headers=REQUEST_HEADERS)
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.load(resp)
        return str(body["tag_name"]).lstrip("v")

    async def watchdog(self):
        if not CHECK_ENABLED:
            LOG.info("update checking disabled (UPDATE_CHECK=off)")
            return
        while True:
            await self.check_latest()
            await asyncio.sleep(CHECK_INTERVAL)

    async def perform_update(self, version=None):
        if self.updating:
            raise RuntimeError("an update is already in progress")
        version = version or self.latest
        if not version:
            raise RuntimeError("no target version known yet - check for updates first")

        self.updating = True
        self.last_update_error = None
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._download_and_swap, version)
            return version
        except Exception as exc:  # noqa: BLE001 - reported to the caller below
            self.last_update_error = str(exc)
            raise
        finally:
            self.updating = False

    def _download_and_swap(self, version):
        url = DOWNLOAD_URL_TEMPLATE.format(version=version)
        LOG.info("downloading %s", url)

        os.makedirs("/data", exist_ok=True)
        with tempfile.TemporaryDirectory(dir="/data", prefix=".update-") as tmp:
            zip_path = os.path.join(tmp, "browser.zip")
            req = urllib.request.Request(url, headers=REQUEST_HEADERS)
            try:
                with urllib.request.urlopen(req, timeout=60) as resp, open(zip_path, "wb") as f:
                    shutil.copyfileobj(resp, f)
            except urllib.error.HTTPError as exc:
                raise RuntimeError(
                    f"download failed ({exc.code}) - is v{version} a real NanoKVM-USB release?"
                ) from exc

            extract_dir = os.path.join(tmp, "www-new")
            os.makedirs(extract_dir)
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(extract_dir)

            if not os.path.isfile(os.path.join(extract_dir, "index.html")):
                raise RuntimeError("downloaded archive doesn't look like the browser build (no index.html)")

            self._inject_shim(extract_dir)
            with open(os.path.join(extract_dir, ".nanokvm-usb-version"), "w") as f:
                f.write(version)

            # Swap in with two same-filesystem renames.
            staging = "/data/www.new"
            self._rmtree_if_exists(staging)
            shutil.move(extract_dir, staging)

            old = "/data/www.old"
            self._rmtree_if_exists(old)
            if os.path.isdir(WWW_DIR):
                os.rename(WWW_DIR, old)
            os.rename(staging, WWW_DIR)
            self._rmtree_if_exists(old)

        LOG.info("updated NanoKVM-USB frontend to v%s", version)

    @staticmethod
    def _rmtree_if_exists(path):
        if os.path.exists(path):
            shutil.rmtree(path)

    @staticmethod
    def _inject_shim(www_dir):
        shutil.copy(SHIM_SRC, os.path.join(www_dir, "nanokvm-pi-shim.js"))
        index_path = os.path.join(www_dir, "index.html")
        with open(index_path) as f:
            html = f.read()
        if "nanokvm-pi-shim.js" in html:
            return
        html = html.replace(
            "<head>", '<head>\n    <script src="/nanokvm-pi-shim.js"></script>', 1
        )
        with open(index_path, "w") as f:
            f.write(html)
