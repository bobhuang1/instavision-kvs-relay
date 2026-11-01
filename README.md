# instavision-kvs-relay

Watch **InstaVision** (`ai.instaview.guardian`) Wi-Fi cameras through their AWS
Kinesis Video Streams WebRTC channels and republish them as plain **RTSP**, so
Frigate, Home Assistant, VLC or any other NVR can consume them like an ordinary
camera.

These cameras expose nothing on the LAN — measured on twelve of them: every port
closed on 80/443/554/5000/8000/8080/8888/34567, and the seven that answer ping
share one OUI (`8c:5c:53`). No RTSP, no ONVIF, no web UI, no local API. The only
video that exists is WebRTC negotiated through the vendor cloud, so a relay that
logs in as a viewer and re-publishes is the only way in.

That is exactly what this does, with one deliberate design choice: the H.264
access units are copied, never decoded. A camera costs a few percent of one core
instead of most of a core, which matters on a small box already running an NVR.

```
InstaVision app account
        |
        |  1. Firebase password sign-in -> /atlas/users/session
        |  2. /atlas/spaces/{space}/stream-credentials?device_id=...
        |     -> AWS creds + channel ARN + WSS endpoint + TURN, per attempt
        v
   relay.py  ---  3. KVS WebRTC viewer join per camera (kvs_viewer.py)
        |         4. Annex-B H.264 straight out of aiortc into ffmpeg
        v
    ffmpeg -c copy  -> RTSP -> MediaMTX (rtsp://127.0.0.1:8574/insta_<name>)
        |
        v
     Frigate / HA / VLC  <-  rtsp://<host>:8574/insta_<name>
```

## What is and is not proven

