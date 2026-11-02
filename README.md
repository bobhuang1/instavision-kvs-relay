# InstaVision-Relay

> **STATUS: NOT A WORKING PROJECT. It has never carried a live frame from a real camera.**
>
> This repository is a reverse-engineering log with working software attached to it. The
> software itself is solid — it signs in, fetches real credentials, joins Kinesis Video
> Streams, negotiates WebRTC, copies H.264 into RTSP, and republishes a synthetic clip
> through the exact production classes. But the one thing it exists to do, *show you a
> camera*, it cannot do, and neither can the vendor's own phone app on this account.
>
> Read this page as a post-mortem before you read it as documentation. If you clone this
> expecting twelve RTSP cameras, you will get twelve paths that never appear and a UI that
> says `WAITING` forever. Section [**What is proven, and what is not**](#what-is-proven-and-what-is-not)
> is the honest summary; [**The investigation, in order**](#the-investigation-in-order)
> is every attempt, including the ones that wasted a day.

Twelve InstaVision (`ai.instaview.guardian`) Wi-Fi cameras on one account, all powered, all
answering ping on the LAN, none of them live in the vendor app and none of them reachable
any other way. The goal was to watch them through their AWS Kinesis Video Streams WebRTC
channels and republish as plain RTSP so Frigate and Home Assistant could consume them.

That did not happen, and **the failure is not in this code**. As of 2026-11-02 no camera on
the account is present in its own KVS channel at all, so there is no master to answer an
offer. Until that changes on the camera side, nothing in this repository can produce video.

---

## What is proven, and what is not

| Half | Status | Evidence |
| --- | --- | --- |
| Vendor login, per-camera credentials, KVS signalling | **Works** | Fetches real credentials for every device on the account, repeatedly, across days |
| KVS viewer join (WebRTC signalling reaches AWS and is accepted) | **Works** | Viewer connects, sends offers, receives responses from the service; `loopback_check.py` proves the envelope is delivered to a peer on the same channel |
| **Republish path** (H.264 → ffmpeg → RTSP → MediaMTX) | **Works** | `selftest.py` pushes a generated 640×360 clip through the real publisher classes and reads it back off MediaMTX as `h264 640x360 15/1`, decoded frame for frame |
| **Cloud snapshots** | **Works** | Every camera returns a 640×360 JPEG from its `snapshot_url`, timestamp burned in, minutes old |
| **PTZ** | Commands accepted, effect unverified | `POST …/ptz/move` returns 200; the head cannot be confirmed to move, because the only view is a snapshot that refreshes on its own schedule |
| **A camera answering a viewer offer** | **Broken — the blocker** | Cameras are absent from their channels entirely (2026-11-01/02). No SDP answer, no ICE, no `recv` of any kind from AWS |
| **Live video end to end** | **Never achieved** | No frame from a real camera has ever arrived |

The relay cannot *make* a camera stream. The phone app was decompiled to confirm the app
does nothing more than this code does: same `stream-credentials` call, same signed
`wss://…?X-Amz-ChannelARN=…&X-Amz-ClientId=…` URL, no start-stream call in its entire
endpoint table. Both the app and this relay are viewers waiting on a master that is not
there — which is exactly why the app's live view hangs too.

---

## The wire model (this part is fully mapped)

Everything below was read out of the APK and confirmed against live responses.

```
Firebase sign-in  →  POST atlas/users/session
                  →  GET  atlas/spaces/{space}/stream-credentials?device_id=…
                        → AWS keys, channel ARN, wss endpoint, TURN
Camera  → joins KVS as MASTER   (no X-Amz-ClientId)
App/relay → joins KVS as VIEWER (X-Amz-ClientId present)
Viewer  → sends {action, messagePayload, senderClientId} SDP offer, refreshed ~10 s
Master  → answers → H.264 over WebRTC (STUN stun.kinesisvideo.<region>.amazonaws.com, TURN)
```

Three details cost real time and are worth stating for anyone repeating this:

* **The envelope is asymmetric.** Outbound is `{"action": …, "messagePayload": …,
  "senderClientId": …}`; inbound is `{"messageType": …}`. Sending `messageType` outbound
  gets a silently accepted message that is never routed — no error, no answer.
* **Viewer vs master is decided by `X-Amz-ClientId`.** Present = viewer, absent = master.
  The vendor's credentials are scoped to `kinesisvideo:ConnectAsViewer` and reject the
  master form with `HTTP 400 "ClientId must be specified for ConnectAsViewer"`.
* **KVS answers unroutable messages with 0-byte frames**, not errors. Any client that
  parses every frame as JSON dies on the first one.

---

## The investigation, in order

Kept in the order it happened, because the order is the point: each step looked like
progress and three of them turned out to be wrong.

### 2026-10-28 — "the camera only streams when you watch it in the app"

Eight cameras swept, then a single viewer offer held open for ten minutes. Result: no SDP
answer, no ICE candidate, no inbound bytes from AWS. The theory of the time — that these
are on-demand devices woken by the phone — was later disproved by the camera's own log.

### 2026-10-29/30 — "wake it over the IoT control channel"

`stream-credentials` also returns `mqtt: {endpoint, client_id_prefix}`. Every attempt to use
it failed, and the failures were conclusive:

| Attempt | Result |
| --- | --- |
| `iot:Connect` with app-style client ids (six shapes tried) | `403` |
| SigV4-presigned MQTT-over-WebSocket to the ATS broker | `403 {"message":"Forbidden"}` |
| `iotdata` shadow read | `403` |
| `iot:DescribeEndpoint`, `iot:ListThings` | `403` — *"no identity-based policy allows the … action"* |

The credentials resolve to `arn:aws:sts::<account>:assumed-role/BackendServiceRole/<space>`.
Backend role, backend resources, no IoT permissions: the wake path is closed to this code,
and per the camera's log it is scoped to the camera's own client id `<spaceId>_<deviceId>`
anyway. **Dead end.**

### 2026-10-30 — "join as master ourselves"

Blocked by KVS itself, not by the vendor: the issued credentials only allow
`ConnectAsViewer`, and the master form is refused. **Dead end** — but it accidentally
produced the most useful tool in the repo (see the presence oracle, below).

### 2026-10-31 — "execute code on the camera through the SD card"

A probe card with `probe_hook.sh`, three speculative root hooks, `tuya\upg\.tyupg.cfg`,
`wifi\config.sh` and a `log_env.cfg` was inserted and the camera power-cycled for three
minutes. **All four hooks failed.** The firmware does walk the card and does open
`log_env.cfg` — it logs the path every 17 seconds — but the file is read as key/value: the
`LOG_LEVEL_*=DEBUG` lines produced no debug output at all. No flash dump, no shell.
Rooting needs the SOP8 flash chip, which nobody has touched yet. **Dead end** for the card.

### 2026-10-31 — what the card gave up anyway

The firmware writes its own debug log to `ivlogs/` with no preparation, and that log
retired several wrong beliefs:

* The camera connects as **master 2.2 s after login and stays connected**, so the "on-demand
  device" theory above was wrong. When nothing plays, the camera was present and idle.
* The cloud API is `api-us.instaview.ai` with `Client-ID: firmware`, a device-scoped
  password login (`/atlas/devices/login`), and a fleet-provisioning **claim certificate** for
  AWS. The AWS half of the camera's boot is separate from the HTTPS half.
* `CVR` records 1080p continuously to the card at ~900 kbps / 12 fps, so the encoder's real
  output is known: a 640×480 negotiation would have been our side of the handshake.
* The camera's own clock runs 8 hours ahead of Pacific in its log filenames — a trap when
  matching footage to events.

**This log was the best evidence of the whole project, and it came from a failed exploit.**
It is also a security finding in its own right: the card prints the home Wi-Fi passphrase and
a 30-day device credential in cleartext on every boot.

### 2026-11-01 — "prove whether a camera is in its channel at all"

`join_oracle.py` takes the master slot for ~0.4 s, which is granted only when nobody else
holds it. A granted probe therefore proves *absence*. Run against Cam10, Cam11 and Cam12:
slot free on all three, seven offers over ~3 minutes, nothing answered. That superseded the
"master connected from boot" reading, which had been true at 10:40 that morning and was not
true by the evening.

`loopback_check.py` then put a viewer and a master **on the same channel** to separate
"cannot reach KVS" from "cannot reach that camera": `viewer->v delivered`,
`viewer->m not delivered`. Our signalling is fine; the peer is missing.

### 2026-11-02 — "is the vendor side broken, or are these cameras broken?"

Six read-only checks, all against the vendor's own API, to stop guessing:

| Check | Result |
| --- | --- |
| `GET /atlas/time` on the cameras' host (`api-us.instaview.ai`) | `200 {"epoch_milli":…}` in 130 ms — **the cloud is healthy** |
| `POST /atlas/devices/login` with a bogus device id | inner `404 Device registry not found` |
| Same, with each of the account's twelve real device ids and a wrong key | inner `400 invalid access key` for **all twelve** — the registry lookup succeeded, so **nothing was deregistered or migrated** |
| SSID and firmware across the fleet | All twelve on the same SSID, all on firmware `3.53.251` — **no split-network story** |
| Space `subscription_details` / `sub_details` / `monitoring` | all `null`; every device `Activated` — **no plan gate** |
| `last_seen` per camera | Most are 2–98 days stale; Cam11 and Cam4 checked in within ~2 h, while **none of the twelve is in its KVS channel** |

The last row is the live contradiction and the most useful result in the project: **cameras
do still reach the vendor cloud and authenticate, yet none of them joins Kinesis Video.** The
break is on the camera's AWS half — claim credentials, `getSignalingChannelEndpoint`, or
`ConnectAsMaster` — after a successful atlas login. A camera rebooted on 2026-11-02 did not
even update `last_seen`, so at least one camera is failing before it gets that far.

That is where the project stops. Nothing client-side can fix it.

---

## Where it fails, concretely

1. **No camera is in its channel.** Presence oracle: master slot free on every camera tried,
   repeatedly, across two days. No master means nothing to answer any offer, including the
   phone app's.
2. **The camera's AWS path is broken but its HTTPS path is not.** Atlas login still works
   from the camera side (`last_seen` advances), KVS does not.
3. **The wake channel is unreachable from here** (IoT 403, six client-id shapes) and
   irrelevant anyway — the camera is already awake.
4. **The camera cannot be instrumented** (no SD execution, no open ports, no root), so the
   failing AWS call can only be read from a card pulled after a boot.
5. **The app fails identically**, which rules out this code as the cause and rules in
   something server- or camera-side.

## Open questions, unanswered

* Does the camera's claim-credential fetch fail, or does it succeed and the KVS endpoint/
  `ConnectAsMaster` fail? The card log from a *current* boot would say in one line.
* Did the vendor rotate device access keys or change fleet provisioning? The registry still
  knows every device, and a wrong key is rejected the same way a rotated one would be.
* Is any camera on the account publishing right now? If one comes back, the relay should
  pick it up within seconds — the gateway holds an offer on every camera for exactly that.
* Is this account-wide or fleet-wide? Only one account was available to test.

---

## What actually runs

Everything below is real and tested; it is just downstream of a camera that never arrives.

**The relay** signs in, fetches per-camera credentials, joins KVS as a viewer, and copies
H.264 access units (never decoding them) into `ffmpeg -c copy` → RTSP → MediaMTX. One
camera costs a few percent of a core instead of most of one.

**The gateway** is the app in front of it: one administrator, four tabs, live tiles,
enrollment, the vendor's own PTZ, snapshots, copyable Frigate YAML, a WHEP proxy. A tile
says `LIVE` only when frames arrive, `snapshot` when all it has is a minutes-old still from
the camera's own upload, and `WAITING` otherwise — **and on this account it has only ever
said the last two.** It never calls a still live.

**The load test** measures the publishing and fan-out layers with synthetic streams. On the
four-core N95 this was built on, with Frigate already running: 1.28–1.50 % CPU per stream,
flat from 5 to 12 streams, memory almost entirely ffmpeg. Honest caveat: the clip is
4.3 Mbit/s per camera, a deliberate worst case, and the **receive** layer — camera → KVS →
`aiortc` — is unmeasured, because no camera has ever published.

## Install and run

Needs Linux with Docker and compose v2, and ports **8574**, **8908**, **8909**, **9998**,
**8665**, **8656** free. `ffmpeg` ships in the image. The cameras need nothing — they are
reached through the vendor cloud.

```bash
git clone https://github.com/bobhuang1/instavision-kvs-relay.git
cd instavision-kvs-relay
cp .env.example .env      # then fill in the values below
docker compose up -d
docker compose logs -f gateway     # Ctrl-C stops the logs, not the stack
```

`state/` arrives with the clone and must stay: it is bind-mounted into the gateway, which
runs as `${INSTAVISION_UID}:${INSTAVISION_GID}`. If it is missing, Docker creates it as
`root` and the gateway exits with `[state] cannot write …/state/session.key`.

| Variable | What it is |
| --- | --- |
| `INSTAVISION_EMAIL` / `INSTAVISION_PASSWORD` | The account the relay signs in as. It will appear as a session in the app. |
| `INSTAVISION_FIREBASE_KEY` | Firebase Web API key of the vendor's project (public client key, not a secret). |
| `INSTAVISION_ENROLLED` | Comma-separated camera names to publish. **Empty publishes nothing.** |

One host, two checkouts: the compose project is named `instavision-relay`, so a second clone
recreates the first's containers, and MediaMTX's ports are literals — a parallel stack needs
its own `mediamtx.test.yml` and shifted ports (`8674`/`8675`/`8918`/`8919`). This was
verified beside a live relay on 2026-10-31.

### Ports

| Service | RTSP | API | HLS | WebRTC |
| --- | --- | --- | --- | --- |
| Frigate's go2rtc | 8554/tcp | — | 1984 | 8555/udp |
| **InstaVision-Relay (MediaMTX)** | **8574/tcp** | **9998** | **8908** | **8909** |

Gateway UI + API on **8665**, relay health alone on **8656**. Publishing is localhost-only;
reading is open to the LAN.

### Diagnostics

| File | What it does |
| --- | --- |
| `probe.py` | Signs in, walks the account, prints spaces, devices and raw device state. |
| `kvs_viewer.py` | The KVS viewer alone: `--device Cam3 --seconds 20`, `--all`, `--save frame.jpg`. |
| `mqtt_probe.py` | Tries the IoT control channel and reports exactly how it is refused. |
| `selftest.py` | The republish round trip — the only end-to-end pass in the repo. |
| `loadtest.py` | Cost per stream for publishing and fan-out, synthetic or against live paths. |
| `gateway.py` / `ui.html` | The app and its page. |

```bash
docker compose up -d mediamtx
python3 -m venv .venv && .venv/bin/pip install aiortc==1.15.0 websockets==17.4
.venv/bin/python selftest.py        # prints PASS; this is the only thing that can pass today
```

## Limitations

* **The camera must already be publishing, and none is.** This is the whole failure.
* **No cloud plan on the test account.** `atlas/spaces/{space}/data` answers
  `Pool_NotFound` and `subscriptions` is empty, so there are no recorded clips to relay,
  only the 1-day snapshot bucket. Whether live view depends on a plan was not determined —
  though `subscription_details`/`sub_details` being `null` on a working-looking account
  suggests it does not gate streaming.
* **Shared with the app.** KVS allows one master — the camera — and many viewers, but a
  camera's uplink is finite. The relay is another viewer beside your phone.
* **Video only, H.264 only.** No audio, no re-encode.
* **Credentials expire in ~5 minutes**, so they are fetched per attempt; a session that is
  already up survives past that.
* **Not official.** An independent, reverse-engineered client of a private API. It can break
  whenever the vendor changes anything, and using it is your call against their terms.

## Verdict

The client side of this problem is solved: the wire model is complete, the signalling is
proven, the republish path is proven, and the remaining fault has been narrowed to the
camera's own AWS credential/join path — the same fault that breaks the vendor's app. Two
things would move it: a card pulled from a camera after a fresh boot, to read which AWS call
fails, and a support ticket armed with the presence-oracle result. Neither is code, which is
why this repository is a log and not a product.

## License

GNU General Public License v3.0 — see [LICENSE.md](LICENSE.md).
