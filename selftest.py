#!/usr/bin/env python3
"""Prove the republish half works, without a camera.

The camera half of this relay cannot be exercised without a camera that publishes,
but everything after the media arrives can be: this generates a real H.264 stream,
pushes it through the same `RtspPublisher` the viewer feeds, and then reads it back
off MediaMTX over RTSP to check the codec, the resolution and that frames decode.

If this passes, the only unknown left is whether your cameras publish to their KVS
channel at all.

    docker compose up -d mediamtx
    ./selftest.py

Exit codes: 0 the round trip worked, 1 MediaMTX refused the publish or the stream did
not read back, 2 ffmpeg/ffprobe missing.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WIDTH, HEIGHT, FPS, SECONDS = 640, 360, 15, 8
RTSP_BASE = os.environ.get("INSTAVISION_RTSP_BASE", "rtsp://127.0.0.1:8574")


def load_relay():
    spec = importlib.util.spec_from_file_location("relay", os.path.join(HERE, "relay.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def probe(url: str, verbose: bool = True) -> dict | None:
    """Ask ffprobe what MediaMTX is serving on that path."""
    proc = subprocess.run(
        ["ffprobe", "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp",
         "-show_entries", "stream=codec_name,width,height,avg_frame_rate",
         "-of", "json", "-i", url],
        capture_output=True, text=True, timeout=45,
    )
    if proc.returncode != 0:
        if verbose:
            print(f"  ffprobe failed: {proc.stderr.strip()[:200]}")
        return None
    try:
        streams = json.loads(proc.stdout).get("streams") or []
    except json.JSONDecodeError:
        return None
    return streams[0] if streams else None


def decodes(url: str, frames: int = 20) -> bool:
    """Decode a few frames: proves the bytes are video, not just a valid SDP."""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp",
         "-i", url, "-frames:v", str(frames), "-f", "null", "-"],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        print(f"  decode failed: {proc.stderr.strip()[:200]}")
    return proc.returncode == 0


def main() -> int:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        print("ffmpeg/ffprobe not found on PATH")
        return 2

    relay = load_relay()
    publisher = relay.RtspPublisher("selftest", RTSP_BASE)
    url = publisher.url
    print(f"publishing to {url}")

    with tempfile.TemporaryDirectory() as tmp:
        clip = os.path.join(tmp, "selftest.h264")
        enc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", f"testsrc=size={WIDTH}x{HEIGHT}:rate={FPS}",
             "-t", str(SECONDS), "-c:v", "libx264", "-preset", "ultrafast",
             "-g", str(FPS), "-f", "h264", clip],
            capture_output=True, text=True,
        )
        if enc.returncode != 0:
            print(f"could not generate a test clip: {enc.stderr.strip()[:200]}")
            return 1
        data = open(clip, "rb").read()
        if not data:
            print("the generated clip is empty")
            return 1
    print(f"feeding {len(data) / 1024:.0f} KB of H.264")

    if not publisher.start():
        print(f"could not start ffmpeg: {publisher.last_error}")
        return 1

    # Feed in a loop, paced like a live camera. MediaMTX drops a publisher that
    # goes quiet for 10 s, so the feed has to outlive the probes - the first
    # version stopped feeding when the clip ended and the path vanished out from
    # under ffprobe. Pacing is deliberately brisk: ffmpeg has to see enough of the
    # stream to build the SDP, and at a leisurely 30 KB/s that takes >3 s, which
    # made the test look broken when it was only slow.
    stop_feed = threading.Event()
    step = max(1, len(data) // (SECONDS * 50))

    def feed() -> None:
        while not stop_feed.is_set():
            for i in range(0, len(data), step):
                if stop_feed.is_set():
                    return
                publisher.write(data[i:i + step])
                time.sleep(0.02)

    feeder = threading.Thread(target=feed, name="selftest-feed", daemon=True)
    feeder.start()

    ok = True
    try:
        # ffmpeg probes up to 5 s of the input before it will write the RTSP
        # header, so the path usually appears ~5 s after the feed starts. Poll
        # rather than guess.
        print("reading it back (ffmpeg needs ~5 s to probe the stream) ...")
        stream, deadline = None, time.time() + 25
        while stream is None and time.time() < deadline:
            stream = probe(url, verbose=False)
            if stream is None:
                time.sleep(1)
        if not stream:
            print(f"  nothing published at {url}")
            ok = False
        else:
            codec = stream.get("codec_name")
            width, height = stream.get("width"), stream.get("height")
            rate = stream.get("avg_frame_rate")
            print(f"  codec={codec} size={width}x{height} rate={rate}")
            if codec != "h264":
                print(f"  FAIL expected h264, got {codec}")
                ok = False
            if (width, height) != (WIDTH, HEIGHT):
                print(f"  FAIL expected {WIDTH}x{HEIGHT}, got {width}x{height}")
                ok = False
            if not decodes(url):
                ok = False
    finally:
        stop_feed.set()
        feeder.join(timeout=3)
        publisher.stop()

    if publisher.last_error:
        print(f"  ffmpeg reported: {publisher.last_error}")
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
