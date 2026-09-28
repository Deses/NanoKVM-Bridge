"""Installs and updates Sipeed's NanoKVM-USB browser build in /data/www.

Installs are built next to it and swapped in by rename, so a failure never touches the live copy.
"""

import asyncio
import glob
import http.client
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

LOG = logging.getLogger("nanokvm-bridge.updater")

BAKED_DIR = "/www-image"
DATA_DIR = "/data"
WWW_DIR = os.path.join(DATA_DIR, "www")
VERSION_FILE = ".nanokvm-usb-version"
SHIM_TAG = '<script src="/nanokvm-bridge-shim.js"></script>'

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
REQUEST_HEADERS = {"User-Agent": "nanokvm-bridge"}

CHECK_ENABLED = os.environ.get("UPDATE_CHECK", "on").strip().lower() not in ("0", "off", "false", "no")
CHECK_INTERVAL = max(60, int(os.environ.get("UPDATE_CHECK_INTERVAL", str(6 * 3600))))
RETRY_INTERVAL = min(CHECK_INTERVAL, 600)  # e.g. no network yet at boot

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


class UpdateError(Exception):
    pass


def parse_version(version):
    if not version or not VERSION_RE.match(version):
        return None
    return tuple(int(part) for part in version.split("."))


def read_version(directory):
    try:
        with open(os.path.join(directory, VERSION_FILE), encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def installed_version():
    return read_version(WWW_DIR)


def install_baked():
    """Install the image's build unless the volume already has it or newer."""
    os.makedirs(DATA_DIR, exist_ok=True)
    _clean_up_interrupted_install()

    baked = read_version(BAKED_DIR)
    installed = parse_version(installed_version())
    if _loads_shim(os.path.join(WWW_DIR, "index.html")) and installed and installed >= parse_version(baked):
        return

    LOG.info("installing NanoKVM-USB frontend v%s", baked)
    with tempfile.TemporaryDirectory(dir=DATA_DIR, prefix=".update-") as tmp:
        new_dir = os.path.join(tmp, "www")
        shutil.copytree(BAKED_DIR, new_dir)
        _prepare(new_dir, baked)
        _swap_in(new_dir)


def _loads_shim(index_path):
    try:
        with open(index_path, encoding="utf-8") as f:
            return SHIM_TAG in f.read()
    except OSError:
        return False


def _clean_up_interrupted_install():
    for leftover in glob.glob(os.path.join(DATA_DIR, ".update-*")):
        shutil.rmtree(leftover, ignore_errors=True)
    old_dir = WWW_DIR + ".old"
    if not os.path.isfile(os.path.join(WWW_DIR, "index.html")) and os.path.isfile(os.path.join(old_dir, "index.html")):
        shutil.rmtree(WWW_DIR, ignore_errors=True)
        os.rename(old_dir, WWW_DIR)
    shutil.rmtree(old_dir, ignore_errors=True)


def _prepare(new_dir, version):
    index_path = os.path.join(new_dir, "index.html")
    if not os.path.isfile(index_path):
        raise UpdateError("no index.html - not the NanoKVM-USB browser build?")
    with open(index_path, encoding="utf-8") as f:
        html = f.read()
    if SHIM_TAG not in html:
        if "<head>" not in html:
            raise UpdateError("unexpected index.html: no <head> to load the shim from")
        with open(index_path, "w", encoding="utf-8") as f:
            f.write(html.replace("<head>", "<head>\n    " + SHIM_TAG, 1))
    with open(os.path.join(new_dir, VERSION_FILE), "w", encoding="utf-8") as f:
        f.write(version)


def _swap_in(new_dir):
    old_dir = WWW_DIR + ".old"
    shutil.rmtree(old_dir, ignore_errors=True)
    if os.path.isdir(WWW_DIR):
        os.rename(WWW_DIR, old_dir)
    os.rename(new_dir, WWW_DIR)
    shutil.rmtree(old_dir, ignore_errors=True)


class Updater:
    def __init__(self):
        self.latest = None
        self.latest_checked_at = None
        self.last_check_error = None
        self.updating = False
        self.last_update_error = None

    def status(self):
        installed = installed_version()
        installed_v, latest_v = parse_version(installed), parse_version(self.latest)
        return {
            "installed": installed,
            "latest": self.latest,
            "latest_checked_at": self.latest_checked_at,
            "update_available": bool(installed_v and latest_v and latest_v > installed_v),
            "updating": self.updating,
            "check_enabled": CHECK_ENABLED,
            "last_check_error": self.last_check_error,
            "last_update_error": self.last_update_error,
        }

    async def run(self):
        if not CHECK_ENABLED:
            LOG.info("update checks disabled")
            return
        while True:
            ok = await self.check_latest()
            await asyncio.sleep(CHECK_INTERVAL if ok else RETRY_INTERVAL)

    async def check_latest(self):
        try:
            latest = await asyncio.to_thread(self._fetch_latest_version)
        except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError) as exc:
            self.last_check_error = str(exc)
            LOG.warning("update check failed: %s", exc)
            return False
        self.latest = latest
        self.latest_checked_at = time.time()
        self.last_check_error = None
        return True

    def _fetch_latest_version(self):
        req = urllib.request.Request(RELEASES_API_URL, headers=REQUEST_HEADERS)
        with urllib.request.urlopen(req, timeout=10) as resp:
            tag = str(json.load(resp)["tag_name"])
        version = tag.removeprefix("v")
        if not parse_version(version):
            raise ValueError(f"unrecognized release tag {tag!r}")
        return version

    async def perform_update(self, version=None):
        if self.updating:
            raise UpdateError("an update is already in progress")
        version = version or self.latest
        if not version:
            raise UpdateError("no release known yet - the update check hasn't succeeded")
        if not parse_version(version):
            raise UpdateError(f"invalid version {version!r}, expected x.y.z")

        self.updating = True
        self.last_update_error = None
        try:
            await asyncio.to_thread(self._download_and_install, version)
        except UpdateError as exc:
            self.last_update_error = str(exc)
            raise
        except (OSError, http.client.HTTPException, zipfile.BadZipFile) as exc:
            self.last_update_error = f"update to v{version} failed: {exc}"
            raise UpdateError(self.last_update_error) from exc
        finally:
            self.updating = False
        LOG.info("installed NanoKVM-USB frontend v%s", version)
        return version

    def _download_and_install(self, version):
        url = DOWNLOAD_URL_TEMPLATE.format(version=version)
        LOG.info("downloading %s", url)
        with tempfile.TemporaryDirectory(dir=DATA_DIR, prefix=".update-") as tmp:
            zip_path = os.path.join(tmp, "browser.zip")
            req = urllib.request.Request(url, headers=REQUEST_HEADERS)
            try:
                with urllib.request.urlopen(req, timeout=60) as resp, open(zip_path, "wb") as f:
                    shutil.copyfileobj(resp, f)
            except urllib.error.HTTPError as exc:
                raise UpdateError(f"download failed (HTTP {exc.code}) - is v{version} a release?") from exc
            new_dir = os.path.join(tmp, "www")
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(new_dir)
            _prepare(new_dir, version)
            _swap_in(new_dir)
