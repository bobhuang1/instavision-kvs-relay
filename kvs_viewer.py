#!/usr/bin/env python3
"""Receive an InstaVision camera's video as a KVS WebRTC viewer.

The app is a WebRTC viewer of an AWS Kinesis Video Streams channel, so a relay
does not need to impersonate a proprietary protocol - it needs to do the same two
things the app does:

  1. ask the vendor API for the camera's channel ARN, WSS endpoint, ICE servers
     and temporary AWS credentials (see probe.py), and
  2. join that channel as a viewer: presign the WSS URL with SigV4, send an SDP
     offer, exchange ICE candidates, receive the media.

This module implements step 2 and nothing else. It is deliberately small so the
signalling can be tested independently of the relay built on top of it.

Why aiortc: it is pure Python over aioice and supports TURN, which matters because
AWS hands out TURN servers for the case where the camera is not reachable directly.
When the relay runs on the same LAN as the camera, ICE usually finds a host
candidate pair and the media never leaves the house.

Usage:
    ./kvs_viewer.py --device Cam3 --seconds 20      # dump SDP and count frames
    ./kvs_viewer.py --device Cam3 --save frame.jpg  # save one received frame
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
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection
from aiortc.mediastreams import MediaStreamError

HERE = os.path.dirname(os.path.abspath(__file__))
STUN = "stun:stun.kinesisvideo.us-west-2.amazonaws.com:443"


def load_probe():
    """Import probe.py so the credential path has exactly one implementation."""
    spec = importlib.util.spec_from_file_location("probe", os.path.join(HERE, "probe.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- SigV4 for the signalling websocket -------------------------------------


def _sign(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


def presign_wss(endpoint: str, region: str, creds: dict, channel_arn: str,
                client_id: str | None, expires: int = 299) -> str:
    """Presign a KVS signalling WSS URL the way the AWS SDK does for browsers.

    KVS signalling authorises the channel through the query string, not a header.
    Two service parameters ride along with the SigV4 ones and are therefore part
    of what gets signed:

        X-Amz-ChannelARN  the channel to join
        X-Amz-ClientId    required for ConnectAsViewer - without it the service
                          answers HTTP 400 "ClientId must be specified"

    The role itself is inferred from X-Amz-ClientId: present means viewer, absent
    means master. Passing client_id=None therefore connects as a MASTER, which is
    only useful as a presence probe (a channel admits one).
    """
    host = endpoint.split("://", 1)[1].rstrip("/")
    path = "/"
    now = dt.datetime.now(dt.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    scope = f"{datestamp}/{region}/kinesisvideo/aws4_request"

    params = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-ChannelARN": channel_arn,
        "X-Amz-Credential": f"{creds['access_key_id']}/{scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(expires),
        "X-Amz-SignedHeaders": "host",
    }
    if client_id is not None:
        params["X-Amz-ClientId"] = client_id
    if creds.get("session_token"):
        params["X-Amz-Security-Token"] = creds["session_token"]

    quote = lambda s: urllib.parse.quote(s, safe="-_.~")  # noqa: E731
    canonical_query = "&".join(
        f"{quote(k)}={quote(v)}" for k, v in sorted(params.items())
    )
    # KVS signs a GET with no body, so the payload hash is SHA256 of the empty
    # string. "UNSIGNED-PAYLOAD" is what a browser SDK may send, but the service
    # compares against the empty hash and answers 403 SignatureDoesNotMatch.
    empty_hash = hashlib.sha256(b"").hexdigest()
    canonical_request = "\n".join(
        ["GET", path, canonical_query, f"host:{host}\n", "host", empty_hash]
    )
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ]
    )
    key = _sign(f"AWS4{creds['secret_access_key']}".encode(), datestamp)
    key = _sign(key, region)
    key = _sign(key, "kinesisvideo")
    key = _sign(key, "aws4_request")
    signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()

    return f"wss://{host}{path}?{canonical_query}&X-Amz-Signature={signature}"


# --- the viewer -------------------------------------------------------------


class _TapBuffer:
    """Jitter buffer wrapper that copies each complete frame to the sink."""

    def __init__(self, inner, viewer: "KvsViewer"):
        self._inner = inner
        self._viewer = viewer

    def add(self, packet):
        pli, frame = self._inner.add(packet)
        if frame is not None and frame.data:
            self._viewer.forwarded += 1
            try:
                self._viewer.encoded_sink(frame.data)
            except Exception as exc:  # a dead publisher must not kill signalling
                self._viewer.log("sink error:", exc)
        return pli, frame

    def __getattr__(self, name):
        return getattr(self._inner, name)


class KvsViewer:
    """One viewer session against one camera's KVS channel."""

    def __init__(self, creds: dict, channel: dict, client_id: str, *, debug=False):
        self.creds = creds
        self.channel = channel
        self.region = creds.get("region") or "us-west-2"
        self.client_id = client_id
        self.debug = debug
        self.answered = asyncio.Event()
        # Set by the relay: called with each complete H.264 access unit, already
        # in Annex-B form, so it can be published without decoding anything.
        self.encoded_sink = None
        self.forwarded = 0
        self.pc: RTCPeerConnection | None = None
        self.ws = None
        self.master_id: str | None = None
        self.frames = 0
        self.bytes_received = 0
        self.video_tracks: list[str] = []
        self.audio_tracks = 0
        self.answer_sdp: str | None = None
        self.candidate_pairs: list[str] = []
        self.error: str | None = None
        self.done = asyncio.Event()

    def log(self, *args):
        if self.debug:
            print("   [kvs]", *args, flush=True)

    def ice_servers(self) -> RTCConfiguration:
        servers = [RTCIceServer(urls=STUN)]
        for entry in self.channel.get("ice_config") or []:
            uris = entry.get("uris") or []
            if uris:
                servers.append(
                    RTCIceServer(
                        urls=uris,
                        username=entry.get("username"),
                        credential=entry.get("password"),
                    )
                )
        return RTCConfiguration(iceServers=servers)

    def _endpoint(self) -> str:
        for ep in self.channel.get("endpoints") or []:
            if str(ep.get("protocol", "")).upper() == "WSS":
                return ep["endpoint"]
        raise RuntimeError("no WSS endpoint in channel info")

    async def connect(self) -> None:
        url = presign_wss(
            self._endpoint(),
            self.region,
            self.creds,
            self.channel["channel_arn"],
            self.client_id,
        )
        self.pc = RTCPeerConnection(self.ice_servers())

        # A viewer asks to receive only. Two video transceivers because this model
        # family can be dual-lens; the master decides how many it fills.
        for _ in range(2):
            self.pc.addTransceiver("video", direction="recvonly")
        self.pc.addTransceiver("audio", direction="recvonly")

        # The app always opens a data channel before offering, and uses it for
        # control messages (wake_up / live_start, PTZ, siren). Mirroring that keeps
        # our offer the same shape the camera already expects.
        self.data_channel = self.pc.createDataChannel("DataChannel")

        @self.data_channel.on("open")
        def on_dc_open():
            self.log("data channel open")
            self.data_channel.send(
                json.dumps(
                    {"senderClientId": self.client_id,
                     "type": "wake_up", "action": "live_start"}
                )
            )

        @self.pc.on("track")
        async def on_track(track):
            if track.kind == "video":
                self.video_tracks.append(track.id)
                asyncio.ensure_future(self.consume_video(track))
            else:
                self.audio_tracks += 1
            self.log("track", track.kind, track.id)

        @self.pc.on("connectionstatechange")
        async def on_state():
            self.log("pc state:", self.pc.connectionState)
            if self.pc.connectionState in ("failed", "closed", "disconnected"):
                if self.pc.connectionState != "disconnected":
                    self.done.set()

        @self.pc.on("iceconnectionstatechange")
        async def on_ice():
            self.log("ice:", self.pc.iceConnectionState)

        @self.pc.on("icecandidate")
        async def on_candidate(candidate):
            if candidate is None:
                return
            payload = {
                "candidate": f"candidate:{candidate.to_sdp()}",
                "sdpMid": candidate.sdpMid,
                "sdpMLineIndex": candidate.sdpMLineIndex,
                "usernameFragment": candidate.usernameFragment,
            }
            await self.send("ICE_CANDIDATE", payload)

        # The socket has to exist before the offer is sent, otherwise send() has
        # nowhere to write to and the master never sees an offer at all.
        async with websockets.connect(url, max_size=2**22, open_timeout=20) as ws:
            self.ws = ws
            self.log("signalling connected")
            await self.pc.setLocalDescription(await self.pc.createOffer())
            await self.send(
                "SDP_OFFER",
                {"type": "offer", "sdp": self.pc.localDescription.sdp},
            )
            self.log("offer sent")
            await self.pump()

    async def send(self, message_type: str, payload: dict):
        """Send one signalling message.

        The message envelope uses `messageType` (not `action`) and carries the
        payload as base64 of a JSON object. A viewer must never set
        recipientClientId - the service rejects it.
        """
        if self.ws is None:
            return
        msg = {
            "messageType": message_type,
            "messagePayload": base64.b64encode(json.dumps(payload).encode()).decode(),
            "senderClientId": self.client_id,
        }
        await self.ws.send(json.dumps(msg))

    async def pump(self) -> None:
        """Read signalling messages until the session ends."""
        assert self.ws is not None
        async for raw in self.ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            message_type = msg.get("messageType") or msg.get("action")
            sender = msg.get("senderClientId")
            raw_payload = msg.get("messagePayload") or ""
            self.log("recv", message_type, "from", sender)

            try:
                data = json.loads(base64.b64decode(raw_payload).decode()) \
                    if raw_payload else {}
            except Exception:
                data = {}

            if message_type == "SDP_ANSWER":
                sdp = data.get("sdp") or ""
                self.answer_sdp = sdp
                self.master_id = sender or self.master_id
                await self.pc.setRemoteDescription(
                    type("D", (), {"type": "answer", "sdp": sdp})()
                )
                self.answered.set()
                self.install_tap()
            elif message_type == "ICE_CANDIDATE":
                self.master_id = sender or self.master_id
                cand = (data.get("candidate") or "").replace("candidate:", "", 1)
                if cand:
                    await self.pc.addIceCandidate(
                        type(
                            "C",
                            (),
                            {
                                "candidate": cand,
                                "sdpMid": data.get("sdpMid"),
                                "sdpMLineIndex": data.get("sdpMLineIndex"),
                            },
                        )()
                    )
            elif message_type == "STATUS_RESPONSE":
                self.log("status:", data)

    def install_tap(self) -> None:
        """Forward encoded frames without decoding them.

        aiortc assembles each complete access unit in the receiver's jitter
        buffer and only then hands it to the decoder thread. Wrapping that buffer
        is the one place where the H.264 stream exists as a whole, already
        Annex-B (the H.264 depayloader prepends start codes for single NALs,
        FU-A and STAP-A alike), so a relay can copy it instead of decoding and
        re-encoding - roughly 10% of the CPU per camera.

        The decoder thread is stopped afterwards: nothing consumes decoded
        frames here, and leaving it running would both burn CPU and grow the
        track's queue without bound.
        """
        if self.encoded_sink is None or self.pc is None:
            return
        for receiver in self.pc.getReceivers():
            if receiver.kind != "video":
                continue
            try:
                inner = receiver._RTCRtpReceiver__jitter_buffer
            except AttributeError:
                self.log("no jitter buffer on receiver; cannot tap")
                continue
            if isinstance(inner, _TapBuffer):
                continue
            receiver._RTCRtpReceiver__jitter_buffer = _TapBuffer(inner, self)
            try:
                receiver._RTCRtpReceiver__stop_decoder()
            except Exception as exc:  # decoder shutdown is an optimisation only
                self.log("decoder stop failed:", exc)

    async def consume_video(self, track) -> None:
        while True:
            try:
                frame = await asyncio.wait_for(track.recv(), timeout=15)
            except (asyncio.TimeoutError, MediaStreamError):
                break
            self.frames += 1
            self.bytes_received += len(frame.to_ndarray().tobytes())
            if self.frames == 1:
                self.log("first frame", frame.width, "x", frame.height)
                self.first_frame = frame
            if self.frames % 30 == 0:
                self.log(f"{self.frames} frames")

    async def run(self, seconds: float, answer_timeout: float | None = None):
        loop = asyncio.get_event_loop()
        started = loop.time()
        task = asyncio.ensure_future(self.connect())
        finished = asyncio.ensure_future(self.done.wait())
        try:
            while True:
                elapsed = loop.time() - started
                if elapsed >= seconds:
                    break
                if task.done() or finished.done():
                    break
                if (answer_timeout is not None and not self.answered.is_set()
                        and elapsed >= answer_timeout):
                    # No camera joined its channel: give up early so the relay's
                    # retry loop notices a camera coming online within seconds
                    # rather than after a full session timeout.
                    self.error = "no SDP answer (camera is not publishing)"
                    break
                await asyncio.wait({task, finished}, timeout=1.0,
                                   return_when=asyncio.FIRST_COMPLETED)
        finally:
            if task.done() and not task.cancelled():
                exc = task.exception()
                if exc is not None:
                    # The socket handshake is where a signing bug shows up, so it
                    # must be reported rather than left in a lost task. The body
                    # carries the service's own explanation of a rejected upgrade.
                    detail = ""
                    body = getattr(getattr(exc, "response", None), "body", None)
                    if body:
                        detail = " | " + body[:400].decode(errors="replace")
                    self.error = f"{type(exc).__name__}: {exc}{detail}"
            else:
                task.cancel()
            finished.cancel()
            if self.pc:
                await self.pc.close()


