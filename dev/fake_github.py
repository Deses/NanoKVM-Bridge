#!/usr/bin/env python3
"""Stand-in for GitHub's release API and downloads; every version serves a zip of --site-dir."""

import argparse
import io
import os
import zipfile

from aiohttp import web


def zip_directory(path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(path):
            for name in files:
                full = os.path.join(root, name)
                zf.write(full, os.path.relpath(full, path))
    return buf.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9998)
    parser.add_argument("--latest", default="9.9.9", help="version reported as the latest release")
    parser.add_argument("--site-dir", required=True, help="directory to serve as the browser build zip")
    args = parser.parse_args()

    archive = zip_directory(args.site_dir)

    async def latest(request):
        return web.json_response({"tag_name": f"v{args.latest}"})

    async def download(request):
        version = request.match_info["version"]
        if request.match_info["name"] != f"nanokvm-usb-browser-v{version}.zip":
            raise web.HTTPNotFound()
        return web.Response(body=archive, content_type="application/zip")

    app = web.Application()
    app.router.add_get("/releases/latest", latest)
    app.router.add_get("/download/v{version}/{name}", download)
    print(f"[fake_github] latest=v{args.latest} on http://{args.host}:{args.port}", flush=True)
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
