#!/usr/bin/env python3
"""InstaVision gateway: one web UI for the cameras, plus the RTSP relay.

This is the InstaVision equivalent of the OSAIO relay's gateway. It runs the KVS
relay (relay.py) in this process, so a camera that publishes to its cloud channel
is republished to MediaMTX as rtsp://<host>:8574/insta_<name>, and it adds the
things the vendor's API does expose directly:

    GET  /                          the camera grid (snapshot tiles + PTZ pad)
    GET  /api/status                relay state joined with per-camera metadata
    GET  /api/snapshot/<name>       latest cloud snapshot, proxied and cached
    POST /api/ptz/<name>            {"pan": int, "tilt": int, "nonStop": bool}
    POST /api/ptz/<name>/stop       stop a move
    POST /api/ptz/<name>/reset      return to the preset position
    GET  /healthz                   relay health alone (for a watchdog)

PTZ is the vendor's own API: POST atlas/spaces/{space}/devices/{id}/ptz/move with
{"pan", "tilt", "nonStop"} (MoveRequest in the app), where the camera advertises
pan/tilt in -10000..0. Snapshots come from the snapshot_url the vendor returns per
camera; they are captured on the camera's own schedule, so they can be minutes old
and the UI says so rather than pretending they are live.

Auth: set INSTAVISION_UI_PASSWORD to require HTTP basic auth on everything. With no
password the listener binds to loopback only - these endpoints can move cameras, so
they are not left open to the LAN by default.

    python3 gateway.py                 # UI on http://127.0.0.1:8665
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

HERE = os.path.dirname(os.path.abspath(__file__))

UI_PORT = int(os.environ.get("INSTAVISION_UI_PORT", "8665"))
UI_PASSWORD = os.environ.get("INSTAVISION_UI_PASSWORD", "")
UI_BIND = os.environ.get("INSTAVISION_UI_BIND") or (
    "0.0.0.0" if UI_PASSWORD else "127.0.0.1")
MEDIAMTX_WEBRTC = os.environ.get(
    "INSTAVISION_WEBRTC_BASE", "http://127.0.0.1:8909").rstrip("/")

# The vendor list endpoint is not free, and the UI polls. Snapshots are captured on
# the camera's own schedule anyway, so a minute of cache costs nothing.
DEVICE_TTL = float(os.environ.get("INSTAVISION_DEVICE_TTL", "60"))
SNAPSHOT_TTL = float(os.environ.get("INSTAVISION_SNAPSHOT_TTL", "30"))

PTZ_NAMES = {"pan", "tilt", "nonstop"}


def load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe = load_module("probe", "probe.py")


def log(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [gateway] {message}", flush=True)


class Cameras:
    """Device list and snapshots, cached, with one re-login on auth expiry."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._devices: list[dict] = []
        self._devices_at = 0.0
        self._snapshots: dict[str, tuple[float, bytes]] = {}
        self._space = ""
        self._session_at = 0.0

    # --- session ----------------------------------------------------------

    def login(self) -> bool:
        token = probe.firebase_login()
        if not token:
            return False
        if probe.request("POST", "atlas/users/session",
                         {"id_token": token}) is None:
            return False
        self._session_at = time.time()
        return True

    def _ensure_session(self) -> None:
        # Refresh every 30 minutes; the vendor session otherwise expires under a
        # long-running gateway and every request starts failing with 401.
        if time.time() - self._session_at > 1800:
            self.login()

    def request(self, method: str, path: str, body: dict | None = None,
                *, raw: bool = False):
        self._ensure_session()
        try:
            return probe.request(method, path, body, raw=raw)
        except urllib.error.HTTPError as exc:
            if exc.code != 401:
                raise
            # One retry with a fresh session: an expired cookie is the usual cause.
            if not self.login():
                raise
            return probe.request(method, path, body, raw=raw)

    # --- devices ----------------------------------------------------------

    def devices(self, force: bool = False) -> list[dict]:
        with self._lock:
            if not force and self._devices and time.time() - self._devices_at < DEVICE_TTL:
                return self._devices
        if not self._space:
            spaces = self.request("GET", "atlas/spaces")
            items = spaces.get("items") if isinstance(spaces, dict) else spaces
            if not items:
                return self._devices
            self._space = items[0]["id"]
        devices = self.request("GET", f"atlas/spaces/{self._space}/devices")
        items = devices.get("items") if isinstance(devices, dict) else devices
        with self._lock:
            if items:
                self._devices = items
                self._devices_at = time.time()
            return self._devices

    def find(self, name: str) -> dict | None:
        wanted = name.lower()
        return next(
            (d for d in self.devices()
             if (d.get("name") or "").lower() == wanted), None)

    def space(self) -> str:
        if not self._space:
            self.devices(force=True)
        return self._space

    def snapshot(self, name: str) -> bytes | None:
        with self._lock:
            cached = self._snapshots.get(name)
            if cached and time.time() - cached[0] < SNAPSHOT_TTL:
                return cached[1]
        device = self.find(name)
        if device is None:
            return None
        url = (device.get("device_state") or {}).get("snapshot_url")
        if not url:
            return None
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
                body = resp.read()
        except (urllib.error.URLError, OSError):
            return None
        with self._lock:
            self._snapshots[name] = (time.time(), body)
        return body

    # --- PTZ --------------------------------------------------------------

    def ptz(self, name: str, pan: int, tilt: int, non_stop: bool,
            action: str = "move") -> tuple[bool, str]:
        device = self.find(name)
        if device is None:
            return False, "unknown camera"
        path = (f"atlas/spaces/{self.space()}/devices/{device['id']}/ptz/{action}")
        body = None if action == "reset" else {
            "pan": int(pan), "tilt": int(tilt), "nonStop": bool(non_stop)}
        try:
            # raw: the endpoint answers 200 with an empty body, which the JSON
            # helper would report as a failure.
            raw = self.request("POST", path, body, raw=True)
        except urllib.error.HTTPError as exc:
            return False, f"HTTP {exc.code}"
        except urllib.error.URLError as exc:
            return False, str(exc.reason)[:120]
        if raw is None:
            return False, "no answer from the vendor API"
        return True, "sent"


