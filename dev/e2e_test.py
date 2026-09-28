#!/usr/bin/env python3
"""End-to-end checks: the real app in headless Chromium against fake hardware (see run_e2e.sh)."""

import argparse
import os
import sys
import time
import urllib.error
import urllib.request

import serial
from playwright.sync_api import sync_playwright

# CH9329 frames the app sends: key 'a' down, then all keys up.
KEY_A_DOWN = bytes.fromhex("57ab000208000004000000000010")
KEYS_UP = bytes.fromhex("57ab00020800000000000000000c")

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


def split_frames(data):
    frames, i = [], 0
    while i + 5 <= len(data):
        if data[i:i + 2] != b"\x57\xab":
            i += 1
            continue
        end = i + 5 + data[i + 4] + 1
        frames.append(data[i:end])
        i = end
    return frames


def checksum_ok(frame):
    return len(frame) >= 6 and sum(frame[:-1]) & 0xFF == frame[-1]


def read_all(port, settle=0.5):
    time.sleep(settle)
    return port.read(port.in_waiting or 1)


def http_get(url):
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.status, resp.headers, resp.read()


def wait_until(predicate, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://127.0.0.1:47812")
    parser.add_argument("--pty", required=True, help="host end of the pty pair given to the container")
    parser.add_argument("--chromium", default=os.environ.get("CHROMIUM_PATH"), help="Chromium binary (default: Playwright's)")
    parser.add_argument("--update", action="store_true", help="also test the update banner (needs fake_github.py)")
    args = parser.parse_args()
    url = args.url.rstrip("/")

    # --- HTTP surface
    status, _, body = http_get(url + "/")
    check("index served with shim tag", status == 200 and b"/nanokvm-bridge-shim.js" in body)
    _, headers, _ = http_get(url + "/nanokvm-bridge-shim.js")
    check("shim sent no-cache", headers.get("Cache-Control") == "no-cache")
    try:
        http_get(url + "/.nanokvm-usb-version")
        check("dotfiles hidden", False)
    except urllib.error.HTTPError as exc:
        check("dotfiles hidden", exc.code == 404)

    dongle = serial.Serial(args.pty, 57600, timeout=1)
    # Desktop Chrome's autoplay policy; headless is more permissive by default.
    launch = {"args": ["--autoplay-policy=document-user-activation-required"]}
    if args.chromium:
        launch["executable_path"] = args.chromium
    with sync_playwright() as p:
        browser = p.chromium.launch(**launch)

        # --- the shim connects video and serial by itself, no clicks
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.goto(url)  # not "networkidle": the video stream never goes idle
        serial_open = lambda: page.evaluate("() => fetch('/api/status').then(r => r.json()).then(s => s.serial.connected)")
        connected = wait_until(lambda: serial_open() and not page.locator(".ant-modal").is_visible())
        check("connects automatically (no clicks)", connected)
        wait_until(lambda: page.eval_on_selector("#video", "el => el.videoWidth") > 0, timeout=3)
        width = page.eval_on_selector("#video", "el => el.videoWidth")
        check("video renders", width > 0, f"videoWidth={width}")
        t0 = page.eval_on_selector("#video", "el => el.currentTime")
        time.sleep(1)
        playing = page.eval_on_selector("#video", "el => !el.paused && el.currentTime")
        check("video plays without a click (not frozen)", bool(playing) and playing > t0)

        # --- keyboard
        dongle.reset_input_buffer()
        page.keyboard.press("a")
        frames = split_frames(read_all(dongle))
        check("keyboard: 'a' down + keys up", frames[:2] == [KEY_A_DOWN, KEYS_UP], " ".join(f.hex() for f in frames))

        # --- mouse
        dongle.reset_input_buffer()
        box = page.locator("#video").bounding_box()
        page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        frames = [f for f in split_frames(read_all(dongle)) if f[3] == 0x04]
        buttons = [f[6] for f in frames]
        check("mouse: absolute frames with valid checksums", frames and all(map(checksum_ok, frames)))
        check("mouse: left button pressed then released", 1 in buttons and buttons[-1] == 0, f"buttons={buttons}")

        page.close()

        # --- auto-connect can be turned off
        page = browser.new_page()
        page.goto(url + "/?autoconnect=0", wait_until="networkidle")
        time.sleep(2)
        check("?autoconnect=0 leaves the device dialog up", page.locator(".ant-modal").is_visible())

        # --- serial semantics match Web Serial (on that page, so auto-connect doesn't race for the port)
        semantics = page.evaluate("""async () => {
            let fired = 0;
            navigator.serial.addEventListener('disconnect', () => fired++);
            const port = await navigator.serial.requestPort();
            await port.open({baudRate: 57600});
            await port.close();
            await port.open({baudRate: 57600});
            let doubleOpen = null;
            try { await port.open({baudRate: 57600}); } catch (e) { doubleOpen = e.name; }
            await new Promise(r => setTimeout(r, 1000));
            await port.close();
            return {fired, doubleOpen};
        }""")
        page.close()
        check("close/reopen fires no disconnect", semantics["fired"] == 0)
        check("opening an open port throws InvalidStateError", semantics["doubleOpen"] == "InvalidStateError")

        # --- update banner
        if args.update:
            page = browser.new_page()
            page.goto(url + "/?autoconnect=0", wait_until="networkidle")
            time.sleep(2.5)
            banner = page.locator("text=update available")
            check("update banner shown", banner.is_visible())
            with page.expect_response(lambda r: r.url.endswith("/api/update"), timeout=30000) as resp:
                page.get_by_role("button", name="Update now").click()
            check("update installs", resp.value.status == 200, str(resp.value.json()))
            page.wait_for_load_state("networkidle")
            time.sleep(2.5)
            version = page.evaluate("() => fetch('/api/version').then(r => r.json())")
            check("banner gone after reload", not page.locator("text=update available").is_visible())
            check("new version installed", version["installed"] == version["latest"], str(version))

        browser.close()

    print(f"\n{results.count(True)}/{len(results)} passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
