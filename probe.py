#!/usr/bin/env python3
"""Explore the InstaVision account: log in, list spaces/devices, show stream credentials.

Read-only. Nothing is enrolled, nothing is switched on - this exists to answer the
two questions the APK analysis left open:

  1. does the account's device list look like the cameras we expect, and
  2. what exactly does stream-credentials hand back for one of them.

Login mirrors the app exactly: Firebase password sign-in, then POST
atlas/users/session with the Firebase ID token, which sets a session cookie that
every later request rides on.

Usage:
    ./instavision/probe.py            # spaces + devices + credential shape
    ./instavision/probe.py --creds    # also dump the credential JSON (secrets masked)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def load_env(path: str) -> dict[str, str]:
    """Minimal .env reader. Values may be quoted and may contain '#'."""
    out: dict[str, str] = {}
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
                val = val[1:-1]
            out[key.strip()] = val
    return out


def settings() -> dict[str, str]:
    """Settings from instavision/.env, overridden by the environment.

    Both paths matter: run by hand, this reads .env next to the script; run in
    the container, Docker Compose passes the same names as environment
    variables and there is no .env inside the image.
    """
    values: dict[str, str] = {}
    path = os.path.join(HERE, ".env")
    if os.path.exists(path):
        values.update(load_env(path))
    values.update(
        {k: v for k, v in os.environ.items() if k.startswith("INSTAVISION_")}
    )
    return values


ENV = settings()
API = ENV.get("INSTAVISION_API", "https://api-us.instavision.ai/").rstrip("/")
PARTNER = ENV.get("INSTAVISION_PARTNER", "instaview")
CLIENT = ENV.get("INSTAVISION_CLIENT_ID", "home-user-portal")
FIREBASE_KEY = ENV.get("INSTAVISION_FIREBASE_KEY", "")

COOKIE_JAR: dict[str, str] = {}


def headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    h = {
        "Accept": "application/json",
        "Accept-Version": "v3",
        "Accept-Language": "en-US",
        "Client-ID": CLIENT,
        "Partner-ID": PARTNER,
        "User-Agent": "InstaVision/2026.10.27.1243 (Android 14)",
    }
    if COOKIE_JAR:
        h["Cookie"] = "; ".join(f"{k}={v}" for k, v in COOKIE_JAR.items())
    if extra:
        h.update(extra)
    return h


def request(method: str, path: str, body: dict | None = None, *, raw: bool = False):
    url = f"{API}/{path.lstrip('/')}"
    data = None
    h = headers()
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            for cookie in resp.headers.get_all("Set-Cookie") or []:
                name, _, rest = cookie.partition("=")
                COOKIE_JAR[name.strip()] = rest.split(";", 1)[0].strip()
            payload = resp.read()
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        print(f"  !! {method} {path} -> {exc.code}")
        print(f"     {payload[:400].decode(errors='replace')}")
        return None if not raw else payload
    if raw:
        return payload
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        print(f"  !! non-JSON from {path}: {payload[:200]}")
        return None


def firebase_login() -> str | None:
    """Exchange email/password for a Firebase ID token."""
    if not FIREBASE_KEY:
        print("  !! INSTAVISION_FIREBASE_KEY is not set")
        return None
    if not ENV.get("INSTAVISION_EMAIL") or not ENV.get("INSTAVISION_PASSWORD"):
        print("  !! INSTAVISION_EMAIL / INSTAVISION_PASSWORD are not set")
        return None
    url = (
        "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"
        f"?key={FIREBASE_KEY}"
    )
    body = json.dumps(
        {
            "email": ENV["INSTAVISION_EMAIL"],
            "password": ENV["INSTAVISION_PASSWORD"],
            "returnSecureToken": True,
        }
    ).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        err = exc.read().decode(errors="replace")
        print(f"  !! Firebase login failed: {err[:300]}")
        return None
    print(f"  firebase ok: uid={data.get('localId')} "
          f"email={data.get('email')} verified={data.get('registered')}")
    return data.get("idToken")


def mask(value):
    """Show that a secret exists without printing it."""
    if isinstance(value, str):
        if len(value) > 24:
            return f"<{len(value)} chars>"
        return value
    return value


def show_creds(payload: dict) -> None:
    """Print the credential shape with secrets replaced by their length."""
    safe = json.loads(json.dumps(payload))
    for key in ("access_key_id", "secret_access_key", "session_token"):
        if key in safe:
            print(f"    {key}: {mask(safe[key])}")
            safe.pop(key)
    print("    region:", safe.get("region"))
    for chan in safe.get("channel_info") or []:
        print(f"    channel device_id={chan.get('device_id')}")
        arn = chan.get("channel_arn") or ""
        print(f"      channel_arn: {arn[:60]}...")
        for ep in chan.get("endpoints") or []:
            print(f"      endpoint: {ep.get('protocol')} {ep.get('endpoint')}")
        ice = chan.get("ice_config") or []
        print(f"      ice servers: {len(ice)}")
        for i in ice:
            uris = i.get("uris")
            print(f"        urls={uris} ttl={i.get('ttl')} "
                  f"user={'set' if i.get('username') else 'none'}")
    for key in safe:
        val = safe[key]
        if key in ("channel_info", "region"):
            continue
        preview = val if not isinstance(val, (dict, list)) else type(val).__name__
        print(f"    {key}: {preview}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--creds", action="store_true", help="dump credential JSON")
    ap.add_argument("--space", help="space id to inspect (default: first)")
    args = ap.parse_args()

    print("== 1. firebase login ==")
    token = firebase_login()
    if not token:
        return 1

    print("\n== 2. atlas session ==")
    sess = request("POST", "atlas/users/session", {"id_token": token})
    if sess is None:
        print("  session exchange failed")
        return 1
    print(f"  session keys: {sorted(sess)[:12] if isinstance(sess, dict) else sess}")
    print(f"  cookies: {sorted(COOKIE_JAR) or 'none'}")

    print("\n== 3. profile ==")
    me = request("GET", "atlas/users")
    if isinstance(me, dict):
        interesting = {
            k: me[k]
            for k in ("id", "email", "name", "partner_id", "locale")
            if k in me
        }
        print(f"  {interesting or sorted(me)[:12]}")

    print("\n== 4. spaces ==")
    spaces = request("GET", "atlas/spaces")
    items = spaces.get("items") if isinstance(spaces, dict) else spaces
    if not items:
        print(f"  raw: {str(spaces)[:400]}")
        return 1
    for sp in items:
        print(f"  - {sp.get('id')}  name={sp.get('name')!r} "
              f"role={sp.get('role')} tz={sp.get('timezone')}")

    space_id = args.space or items[0].get("id")

    print(f"\n== 5. devices in space {space_id} ==")
    devices = request("GET", f"atlas/spaces/{space_id}/devices")
    dev_items = devices.get("items") if isinstance(devices, dict) else devices
    if not dev_items:
        print(f"  raw: {str(devices)[:600]}")
        return 1
    print(f"  {len(dev_items)} device(s)")
    for d in dev_items:
        state = d.get("device_state") or {}
        net = d.get("network") or {}
        print(f"  - {d.get('id')}  {d.get('name')!r}")
        print(f"      type={d.get('device_type')} model={d.get('model')} "
              f"fw={d.get('firmware_version')} power={d.get('power_source')}")
        print(f"      online={d.get('online')} ip={d.get('local_ip') or net.get('ip') or '-'} "
              f"state={sorted(state)[:6]}")

    if args.creds:
        print(f"\n== 6. stream-credentials for first device ==")
        dev_id = dev_items[0].get("id")
        creds = request(
            "GET",
            f"atlas/spaces/{space_id}/stream-credentials?device_id={dev_id}",
        )
        if isinstance(creds, dict):
            show_creds(creds)
        else:
            print(f"  raw: {str(creds)[:600]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