class Handler(BaseHTTPRequestHandler):
    server_version = "instavision-gateway"
    cameras = Cameras()
    relay_status = staticmethod(lambda: {})
    enrolled: list[str] = []

    # --- plumbing ---------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:  # keep the access log quiet
        if os.environ.get("INSTAVISION_UI_VERBOSE"):
            log(f"{self.address_string()} {fmt % args}")

    def _auth_ok(self) -> bool:
        if not UI_PASSWORD:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            raw = base64.b64decode(header[6:]).decode()
        except Exception:
            return False
        return raw.partition(":")[2] == UI_PASSWORD

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, payload) -> None:
        self._send(code, json.dumps(payload).encode(), "application/json")

    def _deny(self) -> None:
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="InstaVision"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return {}

    # --- routing ----------------------------------------------------------

    def do_GET(self) -> None:
        if not self._auth_ok():
            return self._deny()
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            ui = os.path.join(HERE, "ui.html")
            try:
                return self._send(200, open(ui, "rb").read(), "text/html; charset=utf-8")
            except OSError:
                return self._send(500, b"ui.html missing", "text/plain")
        if path == "/healthz":
            status = self.relay_status()
            return self._json(200 if status.get("ok") else 503, status)
        if path == "/api/status":
            return self._json(200, self._status())
        m = re.fullmatch(r"/api/snapshot/([^/]+)", path)
        if m:
            name = unquote(m.group(1))
            body = self.cameras.snapshot(name)
            if body is None:
                return self._send(404, b"no snapshot", "text/plain")
            return self._send(200, body, "image/jpeg")
        return self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        if not self._auth_ok():
            return self._deny()
        path = self.path.split("?", 1)[0]
        m = re.fullmatch(r"/api/ptz/([^/]+)(/stop|/reset)?", path)
        if not m:
            return self._send(404, b"not found", "text/plain")
        name = unquote(m.group(1))
        action = (m.group(2) or "/move").lstrip("/")
        body = self._read_json()
        pan = int(body.get("pan") or 0)
        tilt = int(body.get("tilt") or 0)
        non_stop = bool(body.get("nonStop"))
        ok, detail = self.cameras.ptz(name, pan, tilt, non_stop, action)
        return self._json(200 if ok else 502,
                          {"ok": ok, "camera": name, "action": action,
                           "detail": detail})

    do_HEAD = do_GET

    # --- payload for the UI ----------------------------------------------

    def _status(self) -> dict:
        relay = self.relay_status() or {}
        # Build the player and RTSP URLs from the host the browser actually used, so
        # a phone on the LAN gets working links without a per-host setting.
        host = (self.headers.get("Host") or "127.0.0.1").split(":")[0]
        webrtc = MEDIAMTX_WEBRTC if os.environ.get("INSTAVISION_WEBRTC_BASE") \
            else f"http://{host}:8909"
        rtsp_host = os.environ.get("INSTAVISION_RTSP_HOST") or host
        workers = {w.get("name"): w for w in relay.get("cameras") or []}
        cameras = []
        for device in self.cameras.devices():
            name = device.get("name") or ""
            state = device.get("device_state") or {}
            worker = workers.get(name) or {}
            clusters = [c.get("name") or "" for c in device.get("clusters") or []]
            ptz = any("PTZ" in c.upper() for c in clusters)
            ip = state.get("ip_addr") or ""
            # "streaming" only once frames have actually arrived; a session that is
            # open but silent is waiting, not live.
            state_name = worker.get("state") or (
                "disabled" if name not in self.enrolled else "starting")
            if state_name == "streaming" and not (worker.get("frames") or 0):
                state_name = "waiting"
            cameras.append({
                "name": name,
                "ip": ip,
                "online": bool(ip),
                "ptz": ptz,
                "state": state_name,
                "state_detail": worker.get("last_error") or "",
                "frames": worker.get("frames") or 0,
                "live_url": f"{webrtc}/{_slug(name)}/",
                "rtsp_url": f"rtsp://{rtsp_host}:8574/{_slug(name)}",
            })
        cameras.sort(key=lambda c: c["name"])
        return {"relay": relay, "cameras": cameras, "generated_at": time.time()}