def pick_channel(creds: dict, device_id: str) -> dict:
    for chan in creds.get("channel_info") or []:
        if chan.get("device_id") == device_id:
            return chan
    raise SystemExit("no channel for that device in the credentials")


async def main_async(args) -> int:
    probe = load_probe()
    print("== login + credentials ==")
    token = probe.firebase_login()
    if not token:
        return 1
    if probe.request("POST", "atlas/users/session", {"id_token": token}) is None:
        return 1
    spaces = probe.request("GET", "atlas/spaces")
    space = (spaces["items"] if isinstance(spaces, dict) else spaces)[0]["id"]
    devices = probe.request("GET", f"atlas/spaces/{space}/devices")
    devices = devices["items"] if isinstance(devices, dict) else devices

    wanted = args.device.lower()
    device = next(
        (d for d in devices if wanted in (d.get("name") or "").lower()
         or wanted == (d.get("id") or "").lower()),
        None,
    )
    if device is None:
        print("no such device; available:",
              ", ".join(d.get("name", "?") for d in devices))
        return 1

    creds = probe.request(
        "GET",
        f"atlas/spaces/{space}/stream-credentials?device_id={device['id']}",
    )
    if not isinstance(creds, dict):
        print("no credentials returned")
        return 1
    channel = pick_channel(creds, device["id"])
    print(f"  device  : {device.get('name')} ({device['id']})")
    print(f"  model   : {device.get('model_name')}")
    print(f"  region  : {creds.get('region')}")
    print(f"  channel : {channel['channel_arn'][:58]}...")
    print(f"  wss     : {channel['endpoints'][0]['endpoint']}")
    print(f"  turn    : {len(channel.get('ice_config') or [])} ICE server(s), "
          f"ttl={(channel.get('ice_config') or [{}])[0].get('ttl')}")

    viewer = KvsViewer(creds, channel, device["id"], debug=args.verbose)
    print(f"\n== joining channel as viewer for {args.seconds:g}s ==")
    await viewer.run(args.seconds)

    print("\n== result ==")
    if viewer.error:
        print(f"  error           : {viewer.error}")
    print(f"  answer received : {'yes' if viewer.answer_sdp else 'NO'}")
    print(f"  master client   : {viewer.master_id or '(none)'}")
    print(f"  video tracks    : {len(viewer.video_tracks)}")
    print(f"  audio tracks    : {viewer.audio_tracks}")
    print(f"  frames received : {viewer.frames}")
    if viewer.answer_sdp:
        codecs = [
            line for line in viewer.answer_sdp.splitlines()
            if line.startswith("a=rtpmap") or line.startswith("a=fmtp")
        ]
        print("  answer codecs   :")
        for line in codecs[:10]:
            print(f"    {line}")
    if args.save and getattr(viewer, "first_frame", None) is not None:
        viewer.first_frame.to_image().save(args.save)
        print(f"  saved frame     : {args.save}")
    return 0 if viewer.frames else 2


