# InstaVision-Relay

Watch **InstaVision** (`ai.instaview.guardian`) Wi-Fi cameras through their AWS
Kinesis Video Streams WebRTC channels and republish them as plain **RTSP**, so
Frigate, Home Assistant, VLC or any other NVR can consume them like an ordinary
camera.

Two pieces: **relay.py** does the vendor session and the KVS viewer, and
**gateway.py** puts a small web app in front of it (viewer, enrollment, PTZ,
snapshots, copyable Frigate YAML). One compose project starts MediaMTX and the
gateway.

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

What it needs: Linux with Docker and compose v2, and these host ports free — **8574**,
**8908**, **8909**, **9998**, **8665**, **8656**. `ffmpeg` ships inside the image, so
running the stack needs nothing else on the host; the optional self-test below is the one
part that runs on the host and needs `ffmpeg` there. The cameras themselves need nothing:
they are reached through the vendor cloud.

```bash
git clone https://github.com/bobhuang1/instavision-kvs-relay.git
cd instavision-kvs-relay
cp .env.example .env      # then fill in the three values below
```

`state/` arrives with the clone and must stay: it is bind-mounted into the gateway, which
runs as `${INSTAVISION_UID}:${INSTAVISION_GID}` (default `1000:1000`) so the files it
writes belong to you. If that directory is missing, Docker creates it as `root` and the
gateway exits with `[state] cannot write …/state/session.key` instead of starting — the
fix is one line, either `mkdir -p state` as yourself or set `INSTAVISION_UID`/`INSTAVISION_GID`
in `.env` to `id -u` / `id -g`.

Fill in `.env`:

| Variable | What it is |
| --- | --- |
| `INSTAVISION_EMAIL` / `INSTAVISION_PASSWORD` | The account the relay signs in as. Use one you are willing to have a service sign in as — it will appear as a session in the app. |
| `INSTAVISION_FIREBASE_KEY` | Firebase Web API key of the vendor's project. Read it from the app's own configuration; it is a public client key, not a secret. |
| `INSTAVISION_ENROLLED` | Comma-separated camera names to publish. **Empty publishes nothing.** |

```bash
docker compose up -d
docker compose logs -f gateway     # Ctrl-C stops the logs, not the stack
```

That builds the image, starts MediaMTX and the gateway, which serves the UI on port 8665
and runs the relay inside itself, so the UI and the RTSP paths cannot drift apart. Open
`http://<host>:8665/`. The first visit asks you to create the administrator (username,
password, optional hint); setting `INSTAVISION_UI_PASSWORD` before the first start
creates it for you instead.

One host, two checkouts: the compose project is named `instavision-relay`, so a second
clone on the same machine does not run beside the first — `docker compose up` there
**recreates the first one's containers** with the second checkout's files. A different
project name alone is not enough for a parallel copy: MediaMTX's addresses are literals in
`mediamtx.yml` and `compose.yaml`, so a second stack on the same host dies immediately with
`ERR listen udp :8100: bind: address already in use` (measured 2026-10-31). Give the copy
its own MediaMTX config and point the gateway at it:

```yaml
# compose.test.yml - run with: docker compose -f compose.yaml -f compose.test.yml ...
services:
  mediamtx:
    volumes:
      - ./mediamtx.test.yml:/mediamtx.yml:ro
  gateway:
    environment:
      INSTAVISION_RTSP_BASE: rtsp://127.0.0.1:8674
      INSTAVISION_RELAY_STATUS_PORT: "8676"
      INSTAVISION_UI_PORT: "8675"
      INSTAVISION_WEBRTC_BASE: http://127.0.0.1:8919
      INSTAVISION_HLS_BASE: http://127.0.0.1:8918
```

`mediamtx.test.yml` is `mediamtx.yml` with `rtspAddress: :8674`, `apiAddress:
127.0.0.1:9990`, `rtpAddress: :8110`, `rtcpAddress: :8111`, `hlsAddress: :8918`,
`webrtcAddress: :8919` and `webrtcLocalUDPAddress: :8290`. Then:

```bash
COMPOSE_PROJECT_NAME=iv-test INSTAVISION_UI_PORT=8675 \
  docker compose -f compose.yaml -f compose.test.yml up -d
```

Verified on 2026-10-31 beside a live relay: the test stack came up on 8674/8675/8918/8919
with the live one still holding 8574/8665/8908/8909, `selftest.py` passed against it, and
an RTSP read over the LAN address returned `h264 640x360 15/1` while HLS served the same
path. Note that the gateway still needs a working account sign-in; without one it exits 3
with `could not sign in`, and `restart: always` turns that into a restart loop until `.env`
is filled in.

