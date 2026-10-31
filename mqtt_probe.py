#!/usr/bin/env python3
"""Watch the InstaVision AWS IoT broker, to find what starts a camera's stream.

The phone app is the only client that can start a live view, and the two things it
has that the web client does not are:

  * the AWS KVS WebRTC viewer (which we already implement - it gets no answer), and
  * an AWS IoT MQTT client (`ai.instavision.guardian.sdk.aws.mqtt.MqttConnection`).

`stream-credentials` hands back an IoT endpoint and a client-id prefix alongside the
KVS channel, which means the app keeps a broker connection while it is looking at a
camera. If a camera is told to join its KVS channel, this broker is the only channel
left that could carry that command - so subscribe to it and read what the cameras and
the backend actually publish.

This is a read-only probe: it subscribes and prints. It publishes nothing.

    ./mqtt_probe.py --seconds 90            # subscribe to # and watch
    ./mqtt_probe.py --topic '$aws/things/+/shadow/update/accepted'
    ./mqtt_probe.py --seconds 30 --device Cam3   # only topics naming that device

MQTT here is 3.1.1 carried over a SigV4-presigned WebSocket: the authorisation lives
in the query string (service `iotdevicegateway`), and the CONNECT packet carries no
credentials, which is why there is no username/password anywhere in this file.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import datetime as dt
import hashlib
import hmac
import importlib.util
import json
import os
import sys
import urllib.parse

import websockets

HERE = os.path.dirname(os.path.abspath(__file__))


def load_probe():
    spec = importlib.util.spec_from_file_location("probe", os.path.join(HERE, "probe.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def presign_iot(endpoint: str, region: str, creds: dict, expires: int = 300) -> str:
    """Presign the IoT MQTT-over-WebSocket URL (service `iotdevicegateway`)."""
    host = endpoint.rstrip("/")
    path = "/mqtt"
    now = dt.datetime.now(dt.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    scope = f"{datestamp}/{region}/iotdevicegateway/aws4_request"
    params = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{creds['access_key_id']}/{scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(expires),
        "X-Amz-SignedHeaders": "host",
    }
    if creds.get("session_token"):
        params["X-Amz-Security-Token"] = creds["session_token"]
    quote = lambda s: urllib.parse.quote(s, safe="-_.~")  # noqa: E731
    query = "&".join(f"{quote(k)}={quote(v)}" for k, v in sorted(params.items()))
    canonical = "\n".join(
        ["GET", path, query, f"host:{host}\n", "host", hashlib.sha256(b"").hexdigest()]
    )
    to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", amz_date, scope,
         hashlib.sha256(canonical.encode()).hexdigest()]
    )
    key = _sign(f"AWS4{creds['secret_access_key']}".encode(), datestamp)
    key = _sign(key, region)
    key = _sign(key, "iotdevicegateway")
    key = _sign(key, "aws4_request")
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    return f"wss://{host}{path}?{query}&X-Amz-Signature={signature}"


# --- just enough MQTT 3.1.1 to subscribe and read ---------------------------


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value % 128
        value //= 128
        if value:
            byte |= 0x80
        out.append(byte)
        if not value:
            return bytes(out)


def _string(text: str) -> bytes:
    raw = text.encode()
    return len(raw).to_bytes(2, "big") + raw


def mqtt_connect(client_id: str, keepalive: int = 60) -> bytes:
    body = _string("MQTT") + bytes([4, 0x00]) + keepalive.to_bytes(2, "big")
    body += _string(client_id)
    return bytes([0x10]) + _varint(len(body)) + body


def mqtt_subscribe(packet_id: int, topic: str, qos: int = 0) -> bytes:
    body = packet_id.to_bytes(2, "big") + _string(topic) + bytes([qos])
    return bytes([0x82]) + _varint(len(body)) + body


def parse_packet(data: bytes) -> tuple[int, bytes, int]:
    """Return (packet type, remaining bytes, bytes consumed)."""
    if len(data) < 2:
        raise ValueError("short packet")
    first = data[0]
    multiplier = 1
    length = 0
    pos = 1
    while True:
        if pos >= len(data):
            raise ValueError("truncated length")
        byte = data[pos]
        pos += 1
        length += (byte & 0x7F) * multiplier
        if not byte & 0x80:
            break
        multiplier *= 128
    if len(data) < pos + length:
        raise ValueError("incomplete")
    return first >> 4, data[pos:pos + length], pos + length


async def run(args) -> int:
    probe = load_probe()
    token = probe.firebase_login()
    if not token or probe.request("POST", "atlas/users/session",
                                  {"id_token": token}) is None:
        return 1
    spaces = probe.request("GET", "atlas/spaces")
    space = (spaces["items"] if isinstance(spaces, dict) else spaces)[0]["id"]
    devices = probe.request("GET", f"atlas/spaces/{space}/devices")
    devices = devices["items"] if isinstance(devices, dict) else devices
    wanted = None
    if args.device:
        dev = next((d for d in devices
                    if args.device.lower() in (d.get("name") or "").lower()), None)
        if dev is None:
            print("no such device")
            return 1
        wanted = dev
    device = wanted or devices[0]

    creds = probe.request(
        "GET",
        f"atlas/spaces/{space}/stream-credentials?device_id={device['id']}",
    )
    if not isinstance(creds, dict):
        return 1
    mqtt = creds.get("mqtt") or {}
    endpoint = mqtt.get("endpoint")
    prefix = mqtt.get("client_id_prefix", "")
    region = creds.get("region", "us-west-2")
    if not endpoint:
        print("credentials carry no mqtt endpoint")
        return 1

    client_id = f"{prefix}{base64.urlsafe_b64encode(os.urandom(4)).decode().rstrip('=')}"[:120]
    url = presign_iot(endpoint, region, creds)
    print(f"broker   : {endpoint}")
    print(f"client id: {client_id}")
    print(f"topic    : {args.topic}")
    print(f"watching for {args.seconds}s ...\n", flush=True)

    async with websockets.connect(url, subprotocols=["mqtt"], max_size=2**22,
                                  open_timeout=20) as ws:
        await ws.send(mqtt_connect(client_id))
        raw = await ws.recv()
        kind, body, _ = parse_packet(raw)
        if kind != 2:
            print(f"unexpected first packet type {kind}: {body[:80]}")
            return 1
        code = body[1] if len(body) > 1 else 255
        print(f"CONNACK code={code} "
              f"({'accepted' if code == 0 else 'REJECTED'})\n", flush=True)
        if code != 0:
            return 1
        await ws.send(mqtt_subscribe(1, args.topic, args.qos))

        buffer = b""
        try:
            async with asyncio.timeout(args.seconds):
                while True:
                    buffer += await ws.recv()
                    while True:
                        try:
                            kind, body, used = parse_packet(buffer)
                        except ValueError:
                            break
                        buffer = buffer[used:]
                        if kind == 9:  # SUBACK
                            granted = body[2] if len(body) > 2 else 0x80
                            print(f"SUBACK granted={granted} "
                                  f"({'ok' if granted != 0x80 else 'DENIED'})\n",
                                  flush=True)
                            if granted == 0x80:
                                return 1
                        elif kind == 3:  # PUBLISH
                            qos = (buffer[0] if buffer else 0)
                            tlen = int.from_bytes(body[0:2], "big")
                            topic = body[2:2 + tlen].decode(errors="replace")
                            payload = body[2 + tlen:]
                            stamp = dt.datetime.now().strftime("%H:%M:%S")
                            print(f"{stamp} {topic}  {payload[:300].decode(errors='replace')}",
                                  flush=True)
                        elif kind == 13:  # PINGRESP
                            pass
        except (asyncio.TimeoutError, TimeoutError):
            pass
    print("\nfinished")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=90)
    ap.add_argument("--topic", default="#")
    ap.add_argument("--qos", type=int, default=0)
    ap.add_argument("--device", help="use this camera's credentials")
    args = ap.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
