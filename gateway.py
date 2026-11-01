#!/usr/bin/env python3
"""InstaVision-Relay web app: one page to run and watch the relay.

The relay itself (relay.py) only speaks RTSP to MediaMTX. This process wraps it
in the things a person actually needs: a sign-in screen with a first-run
administrator, tabs for Viewer / Cameras / Streams & ports / Settings, live tiles
with PTZ, zoom, mute and fullscreen, camera add/remove, and copyable Frigate YAML.

The three tabs exist because of three real constraints of these cameras:

    Viewer      MediaMTX WHEP played inline in the tile, with the camera's
                snapshot shown while it is not publishing - which is most of the
                time, so a tile that only spoke WebRTC would sit black
    Cameras     cameras come from the account; "add" enrolls a camera in the
                relay rather than pairing it, because there is nothing local to
                pair with
    No firewall There is no router integration and no vendor server key to
                manage here, so no tab is invented for them

Routes:

    GET  /                          the app (redirects to /login when signed out)
    GET  /login  POST /login        sign in
    POST /setup                     first-run administrator
    POST /logout
    GET  /api/session               {first_run, authenticated, user, hint}
    GET  /api/cameras               account cameras joined with relay state
    POST /api/cameras/<name>/enroll {"enrolled": bool}
    GET  /api/snapshot/<name>       latest cloud snapshot (cached)
    POST /api/ptz/<name>            {"pan", "tilt", "nonStop"}
    POST /api/ptz/<name>/stop|reset
    GET  /api/streams               per-camera URLs + Frigate YAML
    POST /api/settings/password     {"current", "next"}
    POST /api/relay/stop|start
    POST /whep/<name>               WHEP proxy to MediaMTX (same-origin, behind auth)
    GET  /healthz

Auth is a signed session cookie. INSTAVISION_UI_PASSWORD double-duties as HTTP
basic auth for scripts and as the bootstrap administrator when no administrator
exists yet.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.environ.get("INSTAVISION_STATE_DIR", os.path.join(HERE, "state"))
AUTH_FILE = os.path.join(STATE_DIR, "auth.json")
ENROLL_FILE = os.path.join(STATE_DIR, "enrolled.json")
SECRET_FILE = os.path.join(STATE_DIR, "session.key")

UI_PORT = int(os.environ.get("INSTAVISION_UI_PORT", "8665"))
UI_BIND = os.environ.get("INSTAVISION_UI_BIND") or "0.0.0.0"
ENV_PASSWORD = os.environ.get("INSTAVISION_UI_PASSWORD", "")
SESSION_TTL = int(os.environ.get("INSTAVISION_SESSION_TTL", str(7 * 86400)))

# `or` rather than a default argument: compose passes these as empty strings when
# unset, and an empty base turns the WHEP proxy URL into a relative path.
MEDIAMTX_WEBRTC = (os.environ.get("INSTAVISION_WEBRTC_BASE")
                   or "http://127.0.0.1:8909").rstrip("/")
MEDIAMTX_HLS = (os.environ.get("INSTAVISION_HLS_BASE")
                or "http://127.0.0.1:8908").rstrip("/")

DEVICE_TTL = float(os.environ.get("INSTAVISION_DEVICE_TTL", "60"))
SNAPSHOT_TTL = float(os.environ.get("INSTAVISION_SNAPSHOT_TTL", "30"))

PBKDF2_ROUNDS = 200_000


def log(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [gateway] {message}", flush=True)


def load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe = load_module("probe", "probe.py")


def slug(name: str) -> str:
    return "insta_" + re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def read_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: str, payload) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=1)
    os.replace(tmp, path)


# --- auth -------------------------------------------------------------------


def hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS).hex()


class Auth:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._record = read_json(AUTH_FILE, None)
        self._secret = self._load_secret()
        if not self._record and ENV_PASSWORD:
            self.create(os.environ.get("INSTAVISION_UI_USER", "admin"), ENV_PASSWORD, "")

    def _load_secret(self) -> bytes:
        try:
            with open(SECRET_FILE, "rb") as handle:
                data = handle.read()
            if len(data) >= 32:
                return data
        except OSError:
            pass
        secret = secrets.token_bytes(32)
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(SECRET_FILE, "wb") as handle:
                handle.write(secret)
            os.chmod(SECRET_FILE, 0o600)
        except PermissionError as exc:
            # A fresh clone that was mounted before ./state existed leaves Docker
            # creating it as root:root, while compose runs this process as
            # $INSTAVISION_UID. A restart loop of tracebacks hides that; say what
            # to do instead. See the state note in README "Quick start".
            owner = "unknown"
            try:
                owner = str(os.stat(STATE_DIR).st_uid)
            except OSError:
                pass
            raise SystemExit(
                f"\n[state] cannot write {SECRET_FILE}: {exc}\n"
                f"[state] this process runs as uid {os.getuid()}:{os.getgid()}, "
                f"{STATE_DIR} is owned by uid {owner}.\n"
                f"[state] either set INSTAVISION_UID/INSTAVISION_GID in .env to your "
                f"own ids (`id -u`, `id -g`) or make that directory writable by them.\n"
            ) from exc
        return secret

    # --- account ---------------------------------------------------------

    @property
    def first_run(self) -> bool:
        return not self._record

    def hint(self) -> str:
        return (self._record or {}).get("hint", "")

    def username(self) -> str:
        return (self._record or {}).get("username", "")

    def create(self, username: str, password: str, hint: str) -> None:
        salt = secrets.token_bytes(16)
        with self._lock:
            self._record = {
                "username": username,
                "salt": salt.hex(),
                "hash": hash_password(password, salt),
                "hint": hint,
                "created": int(time.time()),
            }
            write_json(AUTH_FILE, self._record)

    def verify(self, username: str, password: str) -> bool:
        record = self._record
        if not record:
            return False
        if username and username != record["username"]:
            return False
        expected = record["hash"]
        got = hash_password(password, bytes.fromhex(record["salt"]))
        return hmac.compare_digest(expected, got)

    def change(self, current: str, new: str) -> bool:
        if not self.verify(self.username(), current):
            return False
        self.create(self.username(), new, self.hint())
        return True

    # --- sessions --------------------------------------------------------

    def issue(self) -> str:
        payload = json.dumps({"u": self.username(), "exp": int(time.time()) + SESSION_TTL})
        body = base64.urlsafe_b64encode(payload.encode()).decode()
        sig = hmac.new(self._secret, body.encode(), hashlib.sha256).hexdigest()
        return f"{body}.{sig}"

    def check(self, token: str) -> bool:
        body, _, sig = (token or "").partition(".")
        if not body or not sig:
            return False
        expected = hmac.new(self._secret, body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return False
        try:
            payload = json.loads(base64.urlsafe_b64decode(body))
        except Exception:
            return False
        return payload.get("exp", 0) > time.time()

    def reset(self) -> None:
        with self._lock:
            self._record = None
            for path in (AUTH_FILE, SECRET_FILE):
                try:
                    os.remove(path)
                except OSError:
                    pass


# --- vendor API with a cached device list -----------------------------------


class Cameras:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._devices: list[dict] = []
        self._devices_at = 0.0
        self._snapshots: dict[str, tuple[float, bytes]] = {}
        self._space = ""
        self._session_at = 0.0

    def login(self) -> bool:
        token = probe.firebase_login()
        if not token:
            return False
        if probe.request("POST", "atlas/users/session", {"id_token": token}) is None:
            return False
        self._session_at = time.time()
        return True

    def _ensure_session(self) -> None:
        if time.time() - self._session_at > 1800:
            self.login()

    def request(self, method: str, path: str, body: dict | None = None, *, raw=False):
        self._ensure_session()
        try:
            return probe.request(method, path, body, raw=raw)
        except urllib.error.HTTPError as exc:
            if exc.code != 401:
                raise
            if not self.login():
                raise
            return probe.request(method, path, body, raw=raw)

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
        return next((d for d in self.devices()
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

    def ptz(self, name: str, pan: int, tilt: int, non_stop: bool,
            action: str = "move") -> tuple[bool, str]:
        device = self.find(name)
        if device is None:
            return False, "unknown camera"
        path = f"atlas/spaces/{self.space()}/devices/{device['id']}/ptz/{action}"
        body = None if action == "reset" else {
            "pan": int(pan), "tilt": int(tilt), "nonStop": bool(non_stop)}
        try:
            raw = self.request("POST", path, body, raw=True)
        except urllib.error.HTTPError as exc:
            return False, f"HTTP {exc.code}"
        except urllib.error.URLError as exc:
            return False, str(exc.reason)[:120]
        if raw is None:
            return False, "no answer from the vendor API"
        return True, "sent"


# --- relay lifecycle, restartable when enrollment changes -------------------


class RelayManager:
    """Runs relay.py in a thread, and can restart it with a new enrollment.

    Enrollment changes are a human action (add/remove camera), so restarting the
    relay is the honest, simple implementation: one process, one set of KVS
    sessions, no half-applied state.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._relay = None
        self._thread: threading.Thread | None = None
        self.enrolled: list[str] = []
        self._module = None

    def _load(self):
        if self._module is None:
            # The relay reads this at import; a held offer is cheap, a credential
            # fetch every 25 s for a dozen cameras is not.
            os.environ["INSTAVISION_ANSWER_TIMEOUT"] = os.environ.get(
                "INSTAVISION_GATEWAY_ANSWER_TIMEOUT", "600")
            os.environ["INSTAVISION_HTTP_PORT"] = os.environ.get(
                "INSTAVISION_RELAY_STATUS_PORT", "8656")
            self._module = load_module("relay", "relay.py")
        return self._module

    def start(self, enrolled: list[str]) -> None:
        with self._lock:
            self._stop_locked()
            module = self._load()
            self.enrolled = list(enrolled)
            relay = module.Relay(self.enrolled, os.environ.get(
                "INSTAVISION_RTSP_BASE", "rtsp://127.0.0.1:8574"))
            self._relay = relay
            self._thread = threading.Thread(
                target=lambda: __import__("asyncio").run(relay.run()),
                name="relay", daemon=True)
            self._thread.start()
        log(f"relay started for {len(enrolled)} camera(s)")

    def _stop_locked(self) -> None:
        if self._relay is not None:
            self._relay.stop()
        if self._thread is not None:
            self._thread.join(timeout=15)
        self._relay = None
        self._thread = None

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()
        log("relay stopped")

    def running(self) -> bool:
        return self._relay is not None

    def status(self) -> dict:
        relay = self._relay
        if relay is None:
            return {"ok": True, "idle": True, "stopped": True, "expected": 0,
                    "streaming": 0, "cameras": []}
        return relay.status()