def _slug(name: str) -> str:
    return "insta_" + re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def main() -> int:
    cameras = Cameras()
    if not cameras.login():
        log("could not sign in - check INSTAVISION_EMAIL / INSTAVISION_PASSWORD / "
            "INSTAVISION_FIREBASE_KEY")
        return 3
    devices = cameras.devices(force=True)
    log(f"signed in: {len(devices)} camera(s) on the account")

    # The relay runs inside this process so the UI and the RTSP paths cannot drift
    # apart: one credential path, one set of KVS sessions.
    enrolled = [d.get("name") for d in devices if d.get("name")]

    # The UI polls every 15 s, but the relay must not re-login that often: holding a
    # KVS offer open is cheap, refetching credentials for twelve cameras every 25 s
    # is not. Hold each session, then rebuild it. Set before relay.py is imported,
    # because that module reads the value at import time.
    os.environ["INSTAVISION_ANSWER_TIMEOUT"] = os.environ.get(
        "INSTAVISION_GATEWAY_ANSWER_TIMEOUT", "600")
    # The relay's own status listener would collide with the UI's port; give it a
    # distinct one so a watchdog can watch the relay alone.
    os.environ["INSTAVISION_HTTP_PORT"] = os.environ.get(
        "INSTAVISION_RELAY_STATUS_PORT", "8656")

    relay_module = load_module("relay", "relay.py")
    relay = relay_module.Relay(enrolled, os.environ.get(
        "INSTAVISION_RTSP_BASE", "rtsp://127.0.0.1:8574"))

    loop_thread = threading.Thread(
        target=lambda: __import__("asyncio").run(relay.run()), daemon=True)
    loop_thread.start()

    Handler.cameras = cameras
    Handler.enrolled = enrolled
    Handler.relay_status = relay.status
    server = ThreadingHTTPServer((UI_BIND, UI_PORT), Handler)
    log(f"UI on http://{UI_BIND}:{UI_PORT}"
        + ("" if UI_PASSWORD else "  (loopback only: set INSTAVISION_UI_PASSWORD "
                                  "to reach it from the LAN)"))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        relay.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
