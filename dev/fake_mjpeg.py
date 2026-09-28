#!/usr/bin/env python3
"""Stand-in for ustreamer: a synthetic MJPEG stream. Run in a VIDEO_DEVICE=none container; needs Pillow."""

import argparse
import asyncio
import io
import time

from aiohttp import web
from PIL import Image, ImageDraw

BOUNDARY = "boundarydonotcross"


def make_frame(width, height, counter):
    img = Image.new("RGB", (width, height), (20, 20, 20))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, width - 1, height - 1], outline=(80, 200, 120), width=4)
    draw.text(
        (20, 20),
        f"NanoKVM-Bridge fake frame #{counter}\n{time.strftime('%H:%M:%S')}",
        fill=(230, 230, 230),
    )
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


async def stream_handler(request):
    width = request.app["width"]
    height = request.app["height"]
    fps = request.app["fps"]

    response = web.StreamResponse(
        status=200,
        headers={"Content-Type": f"multipart/x-mixed-replace; boundary={BOUNDARY}"},
    )
    await response.prepare(request)

    counter = 0
    try:
        while True:
            frame = make_frame(width, height, counter)
            counter += 1
            headers = (
                f"--{BOUNDARY}\r\n"
                f"Content-Type: image/jpeg\r\n"
                f"Content-Length: {len(frame)}\r\n\r\n"
            ).encode("ascii")
            await response.write(headers + frame + b"\r\n")
            await asyncio.sleep(1.0 / fps)
    except ConnectionResetError:
        pass
    return response


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=float, default=10.0)
    args = parser.parse_args()

    app = web.Application()
    app["width"] = args.width
    app["height"] = args.height
    app["fps"] = args.fps
    app.router.add_get("/stream", stream_handler)

    print(f"[fake_mjpeg] serving {args.width}x{args.height} @ {args.fps}fps on http://{args.host}:{args.port}/stream")
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