# --- HTTP -------------------------------------------------------------------


def enrolled_names(cams: Cameras) -> list[str]:
    """Enrollment is a saved list; an empty file means every camera on the account."""
    record = read_json(ENROLL_FILE, None)
    if isinstance(record, dict) and isinstance(record.get("names"), list):
        return record["names"]
    return [d.get("name") for d in cams.devices() if d.get("name")]


class Handler(BaseHTTPRequestHandler):
    server_version = "instavision-relay"
    auth = Auth()
    cameras = Cameras()
    relay = RelayManager()
    # serialises enrollment changes so two clicks cannot race
    enroll_lock = threading.Lock()

    def log_message(self, fmt: str, *args) -> None:
        if os.environ.get("INSTAVISION_UI_VERBOSE"):
            log(f"{self.address_string()} {fmt % args}")

    # --- auth helpers -----------------------------------------------------

    def _cookie_token(self) -> str:
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "iv_session":
                return value
        return ""

    def _authenticated(self) -> bool:
        if self.auth.check(self._cookie_token()):
            return True
        if ENV_PASSWORD:
            header = self.headers.get("Authorization", "")
            if header.startswith("Basic "):
                try:
                    user, _, password = base64.b64decode(header[6:]).decode().partition(":")
                except Exception:
                    return False
                return hmac.compare_digest(password, ENV_PASSWORD)
        return False

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True  # curl and friends
        host = (self.headers.get("Host") or "").lower()
        return origin.split("://")[-1].lower() == host

    def _require_auth(self) -> bool:
        if self._authenticated():
            return True
        if self.path.startswith("/api/") or self.path.startswith("/whep/"):
            self._json(401, {"error": "sign in first"})
        else:
            self._redirect("/login")
        return False

    # --- responses --------------------------------------------------------

    def _send(self, code: int, body: bytes, ctype: str, extra=None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, payload, extra=None) -> None:
        self._send(code, json.dumps(payload).encode(), "application/json", extra)

    def _redirect(self, where: str, extra=None) -> None:
        self.send_response(302)
        self.send_header("Location", where)
        self.send_header("Content-Length", "0")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def _html(self, filename: str) -> None:
        try:
            with open(os.path.join(HERE, filename), "rb") as handle:
                self._send(200, handle.read(), "text/html; charset=utf-8")
        except OSError:
            self._send(500, b"missing " + filename.encode(), "text/plain")

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        ctype = self.headers.get("Content-Type", "")
        if "application/x-www-form-urlencoded" in ctype:
            from urllib.parse import parse_qs
            return {k: v[0] for k, v in parse_qs(raw.decode()).items()}
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}

    def _host(self) -> str:
        return (self.headers.get("Host") or "127.0.0.1").split(":")[0]

    # --- views ------------------------------------------------------------

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]

        if path == "/healthz":
            status = self.relay.status()
            return self._json(200 if status.get("ok") else 503, status)

        if path == "/login":
            if self._authenticated():
                return self._redirect("/")
            return self._html("login.html")

        if path in ("/", "/index.html"):
            if not self._authenticated():
                return self._redirect("/login")
            return self._html("ui.html")

        if path == "/api/session":
            return self._json(200, {
                "first_run": self.auth.first_run,
                "authenticated": self._authenticated(),
                "user": self.auth.username(),
                "hint": self.auth.hint(),
            })

        if not self._require_auth():
            return

        if path == "/api/cameras":
            return self._json(200, {"cameras": self._cameras_payload()})

        if path == "/api/streams":
            return self._json(200, self._streams_payload())

        match = re.fullmatch(r"/api/snapshot/([^/]+)", path)
        if match:
            body = self.cameras.snapshot(unquote(match.group(1)))
            if body is None:
                return self._send(404, b"no snapshot", "text/plain")
            return self._send(200, body, "image/jpeg")

        return self._send(404, b"not found", "text/plain")

    do_HEAD = do_GET

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]

        if not self._same_origin():
            return self._json(403, {"error": "cross-origin request refused"})

        if path == "/api/setup":
            if not self.auth.first_run:
                return self._json(409, {"error": "administrator already exists"})
            body = self._body()
            password = body.get("password") or ""
            if len(password) < 4:
                return self._json(400, {"error": "use at least 4 characters"})
            self.auth.create(body.get("username") or "admin", password,
                             body.get("hint") or "")
            return self._json(200, {"ok": True}, {"Set-Cookie": self._cookie()})

        if path == "/api/login":
            body = self._body()
            if self.auth.verify(body.get("username") or "", body.get("password") or ""):
                return self._json(200, {"ok": True}, {"Set-Cookie": self._cookie()})
            return self._json(401, {"error": "wrong username or password"})

        if path == "/api/logout":
            return self._json(200, {"ok": True},
                              {"Set-Cookie": "iv_session=; Path=/; Max-Age=0"})

        if not self._require_auth():
            return

        match = re.fullmatch(r"/api/cameras/([^/]+)/enroll", path)
        if match:
            body = self._body()
            return self._enroll(unquote(match.group(1)), bool(body.get("enrolled")))

        match = re.fullmatch(r"/api/ptz/([^/]+)(/stop|/reset)?", path)
        if match:
            body = self._body()
            name = unquote(match.group(1))
            action = (match.group(2) or "/move").lstrip("/")
            ok, detail = self.cameras.ptz(
                name, int(body.get("pan") or 0), int(body.get("tilt") or 0),
                bool(body.get("nonStop")), action)
            return self._json(200 if ok else 502,
                              {"ok": ok, "camera": name, "action": action,
                               "detail": detail})

        if path == "/api/settings/password":
            body = self._body()
            if self.auth.change(body.get("current") or "", body.get("next") or ""):
                return self._json(200, {"ok": True}, {"Set-Cookie": self._cookie()})
            return self._json(403, {"error": "current password is wrong"})

        if path in ("/api/relay/stop", "/api/relay/start"):
            with self.enroll_lock:
                if path.endswith("stop"):
                    self.relay.stop()
                else:
                    self.relay.start(self._enrolled())
            return self._json(200, {"ok": True, "running": self.relay.running()})

        match = re.fullmatch(r"/whep/([^/]+)", path)
        if match:
            return self._whep(unquote(match.group(1)))

        return self._send(404, b"not found", "text/plain")

    # --- behaviors --------------------------------------------------------

    def _cookie(self) -> str:
        return (f"iv_session={self.auth.issue()}; Path=/; HttpOnly; "
                f"SameSite=Lax; Max-Age={SESSION_TTL}")

    def _enrolled(self) -> list[str]:
        return enrolled_names(self.cameras)

    def _enroll(self, name: str, enrolled: bool) -> None:
        with self.enroll_lock:
            names = self._enrolled()
            if enrolled and name not in names:
                names.append(name)
            if not enrolled:
                names = [n for n in names if n != name]
            write_json(ENROLL_FILE, {"names": names})
            self.relay.start(names)
        return self._json(200, {"ok": True, "enrolled": enrolled, "enrolled_names": names})

    def _whep(self, name: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        sdp = self.rfile.read(length) if length else b""
        url = f"{MEDIAMTX_WEBRTC}/{slug(name)}/whep"
        request = urllib.request.Request(
            url, data=sdp, method="POST",
            headers={"Content-Type": self.headers.get("Content-Type", "application/sdp")})
        try:
            with urllib.request.urlopen(request, timeout=20) as resp:
                return self._send(resp.status, resp.read(),
                                  resp.headers.get("Content-Type", "application/sdp"))
        except urllib.error.HTTPError as exc:
            return self._send(exc.code, exc.read() or b"whep error", "text/plain")
        except urllib.error.URLError as exc:
            return self._send(502, f"MediaMTX unreachable: {exc.reason}".encode(),
                              "text/plain")

    # --- payloads ---------------------------------------------------------

    def _cameras_payload(self) -> list[dict]:
        status = self.relay.status() or {}
        workers = {w.get("name"): w for w in status.get("cameras") or []}
        enrolled = set(self._enrolled())
        host = self._host()
        cameras = []
        for device in self.cameras.devices():
            name = device.get("name") or ""
            state = device.get("device_state") or {}
            worker = workers.get(name) or {}
            clusters = [c.get("name") or "" for c in device.get("clusters") or []]
            frames = worker.get("frames") or 0
            live = worker.get("state") == "streaming" and frames > 0
            cameras.append({
                "name": name,
                "id": device.get("id"),
                "ip": state.get("ip_addr") or "",
                "online": bool(state.get("ip_addr")),
                "firmware": state.get("firmware_version") or "",
                "model": device.get("model_name") or "",
                "variant": device.get("variant_name") or "",
                "ptz": any("PTZ" in c.upper() for c in clusters),
                "enrolled": name in enrolled,
                "state": "live" if live else ("waiting" if worker else "off"),
                "detail": worker.get("last_error") or "",
                "frames": frames,
                "rtsp": f"rtsp://{host}:8574/{slug(name)}",
                "hls": f"http://{host}:8908/{slug(name)}/index.m3u8",
                "webrtc": f"http://{host}:8909/{slug(name)}/",
                "whep": f"/whep/{quote(name)}",
                "snapshot": f"/api/snapshot/{quote(name)}",
            })
        cameras.sort(key=lambda c: c["name"])
        return cameras

    def _streams_payload(self) -> dict:
        host = self._host()
        cameras = [c for c in self._cameras_payload() if c["enrolled"]]
        lines = ["go2rtc:", "  streams:"]
        for cam in cameras:
            lines.append(f"    {slug(cam['name'])}: {cam['rtsp']}")
        lines += ["", "cameras:"]
        for cam in cameras:
            lines += [
                f"  {slug(cam['name'])}:",
                "    ffmpeg:",
                "      inputs:",
                f"        - path: rtsp://127.0.0.1:8554/{slug(cam['name'])}",
                "          roles: [detect]",
                "    detect:",
                "      enabled: true",
            ]
        return {
            "cameras": cameras,
            "frigate_yaml": "\n".join(lines),
            "ports": [
                {"name": "Gateway (this page)", "address": f"http://{host}:{UI_PORT}"},
                {"name": "RTSP (MediaMTX)", "address": f"rtsp://{host}:8574/<path>"},
                {"name": "HLS", "address": f"http://{host}:8908/<path>/index.m3u8"},
                {"name": "WebRTC / WHEP", "address": f"http://{host}:8909/<path>/"},
                {"name": "Frigate go2rtc", "address": "rtsp://127.0.0.1:8554/<path>"},
            ],
        }


def main() -> int:
    if not Handler.cameras.login():
        log("could not sign in - check INSTAVISION_EMAIL / INSTAVISION_PASSWORD / "
            "INSTAVISION_FIREBASE_KEY")
        return 3
    devices = Handler.cameras.devices(force=True)
    log(f"signed in: {len(devices)} camera(s) on the account")

    enrolled = enrolled_names(Handler.cameras)
    if not read_json(ENROLL_FILE, None):
        write_json(ENROLL_FILE, {"names": enrolled})
    Handler.relay.start(enrolled)

    server = ThreadingHTTPServer((UI_BIND, UI_PORT), Handler)
    log(f"UI on http://{UI_BIND}:{UI_PORT}  "
        f"({'administrator set' if not Handler.auth.first_run else 'first run: create the administrator'})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        Handler.relay.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