**Ports on a shifted copy:** 🔗 Streams & ports and `GET /api/streams` report the ports
the gateway is actually serving on, not the defaults. Each one is read back from the same
setting that decides where the relay publishes or where the WHEP/HLS proxies forward:

| Shown as | Derived from | Fallback |
| --- | --- | --- |
| Gateway (this page) | the port the HTTP server is bound to (`INSTAVISION_UI_PORT`) | 8665 |
| RTSP (MediaMTX) | `INSTAVISION_RTSP_BASE` | 8574 |
| HLS | `INSTAVISION_HLS_BASE` | 8908 |
| WebRTC / WHEP | `INSTAVISION_WEBRTC_BASE` | 8909 |

The `compose.test.yml` above therefore needs no further change: on 8674/8918/8919 the tab
and the API print 8674/8918/8919, and the per-camera `rtsp`/`hls`/`webrtc` URLs and the
generated Frigate `go2rtc:` block follow. Exercised through the real HTTP handler with that
environment on 2026-11-01 (one camera stubbed in, so only the port path was under test):

```
rtsp://192.168.1.30:8674/<path>
http://192.168.1.30:8918/<path>/index.m3u8
http://192.168.1.30:8919/<path>/
```

The one port that stays fixed is **`rtsp://127.0.0.1:8554`** in the generated Frigate
`cameras:` block. That is Frigate's own go2rtc inside the Frigate container, which the
stanza tells Frigate to read from — it is not a port this relay serves, so it does not
move with `INSTAVISION_RTSP_BASE`.

`--list` shows what the account holds (the profile keeps this from starting a second
set of streaming sessions):

```bash
# --profile goes before `run`: it is a flag of `docker compose`, not of `run`.
docker compose --profile relay run --rm relay python3 relay.py --list
```

Publishing is deliberately an allowlist: an empty `INSTAVISION_ENROLLED` means the
relay logs that and publishes nothing, so a camera cannot quietly start using
uplink. The gateway ignores that list and enrolls every camera on the account,
because its job is to be watching when a camera starts streaming.

## The gateway

The app is what makes the relay usable without a terminal: one administrator, four
tabs, live tiles, and the vendor's own PTZ.

| Tab | What it does |
| --- | --- |
| 📹 Viewer | Tile per enrolled camera. Plays MediaMTX WHEP inline when the camera streams, otherwise shows its latest cloud snapshot. Hover for the PTZ pad, mute, zoom (1×–3×) and fullscreen. |
| 📷 Cameras | Every camera on the account with thumbnail, IP, model, PTZ support and stream state; add or remove one from the relay. |
| 🔗 Streams & ports | The RTSP/HLS/WHEP/RTSP-to-Frigate addresses, per-camera URLs, and **Copy Frigate YAML**. |
| ⚙ Settings | Change the password, stop or start the camera sessions, sign out. |

**Sign-in.** First visit creates the administrator (username, password, optional
hint) and stores it in `state/auth.json`; sessions are signed cookies. Setting
`INSTAVISION_UI_PASSWORD` bootstraps that account on first start and doubles as HTTP
basic auth, so scripts can call the API without a cookie.

**Enrollment.** Adding a camera writes `state/enrolled.json` and restarts the relay
with the new list, which takes a few seconds; that is the one operation that
interrupts the other cameras, and it is deliberately explicit. An empty file enrolls
every camera on the account.

**Why a WHEP proxy.** The browser talks to `/whep/<name>` on the gateway, which
forwards to MediaMTX on 8909. Same origin, behind the session, and it keeps working
when the page is opened by IP or by hostname.

| Endpoint | What it does |
| --- | --- |
| `GET /` | The app (redirects to `/login` when signed out) |
| `GET /api/cameras` | Account cameras joined with relay state |
| `POST /api/cameras/<name>/enroll` | `{"enrolled": bool}` — add or remove from the relay |
| `GET /api/snapshot/<name>` | Latest cloud snapshot, cached (`INSTAVISION_SNAPSHOT_TTL`, default 30 s) |
| `POST /api/ptz/<name>` | `{"pan", "tilt", "nonStop"}` — the app's `MoveRequest` |
| `POST /api/ptz/<name>/stop` · `/reset` | Stop a move / return to preset |
| `GET /api/streams` | URLs, ports and generated Frigate YAML |
| `POST /whep/<name>` | WHEP proxy to MediaMTX |
| `GET /healthz` | Relay health alone, for a watchdog |

