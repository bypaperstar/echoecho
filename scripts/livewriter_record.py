#!/usr/bin/env python3
"""Record a Live Writer session as video: launch Chrome with a WAV piped in
as the (fake) microphone, capture the tab via CDP screencast frames, and
assemble frames + the spoken audio into an .mp4 with ffmpeg.

No TCC screen-recording permission is needed (CDP renders the tab), no
puppeteer/npm — just Chrome, ffmpeg, and the websockets package already in
the repo venv.

  python3 scripts/livewriter_record.py \
      --url "http://127.0.0.1:8799/?autostart=1&notype=1" \
      --wav livewriter-results/<...>/input.wav \
      --out demo.mp4 --duration 70 [--headless] [--audio-offset 1.2]
"""

import argparse
import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request


def find_chrome():
    for c in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
              "google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
        p = c if c.startswith("/") and os.path.exists(c) else shutil.which(c)
        if p:
            return p
    raise RuntimeError("no chrome found")


def find_ffmpeg():
    for c in ("ffmpeg", "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        p = shutil.which(c) or (c if c.startswith("/") and os.path.exists(c) else None)
        if p:
            return p
    raise RuntimeError("no ffmpeg found")


async def record(args):
    import websockets
    dbg_port = args.debug_port
    profile = tempfile.mkdtemp(prefix="lw-rec-")
    chrome_args = [
        find_chrome(),
        "--remote-debugging-port=%d" % dbg_port,
        "--remote-allow-origins=*",
        "--user-data-dir=%s" % profile,
        "--no-first-run", "--no-default-browser-check",
        "--use-fake-device-for-media-stream",
        "--use-fake-ui-for-media-stream",
        "--use-file-for-fake-audio-capture=%s%%noloop" % os.path.abspath(args.wav),
        "--autoplay-policy=no-user-gesture-required",
        "--window-size=%d,%d" % (args.width, args.height + 88),
        "--mute-audio",
    ]
    if args.headless:
        chrome_args += ["--headless=new", "--disable-gpu"]
    chrome_args.append(args.url)
    chrome = subprocess.Popen(chrome_args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # find the page target
    ws_url = None
    for _ in range(60):
        try:
            data = json.load(urllib.request.urlopen("http://127.0.0.1:%d/json" % dbg_port, timeout=2))
            for t in data:
                if t.get("type") == "page" and "127.0.0.1" in t.get("url", ""):
                    ws_url = t["webSocketDebuggerUrl"]
                    break
            if ws_url:
                break
        except Exception:
            pass
        time.sleep(0.5)
    if not ws_url:
        chrome.terminate()
        raise RuntimeError("no debuggable page found")

    frames_dir = tempfile.mkdtemp(prefix="lw-frames-")
    frames = []  # (path, cdp_timestamp)
    mid = [0]

    async with websockets.connect(ws_url, max_size=None) as ws:
        async def send(method, params=None):
            mid[0] += 1
            await ws.send(json.dumps({"id": mid[0], "method": method, "params": params or {}}))
            return mid[0]

        await send("Page.enable")
        await send("Page.startScreencast", {
            "format": "jpeg", "quality": 82,
            "maxWidth": args.width, "maxHeight": args.height + 88,
            "everyNthFrame": 1,
        })
        t_end = time.monotonic() + args.duration
        n = 0
        while time.monotonic() < t_end:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, t_end - time.monotonic()))
            except asyncio.TimeoutError:
                continue
            msg = json.loads(raw)
            if msg.get("method") == "Page.screencastFrame":
                p = msg["params"]
                n += 1
                path = os.path.join(frames_dir, "f%06d.jpg" % n)
                with open(path, "wb") as f:
                    f.write(base64.b64decode(p["data"]))
                frames.append((path, p["metadata"].get("timestamp", time.time())))
                await send("Page.screencastFrameAck", {"sessionId": p["sessionId"]})
        await send("Page.stopScreencast")

    chrome.terminate()
    try:
        chrome.wait(5)
    except subprocess.TimeoutExpired:
        chrome.kill()

    if len(frames) < 2:
        raise RuntimeError("captured %d frames — nothing to assemble" % len(frames))

    # concat demuxer with real per-frame durations (screencast is variable-rate)
    concat = os.path.join(frames_dir, "list.txt")
    with open(concat, "w") as f:
        for i, (path, ts) in enumerate(frames):
            f.write("file '%s'\n" % path)
            dur = (frames[i + 1][1] - ts) if i + 1 < len(frames) else 0.5
            f.write("duration %.4f\n" % max(0.01, min(2.0, dur)))
        f.write("file '%s'\n" % frames[-1][0])

    ffmpeg = find_ffmpeg()
    cmd = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", concat,
           "-itsoffset", str(args.audio_offset), "-i", args.wav,
           "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",  # libx264 needs even dims
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30",
           "-c:a", "aac", "-shortest", args.out]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    shutil.rmtree(frames_dir, ignore_errors=True)
    shutil.rmtree(profile, ignore_errors=True)
    print("recorded %d frames -> %s" % (len(frames), args.out))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--wav", required=True, help="audio piped in as the mic AND muxed into the video")
    ap.add_argument("--out", required=True)
    ap.add_argument("--duration", type=float, default=60)
    ap.add_argument("--width", type=int, default=1360)
    ap.add_argument("--height", type=int, default=850)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--debug-port", type=int, default=9333)
    ap.add_argument("--audio-offset", type=float, default=1.2,
                    help="seconds of video before the mic audio starts")
    args = ap.parse_args()
    asyncio.run(record(args))


if __name__ == "__main__":
    sys.exit(main())
