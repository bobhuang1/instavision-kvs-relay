#!/usr/bin/env python3
"""InstaVision -> RTSP relay for Frigate.

The InstaVision cameras are WebRTC viewers' endpoints on AWS Kinesis Video
Streams channels; they expose nothing local (every port closed, same OUI, no
RTSP/ONVIF), so - like the OSAIO group - the only way into Frigate is to act as
a viewer and republish.

What this does, per enrolled camera:

    1. refresh the vendor session (Firebase password -> atlas/users/session)
    2. GET atlas/spaces/{space}/stream-credentials?device_id=...  -> AWS creds,
       channel ARN, WSS endpoint and TURN servers (they expire in ~5 minutes,
       so they are fetched per attempt rather than cached)
    3. join the channel as a KVS WebRTC viewer (see kvs_viewer.py)
    4. copy the H.264 access units straight out of aiortc's jitter buffer into
       ffmpeg, which republishes them to MediaMTX over RTSP with `-c copy`

Step 4 is the important design choice: nothing is decoded or re-encoded, so a
camera costs a few percent of one core instead of most of a core per stream.
That matters on a four-core box already running Frigate, Home Assistant, Ollama,
SQL Server and Postgres.

    ./relay.py --list                 # show the account's cameras and exit
    ./relay.py --once                 # one attempt per camera, then exit
    ./relay.py                        # run until stopped (what the container does)
    ./relay.py --device Cam3 --once    # probe a single camera end to end

Exit codes: 0 finished cleanly, 1 login/config failure, 3 misconfigured .env.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import importlib.util
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.environ.get("INSTAVISION_STATE_DIR", os.path.join(HERE, "state"))

# How long one viewer session runs before it is rebuilt with fresh credentials.
# The AWS credentials last ~5 minutes but an established WebRTC session does not
# need them again; an hourly rebuild keeps the credential path exercised without
# churning the camera every few minutes the way the Tuya relay had to.
SESSION_SECONDS = int(os.environ.get("INSTAVISION_SESSION_SECONDS", "3600"))

# If no camera answers the offer within this, it is not publishing. Bailing out
# quickly is what lets a camera that comes online be picked up in seconds
# instead of after a full session timeout.
ANSWER_TIMEOUT = int(os.environ.get("INSTAVISION_ANSWER_TIMEOUT", "25"))

# Backoff between attempts, reset after a session that actually delivered frames.
BACKOFF_MIN = 5
BACKOFF_MAX = 60

# A session that never produced a frame is a failure even if the offer was
# answered, so it gets the same backoff treatment.
MIN_FRAMES_FOR_SUCCESS = 10


def load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def log(name: str, message: str) -> None:
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{stamp} [{name}] {message}", flush=True)


def slugify(name: str) -> str:
    """RTSP path component: MediaMTX paths are plain strings, keep them boring."""
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return f"insta_{slug}"


class RtspPublisher:
    """Publish an Annex-B H.264 byte stream to MediaMTX over RTSP.

    ffmpeg does the RTSP handshake and RTP packetisation with `-c copy`, so this
    class only has to keep its stdin fed. Writes happen on a dedicated thread:
    ffmpeg can stall (a MediaMTX restart closes the socket), and doing that on the
    event loop would freeze every camera's signalling at once.
    """

    def __init__(self, name: str, rtsp_base: str, ffmpeg: str = "ffmpeg"):
        self.name = name
        self.url = f"{rtsp_base.rstrip('/')}/{slugify(name)}"
        self.ffmpeg = ffmpeg
        self.proc: subprocess.Popen | None = None
        self.queue: queue.Queue[bytes | None] = queue.Queue(maxsize=400)
        self.thread: threading.Thread | None = None
        self.dropped = 0
        self.written = 0
        self.last_error = ""

    def start(self) -> bool:
        cmd = [
            self.ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostdin",
            "-fflags", "+genpts", "-f", "h264", "-i", "pipe:0",
            "-c", "copy", "-an",
            "-f", "rtsp", "-rtsp_transport", "tcp", self.url,
        ]
        try:
            self.proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
        except OSError as exc:
            self.last_error = f"cannot start ffmpeg: {exc}"
            return False
        self.thread = threading.Thread(target=self._pump, name=f"pub-{self.name}",
                                       daemon=True)
        self.thread.start()
        log(self.name, f"publishing to {self.url}")
        return True

    def _pump(self) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        while True:
            item = self.queue.get()
            if item is None:
                break
            try:
                self.proc.stdin.write(item)
                self.proc.stdin.flush()
                self.written += 1
            except (BrokenPipeError, ValueError, OSError) as exc:
                self.last_error = f"ffmpeg stdin closed: {exc}"
                break
        try:
            self.proc.stdin.close()
        except OSError:
            pass

    def write(self, annexb: bytes) -> None:
        """Called from the viewer for every complete access unit."""
        if self.proc is None or self.proc.poll() is not None:
            if not self.last_error and self.proc is not None:
                self.last_error = f"ffmpeg exited (code {self.proc.returncode})"
            return
        try:
            self.queue.put_nowait(annexb)
        except queue.Full:
            self.dropped += 1

    def stop(self) -> None:
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            pass
        if self.proc is not None:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            stderr = b""
            try:
                if self.proc.stderr is not None:
                    stderr = self.proc.stderr.read() or b""
            except OSError:
                pass
            if stderr and not self.last_error:
                self.last_error = stderr.decode(errors="replace").strip()[:300]
        self.proc = None

    def healthy(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


class CameraWorker:
    """Keeps one camera streaming, with backoff, until the relay stops."""

    def __init__(self, relay: "Relay", name: str, device_id: str):
        self.relay = relay
        self.name = name
        self.device_id = device_id
        self.state = "starting"
        self.frames = 0
        self.restarts = 0
        self.last_frame_age: float | None = None
        self.last_error = ""
        self.started_at: float | None = None
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> dict:
        return {
            "name": self.name,
            "device_id": self.device_id,
            "state": self.state,
            "path": slugify(self.name),
            "frames": self.frames,
            "restarts": self.restarts,
            "last_frame_age_s": None if self.last_frame_age is None
            else round(time.time() - self.last_frame_age, 1),
            "last_error": self.last_error,
        }

    async def run(self, once: bool = False) -> None:
        backoff = BACKOFF_MIN
        while not self._stop.is_set():
            started = time.time()
            self.state = "connecting"
            self.last_error = ""
            delivered = await self._session()
            if self._stop.is_set() or once:
                break
            if delivered:
                backoff = BACKOFF_MIN
                self.state = "idle"
            else:
                self.state = "waiting"
                self.restarts += 1
                log(self.name, f"retrying in {backoff}s ({self.last_error or 'no frames'})")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, BACKOFF_MAX)
            _ = started
        self.state = "stopped"

    async def _session(self) -> bool:
        """One credential fetch + viewer session. Returns True if frames flowed."""
        probe = self.relay.probe
        try:
            creds = await asyncio.to_thread(
                probe.request, "GET",
                f"atlas/spaces/{self.relay.space_id}/stream-credentials"
                f"?device_id={self.device_id}",
            )
        except Exception as exc:
            self.last_error = f"credentials: {type(exc).__name__}: {exc}"
            return False
        if not isinstance(creds, dict):
            self.last_error = "credentials rejected"
            return False
        channel = next(
            (c for c in creds.get("channel_info") or []
             if c.get("device_id") == self.device_id),
            None,
        )
        if channel is None:
            self.last_error = "no channel for this device"
            return False

        viewer = self.relay.viewer_module.KvsViewer(
            creds, channel, f"relay-{slugify(self.name)}",
            debug=self.relay.verbose,
        )
        publisher = RtspPublisher(self.name, self.relay.rtsp_base,
                                  self.relay.ffmpeg)
        if not publisher.start():
            self.last_error = publisher.last_error
            return False

        viewer.encoded_sink = publisher.write
        self.started_at = time.time()
        self.state = "streaming"

        try:
            await viewer.run(SESSION_SECONDS, answer_timeout=ANSWER_TIMEOUT)
        finally:
            publisher.stop()
            self.frames += viewer.forwarded
            if viewer.forwarded:
                self.last_frame_age = time.time()
            if viewer.error:
                self.last_error = viewer.error
            elif publisher.last_error:
                self.last_error = publisher.last_error
            elif not viewer.answer_sdp:
                self.last_error = "no answer from camera"
        log(self.name, f"session ended after {time.time() - self.started_at:.0f}s: "
                       f"frames={viewer.forwarded} bytes_out={publisher.written} "
                       f"dropped={publisher.dropped}"
                       + (f" err={self.last_error}" if self.last_error else ""))
        return viewer.forwarded >= MIN_FRAMES_FOR_SUCCESS


class Relay:
    def __init__(self, enrolled: list[str], rtsp_base: str, verbose: bool = False):
        self.enrolled = enrolled
        self.rtsp_base = rtsp_base
        self.verbose = verbose
        self.ffmpeg = os.environ.get("FFMPEG_PATH", "ffmpeg")
        self.probe = load_module("probe", "probe.py")
        self.viewer_module = load_module("kvs_viewer", "kvs_viewer.py")
        self.space_id = ""
        self.workers: list[CameraWorker] = []
        self.logged_in_at = 0.0
        self.idle = False
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()
        for worker in self.workers:
            worker.stop()

    async def login(self) -> bool:
        token = await asyncio.to_thread(self.probe.firebase_login)
        if not token:
            return False
        sess = await asyncio.to_thread(
            self.probe.request, "POST", "atlas/users/session", {"id_token": token}
        )
        if sess is None:
            return False
        spaces = await asyncio.to_thread(self.probe.request, "GET", "atlas/spaces")
        items = spaces.get("items") if isinstance(spaces, dict) else spaces
        if not items:
            log("relay", "no spaces on this account")
            return False
        self.space_id = items[0]["id"]
        self.logged_in_at = time.time()
        return True

    async def devices(self) -> list[dict]:
        devices = await asyncio.to_thread(
            self.probe.request, "GET", f"atlas/spaces/{self.space_id}/devices"
        )
        items = devices.get("items") if isinstance(devices, dict) else devices
        return items or []

    async def run(self, once: bool = False) -> int:
        # Nothing enrolled: stay up and serve status, but do not log in. Exiting
        # instead would make `restart: always` re-run the vendor login every few
        # seconds for no reason, which is exactly what gets an account throttled.
        if not self.enrolled:
            self.idle = True
            log("relay", "INSTAVISION_ENROLLED is empty - idle, publishing nothing")
            self.status_server_start()
            await self._stop.wait()
            return 0

        if not await self.login():
            return 1
        devices = await self.devices()
        if not devices:
            log("relay", "account has no devices")
            return 1

        wanted = {name.lower() for name in self.enrolled}
        selected = [
            d for d in devices
            if (d.get("name") or "").lower() in wanted
        ]
        missing = wanted - {(d.get("name") or "").lower() for d in devices}
        if missing:
            log("relay", f"enrolled but not on the account: {', '.join(sorted(missing))}")
        if not selected:
            self.idle = True
            log("relay", "no enrolled camera matched a device - idle")
            self.status_server_start()
            await self._stop.wait()
            return 0

        self.status_server_start()
        log("relay", f"publishing {len(selected)} camera(s): "
                     f"{', '.join(d['name'] for d in selected)}")
        self.workers = [
            CameraWorker(self, d["name"], d["id"]) for d in selected
        ]
        await asyncio.gather(*(w.run(once=once) for w in self.workers))
        log("relay", "stopped")
        return 0

    # --- health -----------------------------------------------------------

    def status(self) -> dict:
        workers = [w.snapshot() for w in self.workers]
        streaming = [w for w in workers if w["state"] == "streaming"
                     and (w["last_frame_age_s"] or 0) < 15]
        return {
            # Idle (nothing enrolled) is healthy by definition: there is nothing
            # that ought to be streaming. A watchdog should not alert on it.
            "ok": len(streaming) == len(workers),
            "idle": self.idle,
            "space_id": self.space_id,
            "logged_in_age_s": round(time.time() - self.logged_in_at, 1)
            if self.logged_in_at else None,
            "expected": len(workers),
            "streaming": len(streaming),
            "cameras": workers,
        }

    def status_server_start(self) -> None:
        """Tiny JSON status listener, so a watchdog does not have to guess."""
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps(relay.status(), indent=1).encode()
                code = 200 if relay.status()["ok"] else 503
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        # 0.0.0.0 is deliberate: the watchdog runs on the host, not in the
        # container, and this exposes only camera names and frame counters.
        bind = os.environ.get("INSTAVISION_HTTP_BIND", "0.0.0.0")
        port = int(os.environ.get("INSTAVISION_HTTP_PORT", "8655"))
        try:
            server = ThreadingHTTPServer((bind, port), Handler)
        except OSError as exc:
            log("relay", f"status port {port} unavailable: {exc}")
            return
        thread = threading.Thread(target=server.serve_forever,
                                  name="status-http", daemon=True)
        thread.start()
        log("relay", f"status on http://{bind}:{port}/")


def enrolled_from_env() -> list[str]:
    raw = os.environ.get("INSTAVISION_ENROLLED", "").strip()
    return [part.strip() for part in raw.split(",") if part.strip()]


async def list_devices() -> int:
    probe = load_module("probe", "probe.py")
    token = await asyncio.to_thread(probe.firebase_login)
    if not token or await asyncio.to_thread(
        probe.request, "POST", "atlas/users/session", {"id_token": token}
    ) is None:
        return 1
    spaces = await asyncio.to_thread(probe.request, "GET", "atlas/spaces")
    items = spaces.get("items") if isinstance(spaces, dict) else spaces
    space = items[0]["id"]
    devices = await asyncio.to_thread(
        probe.request, "GET", f"atlas/spaces/{space}/devices"
    )
    devices = devices.get("items") if isinstance(devices, dict) else devices
    print(f"{'name':10} {'ip':16} {'online':8} {'path':22} device id")
    for d in devices or []:
        state = d.get("device_state") or {}
        ip = state.get("ip_addr") or "-"
        alive = subprocess.run(["ping", "-c", "1", "-W", "1", ip],
                               capture_output=True).returncode == 0 if ip != "-" else False
        print(f"{d['name']:10} {ip:16} {'up' if alive else 'down':8} "
              f"{slugify(d['name']):22} {d['id']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="list cameras and exit")
    ap.add_argument("--once", action="store_true", help="one attempt per camera")
    ap.add_argument("--device", action="append", default=[],
                    help="limit to these camera names (repeatable)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if args.list:
        return asyncio.run(list_devices())

    # Settings can come from instavision/.env (run by hand) or from the
    # container's environment; the check is on the values, not on the file.
    probe = load_module("probe", "probe.py")
    missing = [k for k in ("INSTAVISION_EMAIL", "INSTAVISION_PASSWORD",
                           "INSTAVISION_FIREBASE_KEY") if not probe.ENV.get(k)]
    if missing:
        log("relay", f"missing settings: {', '.join(missing)}")
        return 3

    enrolled = args.device or enrolled_from_env()
    rtsp_base = os.environ.get(
        "INSTAVISION_RTSP_BASE", "rtsp://127.0.0.1:8574"
    )
    relay = Relay(enrolled, rtsp_base, verbose=args.verbose)
    os.makedirs(STATE_DIR, exist_ok=True)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    task = loop.create_task(relay.run(once=args.once))

    def shutdown(signum, _frame):
        log("relay", f"signal {signum}: stopping")
        relay.stop()
        if not task.done():
            task.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, shutdown, sig, None)
        except NotImplementedError:
            signal.signal(sig, shutdown)

    try:
        return loop.run_until_complete(task)
    except asyncio.CancelledError:
        return 0
    finally:
        loop.close()


if __name__ == "__main__":
    sys.exit(main())