PTZ is not a guess: the app's own `DeviceRepository.ptzMove` posts to
`atlas/spaces/{space}/devices/{id}/ptz/move` with `MoveRequest{pan, tilt, nonStop}`,
and every camera advertises a `0xFC12 PTZ` cluster with pan and tilt in the range
`-10000..0`. The pad sends steps of 300 and always offers a stop button, because
`nonStop: true` keeps the head moving until told to stop.

**What the tiles are.** A tile says `LIVE` only when the relay is receiving frames,
`snapshot` when all it has is a still from the camera's own upload (minutes old), and
`offline` when the camera reports no address. It never calls a still live.

**Deliberately absent.** There is no router or firewall control and no vendor
server key to manage here, so no tab pretends to offer them; two-step sign-in is
not implemented either.

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

Every service on an NVR host tends to run with `network_mode: host`, so these ports
are chosen not to collide with anything else on the box:

| Service | RTSP | API | HLS | WebRTC |
| --- | --- | --- | --- | --- |
| Frigate's go2rtc | 8554/tcp | — | 1984 | 8555/udp |
| another camera bridge (MediaMTX) | 8564/tcp | 9997 | 8898 | 8899 |
| **InstaVision-Relay (MediaMTX)** | **8574/tcp** | **9998** | **8908** | **8909** |

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
| `INSTAVISION_UI_BIND` | `0.0.0.0` | Gateway bind address. |
| `INSTAVISION_UI_PASSWORD` | *(empty)* | Bootstrap administrator password, and HTTP basic auth for scripts. Without it the first visit offers to create the administrator. |
| `INSTAVISION_SESSION_TTL` | `604800` | Session cookie lifetime, seconds. |
| `INSTAVISION_UID` / `INSTAVISION_GID` | `1000` | Host user the container runs as, so it can write `./state`. |
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

Proves everything after the media arrives, with no camera involved. This is the one part
that runs on the **host**, not in a container, so it needs Python 3.11+ with `venv` and
`ffmpeg`/`ffprobe` on `PATH` — it exits `2` naming the missing binary rather than
pretending to pass:

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

## Load test

How much does one more camera cost? That depends on three layers, and they are
paid for separately:

| Layer | Where | Measured by |
| --- | --- | --- |
| **receive** | camera → KVS WebRTC → `aiortc` depayload | the gateway process, only with a camera that publishes |
| **publish** | H.264 access units → `ffmpeg -c copy` → MediaMTX | [`loadtest.py`](loadtest.py) |
| **fan-out** | MediaMTX path → one RTSP reader each (go2rtc, Frigate) | [`loadtest.py`](loadtest.py) |

```bash
python3 loadtest.py                         # 5, 8 and 12 synthetic streams
python3 loadtest.py 6 12 --seconds 60       # chosen concurrency levels
python3 loadtest.py --clip garage.mp4 4     # a real recording, same harness
python3 loadtest.py --real --seconds 120    # live cameras, publishes nothing
```

By default it publishes a generated clip through the relay's own `RtspPublisher`
and attaches an ffmpeg reader to each path, so what is measured is the code that
runs in production, not a model of it. `--real` skips publishing entirely: it
finds the `insta_*` paths MediaMTX is carrying right now, attaches
`--readers` (default 1) consumers to each, and reports what the actual cameras
cost — useful because bitrate, frame size and GOP structure all change the copy
and fan-out cost.

`--clip` accepts a raw Annex-B `.h264` or anything ffmpeg can open; containers are
remuxed once with `-c copy` (never re-encoded), so a recorded clip from one of
your own cameras is the cheapest honest test there is. Settings that are not
flags live in the environment: `INSTAVISION_LOADTEST_SIZE` (default `1280x720`),
`INSTAVISION_LOADTEST_FPS` (15), `INSTAVISION_LOADTEST_CLIP_SECONDS` (6),
`INSTAVISION_MEDIAMTX_API` (the path API), `INSTAVISION_RELAY_STATUS`, and
`INSTAVISION_FRIGATE_API` (optional — when reachable, Frigate's detector latency
and iGPU figures are sampled before and after each phase).

Results are printed as one JSON line per phase, a table at the end, and written to
`loadtest.json`. Exit codes: `0` measured, `2` nothing to measure (no live path in
`--real`), `3` MediaMTX's API unreachable.

On the four-core N95 this project was built on, with Frigate already running (so
the baseline is the *machine*, not the relay):