async def listen_all(seconds: float, verbose: bool) -> None:
    """Join every camera's channel at once and report whichever answers.

    Useful when the camera only joins its channel while a stream is being
    requested: open the phone app, and whichever camera is being watched answers
    here too. That is also the proof that two viewers can share one camera.
    """
    probe = load_probe()
    token = probe.firebase_login()
    if not token or probe.request("POST", "atlas/users/session", {"id_token": token}) is None:
        return
    spaces = probe.request("GET", "atlas/spaces")
    space = (spaces["items"] if isinstance(spaces, dict) else spaces)[0]["id"]
    devices = probe.request("GET", f"atlas/spaces/{space}/devices")
    devices = devices["items"] if isinstance(devices, dict) else devices

    import subprocess as sp

    alive = []
    for d in devices:
        ip = (d.get("device_state") or {}).get("ip_addr")
        if not ip:
            continue
        if sp.run(["ping", "-c", "1", "-W", "1", ip], capture_output=True).returncode == 0:
            alive.append(d)
    print(f"listening on {len(alive)} awake camera(s) for {seconds:g}s: "
          f"{', '.join(d['name'] for d in alive)}", flush=True)

    viewers = []
    for d in alive:
        creds = probe.request(
            "GET", f"atlas/spaces/{space}/stream-credentials?device_id={d['id']}"
        )
        if not isinstance(creds, dict):
            continue
        try:
            chan = pick_channel(creds, d["id"])
        except SystemExit:
            continue
        viewers.append((d["name"], KvsViewer(creds, chan, d["id"], debug=verbose)))

    await asyncio.gather(*(v.run(seconds) for _, v in viewers), return_exceptions=True)

    print("\n== who answered ==", flush=True)
    for name, v in viewers:
        status = "ANSWER" if v.answer_sdp else "silent"
        print(f"  {name:9} {status:7} frames={v.frames} tracks={len(v.video_tracks)}"
              + (f" error={v.error}" if v.error else ""), flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="Cam3", help="camera name or device id")
    ap.add_argument("--seconds", type=float, default=20)
    ap.add_argument("--save", help="save the first received frame as an image")
    ap.add_argument("--all", action="store_true",
                    help="join every awake camera's channel at once")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    try:
        if args.all:
            asyncio.run(listen_all(args.seconds, args.verbose))
            return 0
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