| Half | Status | Evidence |
| --- | --- | --- |
| Vendor login, per-camera credentials, KVS signalling | Works | Fetches real credentials for every device on the account |
| **Republish path** (WebRTC H.264 -> ffmpeg -> RTSP -> MediaMTX) | **Works** | [`selftest.py`](selftest.py): a generated 640×360 clip is pushed through the real publisher classes and read back off MediaMTX as `h264 640x360 15/1`, decoded frame for frame |
| **Cloud snapshots** | **Works** | Every camera returns a 640×360 JPEG from its `snapshot_url`; the burned-in timestamp shows the camera uploading on its own schedule (minutes old, so the UI dates them rather than calling them live) |
| **PTZ** | **Commands accepted, effect unverified** | `POST …/ptz/move` with `{"pan", "tilt", "nonStop"}` (the app's `MoveRequest`) returns 200, and pan/tilt are advertised in `-10000..0`. Whether the head physically moves could not be confirmed: the camera reports no change in its PTZ attribute values, and the only view of the camera is a snapshot that updates on its own schedule |
| **Camera capture** (a camera actually publishing when we join) | **Not proven** | On this account, no camera answered an offer: an 8-camera sweep, then a viewer offer held open on one camera for ten minutes, both produced no SDP answer, no `recv` of any kind from AWS, and no RTSP path — see [`selftest.py`](selftest.py) for the half that does work |

The relay cannot *make* a camera stream. The app's own viewer path was
decompiled to check: it calls the same `stream-credentials` endpoint, builds the
same `wss://…?X-Amz-ChannelARN=…&X-Amz-ClientId=…` URL, signs it with service
`kinesisvideo` and connects — **there is no call in between**. The app's whole
endpoint table contains no start-stream, no `/live`, no `/session` call. The
decision to publish is made by the camera, and the two channels that could carry
a wake command are closed to us:

* **KVS master role.** KVS derives viewer vs master from `X-Amz-ClientId`:
  present means viewer, absent means master. The vendor-issued credentials are
  scoped to `kinesisvideo:ConnectAsViewer` and reject the master form with
  `HTTP 400 "ClientId must be specified for ConnectAsViewer"`.
* **The IoT control channel.** `stream-credentials` hands out
  `mqtt: {endpoint: <account>-ats.iot.<region>.amazonaws.com, client_id_prefix: app_<uid>_}`.
  A SigV4-presigned MQTT-over-WebSocket to that broker is refused with
  `403 {"message":"Forbidden"}` for every client-id shape tried.

**Practical consequence:** a camera is captured while it is uploading on its own
— typically while someone is watching it in the InstaVision app, or because of
its own motion detection. The relay polls with backoff and picks the stream up
within seconds whenever that happens. To test whether your cameras behave the
same way, open a camera's live view in the phone app while the relay is running
and watch for `insta_<name>` to appear.

## Quick start

```bash
git clone https://github.com/bobhuang1/instavision-kvs-relay.git
cd instavision-kvs-relay
cp .env.example .env      # then fill in the three values below
```

Fill in `.env`:

| Variable | What it is |
| --- | --- |
| `INSTAVISION_EMAIL` / `INSTAVISION_PASSWORD` | The account the relay signs in as. Use one you are willing to have a service sign in as — it will appear as a session in the app. |
| `INSTAVISION_FIREBASE_KEY` | Firebase Web API key of the vendor's project. Read it from the app's own configuration; it is a public client key, not a secret. |
| `INSTAVISION_ENROLLED` | Comma-separated camera names to publish. **Empty publishes nothing.** |

```bash
docker compose up -d
docker compose logs -f gateway
```

That starts MediaMTX and the gateway, which serves the UI on port 8665 and runs the
relay inside itself, so the UI and the RTSP paths cannot drift apart. Open
`http://<host>:8665/`.

`--list` shows what the account holds (the profile keeps this from starting a second
set of streaming sessions):

```bash
docker compose run --rm --profile relay relay python3 relay.py --list
```

Publishing is deliberately an allowlist: an empty `INSTAVISION_ENROLLED` means the
relay logs that and publishes nothing, so a camera cannot quietly start using
uplink. The gateway ignores that list and enrolls every camera on the account,
because its job is to be watching when a camera starts streaming.

## Web UI and PTZ

The gateway is the InstaVision equivalent of the OSAIO relay's gateway, using the
same idea: one page for every camera, with a live pane and the vendor's own PTZ
controls.

| Endpoint | What it does |
| --- | --- |
| `GET /` | Camera grid: snapshot tile per camera, online/streaming badge, PTZ pad |
| `GET /api/status` | Relay state joined with per-camera metadata (JSON) |
| `GET /api/snapshot/<name>` | Latest cloud snapshot, proxied and cached (`INSTAVISION_SNAPSHOT_TTL`, default 30 s) |
| `POST /api/ptz/<name>` | `{"pan": int, "tilt": int, "nonStop": bool}` — the app's `MoveRequest` |
| `POST /api/ptz/<name>/stop` | Stop a move |
| `POST /api/ptz/<name>/reset` | Return to the preset position |
| `GET /healthz` | Relay health alone, for a watchdog |

PTZ is not a guess: the app's own `DeviceRepository.ptzMove` posts to
`atlas/spaces/{space}/devices/{id}/ptz/move` with `MoveRequest{pan, tilt, nonStop}`,
and each camera advertises a `0xFC12 PTZ` cluster with pan and tilt in the range
`-10000..0`. The UI sends steps of 300, and always offers a stop button because
`nonStop: true` keeps the head moving until told to stop.

**Auth.** Set `INSTAVISION_UI_PASSWORD` and every endpoint requires HTTP basic auth
(any username). With no password the UI binds to loopback only — these routes can
move cameras, so they are not left open to the LAN by default.

**What the tiles are.** Snapshots come from the camera's own snapshot upload, not
from a live stream, so a tile can be minutes old. The UI never calls them live: a
tile reads `LIVE` only when the relay is receiving frames, `snapshot` when all it has
is a still, and `offline` when the camera reports no IP.

## Adding a camera to Frigate

Each camera lands on `rtsp://<host>:8574/insta_<name>`, where `<name>` is the
camera name lower-cased with runs of non-alphanumerics collapsed to `_`
(`Cam3` becomes `insta_cam3`).

```yaml
# go2rtc
go2rtc:
  streams:
    insta_cam3: rtsp://192.168.1.30:8574/insta_cam3

cameras:
  insta_cam3:
    ffmpeg:
      inputs:
        - path: rtsp://127.0.0.1:8554/insta_cam3
          roles: [detect, record]
    detect:
      enabled: true
```

The relay only carries video. Audio is left out on purpose: nothing here needs it,
and re-encapsulating it is one more thing to go wrong.

## Ports

All three relays on a typical NVR host run with `network_mode: host`, so the ports
are chosen not to collide:

| Service | RTSP | API | HLS | WebRTC |
| --- | --- | --- | --- | --- |
| Frigate's go2rtc | 8554/tcp | — | 1984 | 8555/udp |
| OSAIO relay (MediaMTX) | 8564/tcp | 9997 | 8898 | 8899 |
| **this relay (MediaMTX)** | **8574/tcp** | **9998** | **8908** | **8909** |

The gateway's own pages sit on **8665** (UI + API), with the relay's health listener
on **8656** so a watchdog can watch the relay alone.

In `mediamtx.yml`, publishing is localhost-only (the relay runs on the same host);
reading is open to the LAN so Frigate, Home Assistant and a phone on the sofa can
all pull a stream.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `INSTAVISION_ENROLLED` | *(empty)* | Allowlist of camera names to publish. |
| `INSTAVISION_RTSP_BASE` | `rtsp://127.0.0.1:8574` | Where to publish. |
| `INSTAVISION_SESSION_SECONDS` | `3600` | How long one viewer session runs before it is rebuilt with fresh credentials. |
| `INSTAVISION_ANSWER_TIMEOUT` | `25` | Seconds to wait for a camera to answer the SDP offer. Short, so a camera that comes online is picked up quickly. |
| `INSTAVISION_HTTP_PORT` | `8655` | Status port. `GET /` returns 200 only when every enrolled camera is streaming, 503 otherwise, so it is usable as a container health check. |
| `INSTAVISION_HTTP_BIND` | `0.0.0.0` | Status bind address. |
| `INSTAVISION_STATE_DIR` | `./state` | Status logs and per-camera state. |
| `INSTAVISION_UI_PORT` | `8665` | Gateway UI port. |
| `INSTAVISION_UI_BIND` | loopback unless a password is set | UI bind address. |
| `INSTAVISION_UI_PASSWORD` | *(empty)* | Require HTTP basic auth on the UI and PTZ routes. |
| `INSTAVISION_GATEWAY_ANSWER_TIMEOUT` | `600` | How long the gateway holds a KVS offer before rebuilding the session. |
| `INSTAVISION_SNAPSHOT_TTL` | `30` | Snapshot cache, seconds. |
| `FFMPEG_PATH` | `ffmpeg` | ffmpeg binary. |
| `INSTAVISION_API`, `INSTAVISION_PARTNER`, `INSTAVISION_CLIENT_ID`, `INSTAVISION_KVS_REGION` | see `.env.example` | Vendor endpoints and client identity. Only change these if the vendor moves. |

## Command line

```bash
python3 relay.py --list                  # list the account's cameras and exit
python3 relay.py --once                  # one attempt per camera, then exit
python3 relay.py --device Cam3 --once    # probe a single camera end to end
python3 relay.py                         # run until stopped (what the container does)
```

Exit codes: `0` clean, `1` login or config failure, `3` incomplete `.env`.

## Self-test

Proves everything after the media arrives, with no camera involved:

```bash
docker compose up -d mediamtx
python3 -m venv .venv && .venv/bin/pip install aiortc==1.15.0 websockets==17.4
.venv/bin/python selftest.py
```

It generates a real H.264 clip, pushes it through the same `RtspPublisher` the
relay uses, then reads it back off MediaMTX and decodes frames from it:

```
publishing to rtsp://127.0.0.1:8574/insta_selftest
feeding 228 KB of H.264
reading it back (ffmpeg needs ~5 s to probe the stream) ...
  codec=h264 size=640x360 rate=15/1
PASS
```

Note the ~5 s: ffmpeg probes up to five seconds of input before it writes the RTSP
header, so a path appears about five seconds after a feed starts. That delay is
in the publishing path, not in the test.

## Diagnostics

| File | What it does |
| --- | --- |
| [`probe.py`](probe.py) | Signs in, walks the account, prints spaces, devices and raw device state. `--creds` dumps the credential response for one camera, `--space` picks a space. |
| [`kvs_viewer.py`](kvs_viewer.py) | The KVS WebRTC viewer on its own: `--device Cam3 --seconds 20`, `--all` to sweep every camera, `--save frame.jpg` to keep the first frame. |
| [`mqtt_probe.py`](mqtt_probe.py) | Tries the app's AWS IoT MQTT control channel and reports exactly how it is refused. |
| [`selftest.py`](selftest.py) | The republish round trip described above. |
| [`gateway.py`](gateway.py) | The web UI, snapshot proxy and PTZ routes; runs the relay in-process. |
| [`ui.html`](ui.html) | The page itself — plain HTML/CSS/JS, no build step. |

## Limitations

* **The camera must already be publishing.** See "What is and is not proven". The
  gateway holds a viewer offer on every camera, so one that starts publishing is
  picked up within seconds — but nothing here can start the camera itself.
* **No cloud plan on the test account.** `atlas/spaces/{space}/data` answers
  `Pool_NotFound` and `subscriptions` is empty, so there are no recorded clips to
  relay, only the 1-day snapshot bucket. Whether live view depends on a plan could
  not be determined from the client side.
* **Shared with the app.** KVS allows one master per channel — the camera — and
  any number of viewers, but a camera's uplink and viewer slots are finite. The
  relay is another viewer alongside your phone, so expect contention if you
  watch the same camera in the app while it is enrolled here.
* **Video only, H.264 only.** No audio, no re-encode.
* **Credentials expire in about five minutes**, so they are fetched per attempt
  rather than cached; an established WebRTC session survives past that.
* **Not official.** This is an independent, reverse-engineered client of a
  vendor's private API. It can break whenever they change anything, and using it
  is your call to make against their terms of service. Test it on your own
  cameras.

## License

GNU General Public License v3.0 — see [LICENSE.md](LICENSE.md).