| Streams | system CPU | publisher | consumers | per stream | Mbit/s in | RSS |
| --- | --- | --- | --- | --- | --- | --- |
| baseline | 85.5% | — | — | — | — | — |
| 5 | 86.2% | 3.5% | 4.0% | **1.50%** | 21.7 | 537 MB |
| 8 | 85.7% | 5.4% | 6.1% | **1.44%** | 34.6 | 858 MB |
| 12 | 92.4% | 7.1% | 8.3% | **1.28%** | 51.2 | 1288 MB |

Cost per stream is flat, and the memory is almost entirely ffmpeg (`~107 MB` per
stream pair) rather than the relay, so the ceiling here is the fan-out readers, not
the copy. Frigate's detector latency stayed between 37 and 40 ms across the sweep
with zero dropped frames.

Two caveats on the absolute numbers. The generated clip runs at **4.3 Mbit/s per
camera**, which is a deliberate worst case — a recording from one of the real
1080p cameras here measures 2.1 Mbit/s — so the per-stream CPU is an upper bound.
And the baseline is 85%, not 2%, because this box was already running Frigate,
Home Assistant, Ollama, SQL Server and Postgres; what transfers to another machine
is the *increment*.
Use `--clip` with one of your own recordings when the figure has to be exact.

The one number this harness cannot produce is the receive layer: with no camera
publishing, `aiortc` never runs. `--real` closes that gap on a live system — it
accounts the gateway's CPU (found by scanning `/proc`, or `--pid` if the container
is not visible) alongside the readers, so the per-camera relay cost appears there.
That mode is **written but unproven**: no camera on the test account has ever
published, so it could only be checked against its no-live-camera path, which
fails with a clear message and exit code 2 rather than inventing a number.

## Diagnostics

| File | What it does |
| --- | --- |
| [`probe.py`](probe.py) | Signs in, walks the account, prints spaces, devices and raw device state. `--creds` dumps the credential response for one camera, `--space` picks a space. |
| [`kvs_viewer.py`](kvs_viewer.py) | The KVS WebRTC viewer on its own: `--device Cam3 --seconds 20`, `--all` to sweep every camera, `--save frame.jpg` to keep the first frame. |
| [`mqtt_probe.py`](mqtt_probe.py) | Tries the app's AWS IoT MQTT control channel and reports exactly how it is refused. |
| [`selftest.py`](selftest.py) | The republish round trip described above. |
| [`loadtest.py`](loadtest.py) | Concurrency harness: cost per stream for publishing and fan-out, synthetic or against live cameras. See "Load test". |
| [`gateway.py`](gateway.py) | The app: sessions, enrollment, snapshots, PTZ, WHEP proxy; runs the relay in-process. |
| [`ui.html`](ui.html) | The tabbed page — plain HTML/CSS/JS, no build step. |
| [`login.html`](login.html) | Sign-in and first-run administrator. |

Signalling is not silent any more. Everything inbound used to go through
`KvsViewer.log`, which is gated on `--verbose` — a flag the gateway never passes — so
a GO_AWAY, a refusal or a socket close was invisible in the running stack and every
failure read as `no SDP answer (camera is not publishing)`. Answers, GO_AWAYs, close
codes and any non-routine message type now print unconditionally (`[kvs <client>] …`),
and a closed socket is recorded as `signalling closed (code: reason)` on the session.
A camera that answers nobody therefore leaves a record saying so, instead of looking
like a timeout.

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
* **Video only; H.264 in practice.** No audio, no re-encode. The pipe demuxer is
  no longer assumed: `relay.py` asks the SDP answer what the camera agreed to send
  and starts ffmpeg with `-f h264` or `-f hevc` accordingly, refusing a codec it
  cannot copy as Annex-B (VP8/VP9/AV1) with a named error instead of feeding those
  bytes to the H.264 parser, which only ever produced `unspecified size` and a
  zero-frame session. What that refusal can catch today is narrow: aiortc 1.15
  offers H.264 and VP8 for video and has no H.265 support, so an H.265-only camera
  is rejected during negotiation rather than reaching the publisher. The mapping
  matters the moment that changes, and it makes the negotiated codec visible in
  the session log (`codec=h264 fmt=h264`).
* **Credentials expire in about five minutes**, so they are fetched per attempt
  rather than cached; an established WebRTC session survives past that.
* **Not official.** This is an independent, reverse-engineered client of a
  vendor's private API. It can break whenever they change anything, and using it
  is your call to make against their terms of service. Test it on your own
  cameras.

## License

GNU General Public License v3.0 — see [LICENSE.md](LICENSE.md).
