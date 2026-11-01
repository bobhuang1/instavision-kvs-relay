#!/usr/bin/env python3
"""Measure what the relay costs at N concurrent cameras.

A streaming camera passes through three layers, and they are paid for separately:

    receive    camera -> KVS WebRTC -> aiortc depayload      (in gateway.py, per camera)
    publish    H.264 access units -> ffmpeg -c copy -> MediaMTX  (relay.RtspPublisher)
    fan-out    MediaMTX path -> one RTSP reader each         (go2rtc / Frigate / VLC)

The middle and last layers can be driven without a camera that publishes, which is
what the default mode does: it publishes a generated clip through the relay's own
`RtspPublisher` and attaches an ffmpeg reader to each path, so the numbers are the
real cost of the code that will run in production rather than a model of it.

`--real` measures actual cameras instead. It publishes nothing; it finds the
`insta_*` paths MediaMTX is currently carrying and attaches readers to those, so
the CPU and bitrate figures reflect real resolution, real bitrates and real GOP
structure. Use it when the gateway reports cameras streaming:

    python3 loadtest.py                        # 5, 8, 12 synthetic streams
    python3 loadtest.py 6 12 --seconds 60      # chosen counts
    python3 loadtest.py --clip garage.mp4 4    # a real recording, same harness
    python3 loadtest.py --real --seconds 120   # live cameras only

`--clip` accepts any ffmpeg-readable file: a raw Annex-B `.h264` is fed straight
in, anything else is remuxed to Annex-B once (no re-encode). Feeding a recording
from one of your own cameras is the cheapest honest load test there is, because
the bitrate and frame size are the ones Frigate will actually have to carry.

Exit codes: 0 a measurement was produced, 2 nothing to measure (no live path in
--real, or ffmpeg could not be run), 3 MediaMTX's API could not be reached.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

RTSP_BASE = (os.environ.get("INSTAVISION_RTSP_BASE") or "rtsp://127.0.0.1:8574").rstrip("/")
# MediaMTX's control API: apiAddress in mediamtx.yml.
MEDIAMTX_API = (os.environ.get("INSTAVISION_MEDIAMTX_API")
                or "http://127.0.0.1:9998").rstrip("/")
# The relay's health listener, used in --real to report what it thinks it is doing.
RELAY_STATUS = (os.environ.get("INSTAVISION_RELAY_STATUS")
                or "http://127.0.0.1:8656").rstrip("/")
# Optional: Frigate's own sampler, for "did the relay cost the detector/GPU".
FRIGATE_API = (os.environ.get("INSTAVISION_FRIGATE_API")
               or "http://127.0.0.1:5000").rstrip("/")

CLIP_SIZE = os.environ.get("INSTAVISION_LOADTEST_SIZE") or "1280x720"
CLIP_RATE = int(os.environ.get("INSTAVISION_LOADTEST_FPS") or 15)
CLIP_SECONDS = int(os.environ.get("INSTAVISION_LOADTEST_CLIP_SECONDS") or 6)

# Paths this harness creates, so --real never mistakes its own load for a camera.
SYNTHETIC_PREFIX = "insta_load"
HZ = os.sysconf("SC_CLK_TCK")


# ---------------------------------------------------------------------------
# relay plumbing
# ---------------------------------------------------------------------------
def load_relay():
    spec = importlib.util.spec_from_file_location("relay", os.path.join(HERE, "relay.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def http_json(url: str, timeout: float = 5) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.load(resp)


def paths() -> dict:
    """MediaMTX's view of every path. Raises rather than pretending it is empty."""
    data = http_json(f"{MEDIAMTX_API}/v3/paths/list")
    return {item.get("name"): item for item in data.get("items") or []}


def relay_status() -> dict:
    """The relay's health listener. Its /status answers 503 while cameras are down,
    which still carries a body worth reporting, so read that rather than give up."""
    try:
        return http_json(f"{RELAY_STATUS}/status")
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read() or b"{}")
        except ValueError:
            return {}
    except (OSError, ValueError, urllib.error.URLError):
        return {}


def frigate_stats() -> dict:
    """Frigate's own view: detector latency, iGPU busy/decode, dropped frames.

    The GPU has no perf counters exposed on this kernel (`intel_gpu_top` aborts),
    so where Frigate is present its sampler is the instrument for "did this cost
    the GPU". Absent Frigate the figures are simply omitted.
    """
    try:
        data = http_json(f"{FRIGATE_API}/api/stats")
    except (OSError, ValueError, urllib.error.URLError):
        return {}

    def pct(value):
        try:
            return float(str(value).rstrip("%"))
        except (TypeError, ValueError):
            return None

    gpu = (data.get("gpu_usages") or {}).get("N95 iGPU") or {}
    cameras = data.get("cameras") or {}
    first = next(iter((data.get("detectors") or {}).values()), {})
    return {
        "inference_ms": first.get("inference_speed"),
        "gpu_pct": pct(gpu.get("gpu")),
        "gpu_dec_pct": pct(gpu.get("dec")),
        "camera_fps": round(sum((c.get("camera_fps") or 0) for c in cameras.values()), 1),
        "dropped_fps": round(sum((c.get("skipped_fps") or 0) for c in cameras.values()), 1),
    }


# ---------------------------------------------------------------------------
# process accounting
# ---------------------------------------------------------------------------
def cpu_jiffies(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/stat") as handle:
            fields = handle.read().rsplit(")", 1)[1].split()
        return int(fields[11]) + int(fields[12])
    except (OSError, IndexError, ValueError):
        return 0


def cpu_delta(start: dict[int, int], group: list[int]) -> float:
    """CPU seconds burned by a group since it was sampled.

    A process that has exited reads as 0, which would make its delta negative;
    such a PID is skipped rather than counted backwards (its CPU is then
    under-reported, but a reader that dies mid-phase is already a failure that
    shows up in `reader_errors`).
    """
    total = 0
    for pid in group:
        now = cpu_jiffies(pid)
        if now:
            total += max(0, now - start.get(pid, 0))
    return total / HZ


def rss_mb(pid: int) -> float:
    try:
        with open(f"/proc/{pid}/statm") as handle:
            pages = int(handle.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE") / 1e6
    except (OSError, IndexError, ValueError):
        return 0.0


def ppid(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/stat") as handle:
            return int(handle.read().rsplit(")", 1)[1].split()[1])
    except (OSError, IndexError, ValueError):
        return 0


def pids() -> list[int]:
    return [int(name) for name in os.listdir("/proc") if name.isdigit()]


def find_gateway() -> int | None:
    """The gateway process, if this is running on the host the relay runs on."""
    for pid in pids():
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as handle:
                argv = handle.read().split(b"\0")
        except OSError:
            continue
        if any(arg.endswith(b"gateway.py") for arg in argv):
            return pid
    return None


def family(pid: int) -> list[int]:
    """A process and its direct children - the relay plus its ffmpeg publishers."""
    everyone = pids()
    return [pid] + [child for child in everyone if ppid(child) == pid]


def system_cpu() -> tuple[int, int]:
    with open("/proc/stat") as handle:
        parts = handle.readline().split()[1:]
    values = [int(v) for v in parts]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(values), idle


def utilisation(sample) -> float:
    start, end = sample
    total = end[0] - start[0]
    idle = end[1] - start[1]
    return round(100 * (1 - idle / max(1, total)), 1)


# ---------------------------------------------------------------------------
# the clip
# ---------------------------------------------------------------------------
def probe_seconds(path: str) -> float | None:
    """Playback duration of a media file, or None when the container omits it."""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, timeout=20)
        value = float(proc.stdout.strip())
        return value if value > 0.5 else None
    except (subprocess.SubprocessError, ValueError):
        return None


def clip_bytes(path: str | None, workdir: str) -> tuple[bytes, str, float]:
    """H.264 Annex-B to publish, where it came from, and how long it plays for."""
    if path:
        if not os.path.exists(path):
            raise SystemExit(f"clip not found: {path}")
        if os.path.splitext(path)[1].lower() in (".h264", ".264"):
            seconds = probe_seconds(path) or float(CLIP_SECONDS)
            return open(path, "rb").read(), path, seconds
        # Any container: remux once, never re-encode, so bitrate is preserved.
        cached = os.path.join(workdir, "clip-from-file.h264")
        if not os.path.exists(cached):
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", path,
                 "-an", "-c:v", "copy", "-bsf:v", "h264_mp4toannexb", "-f", "h264", cached],
                check=True,
            )
        # Timing comes from the original container, which always knows its duration.
        return (open(cached, "rb").read(), f"{path} (remuxed to Annex-B)",
                probe_seconds(path) or float(CLIP_SECONDS))

    cached = os.path.join(workdir, f"clip-{CLIP_SIZE}-{CLIP_RATE}fps.h264")
    if not os.path.exists(cached):
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
             "-i", f"testsrc2=size={CLIP_SIZE}:rate={CLIP_RATE}", "-t", str(CLIP_SECONDS),
             "-c:v", "libx264", "-preset", "ultrafast", "-g", str(CLIP_RATE),
             "-f", "h264", cached],
            check=True,
        )
    return (open(cached, "rb").read(),
            f"generated testsrc2 {CLIP_SIZE}@{CLIP_RATE}fps, {CLIP_SECONDS}s",
            float(CLIP_SECONDS))


# ---------------------------------------------------------------------------
# readers
# ---------------------------------------------------------------------------
def start_reader(url: str) -> subprocess.Popen:
    return subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
         "-rtsp_transport", "tcp", "-i", url, "-c", "copy", "-f", "null", "-"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )


def stop_readers(readers: list[subprocess.Popen]) -> list[str]:
    """Terminate readers and report the ones that failed."""
    problems = []
    for reader in readers:
        if reader.poll() not in (None, 0):
            problems.append(f"exit {reader.poll()}")
    for reader in readers:
        if reader.poll() is None:
            reader.terminate()
    for reader in readers:
        try:
            reader.wait(timeout=5)
        except subprocess.TimeoutExpired:
            reader.kill()
    for reader in readers:
        try:
            if reader.stderr is not None:
                text = (reader.stderr.read() or b"").decode(errors="replace").strip()
            else:
                text = ""
        except OSError:
            text = ""
        if text and not problems:
            problems.append(text.splitlines()[0][:160])
    return problems


# ---------------------------------------------------------------------------
# synthetic mode
# ---------------------------------------------------------------------------
def run_phase(relay, n: int, seconds: int, clip: bytes, clip_seconds: float,
              baseline: float) -> dict:
    # Feed at the clip's own playback rate. Publishing faster than real time would
    # measure a bitrate no camera produces, and slower understates what a camera
    # actually costs, so chunk size and interval are derived from the duration.
    writes_per_second = 20
    step = max(1, int(len(clip) / clip_seconds / writes_per_second))
    stop = threading.Event()
    publishers, feeders = [], []

    # RtspPublisher prepends "insta_", so the published path is insta_load{i}.
    wanted = [relay.slugify(f"load{i}") for i in range(n)]

    for i in range(n):
        publisher = relay.RtspPublisher(f"load{i}", RTSP_BASE)
        publisher.start()
        publishers.append(publisher)

        def feed(pub=publisher):
            while not stop.is_set():
                for pos in range(0, len(clip), step):
                    if stop.is_set():
                        return
                    pub.write(clip[pos:pos + step])
                    time.sleep(1 / writes_per_second)

        thread = threading.Thread(target=feed, daemon=True)
        thread.start()
        feeders.append(thread)

    # Wait for the paths before attaching consumers, the way go2rtc does - ffmpeg
    # probes up to five seconds before it writes the RTSP header.
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            present = paths()
        except (OSError, ValueError, urllib.error.URLError):
            present = {}
        if all(name in present for name in wanted):
            break
        time.sleep(1)

    readers = [start_reader(f"{RTSP_BASE}/{name}") for name in wanted]
    time.sleep(8)                                    # let readers settle

    pub_pids = [p.proc.pid for p in publishers if p.proc]
    read_pids = [r.pid for r in readers]
    start_cpu = {pid: cpu_jiffies(pid) for pid in pub_pids + read_pids}
    before_paths = paths()
    frig_before = frigate_stats()
    sample_start = system_cpu()
    t0 = time.time()

    time.sleep(seconds)

    elapsed = time.time() - t0
    sample_end = system_cpu()
    after_paths = paths()

    pub_cpu = cpu_delta(start_cpu, pub_pids)
    read_cpu = cpu_delta(start_cpu, read_pids)
    received = 0
    for name in wanted:
        received += max(0, (after_paths.get(name) or {}).get("bytesReceived", 0)
                        - (before_paths.get(name) or {}).get("bytesReceived", 0))

    # Sample memory before anything is torn down: /proc entries vanish with the PIDs.
    child_rss = round(sum(rss_mb(pid) for pid in pub_pids + read_pids), 1)
    problems = stop_readers(readers)
    stop.set()
    for thread in feeders:
        thread.join(timeout=2)
    for publisher in publishers:
        publisher.stop()

    return {
        "mode": "synthetic",
        "cameras": n,
        "seconds": round(elapsed, 1),
        "baseline_cpu_pct": round(baseline, 1),
        "system_cpu_pct": utilisation((sample_start, sample_end)),
        "publisher_cpu_pct": round(100 * pub_cpu / elapsed, 1),
        "consumer_cpu_pct": round(100 * read_cpu / elapsed, 1),
        "per_camera_cpu_pct": round(100 * (pub_cpu + read_cpu) / elapsed / n, 2) if n else 0.0,
        "mbit_in": round(received * 8 / elapsed / 1e6, 2),
        "child_rss_mb": child_rss,
        "load1": open("/proc/loadavg").read().split()[0],
        "reader_errors": problems,
        "frigate_before": frig_before,
        "frigate_after": frigate_stats(),
    }


# ---------------------------------------------------------------------------
# real-camera mode
# ---------------------------------------------------------------------------
def live_paths(settle: float = 4.0) -> dict[str, float]:
    """Paths that are actually receiving bytes, with their incoming bitrate."""
    first = paths()
    time.sleep(settle)
    second = paths()
    live = {}
    for name, item in second.items():
        if name.startswith(SYNTHETIC_PREFIX) or name.startswith("insta_selftest"):
            continue
        before = (first.get(name) or {}).get("bytesReceived") or 0
        moved = (item.get("bytesReceived") or 0) - before
        if moved > 0:
            live[name] = moved * 8 / settle / 1e6
    return live


def run_real(paths_live: dict[str, float], readers_each: int, seconds: int,
             relay_pid: int | None, baseline: float) -> dict:
    cameras = sorted(paths_live)
    readers = []
    for name in cameras:
        for _ in range(readers_each):
            readers.append((name, start_reader(f"{RTSP_BASE}/{name}")))
    time.sleep(8)                                    # let readers settle

    relay_pids = family(relay_pid) if relay_pid else []
    read_pids = [reader.pid for _name, reader in readers]
    start_reader_cpu = {pid: cpu_jiffies(pid) for pid in read_pids}
    start_relay_cpu = {pid: cpu_jiffies(pid) for pid in relay_pids}
    before = paths()
    frig_before = frigate_stats()
    sample_start = system_cpu()
    t0 = time.time()

    time.sleep(seconds)

    elapsed = time.time() - t0
    sample_end = system_cpu()
    after = paths()

    read_cpu = cpu_delta(start_reader_cpu, read_pids)
    relay_cpu = cpu_delta(start_relay_cpu, relay_pids)

    per_camera = {}
    for name in cameras:
        moved = max(0, (after.get(name) or {}).get("bytesReceived", 0)
                    - (before.get(name) or {}).get("bytesReceived", 0))
        per_camera[name] = round(moved * 8 / elapsed / 1e6, 2)

    child_rss = round(sum(rss_mb(pid) for pid in read_pids + relay_pids), 1)
    problems = stop_readers([reader for _name, reader in readers])

    return {
        "mode": "real",
        "cameras": len(cameras),
        "readers_each": readers_each,
        "seconds": round(elapsed, 1),
        "baseline_cpu_pct": round(baseline, 1),
        "system_cpu_pct": utilisation((sample_start, sample_end)),
        "mbit_in_per_camera": per_camera,
        "consumer_cpu_pct": round(100 * read_cpu / elapsed, 1),
        "per_camera_consumer_cpu_pct": (round(100 * read_cpu / elapsed / len(cameras), 2)
                                        if cameras else 0.0),
        "relay_cpu_pct": round(100 * relay_cpu / elapsed, 1) if relay_pids else None,
        "per_camera_relay_cpu_pct": (round(100 * relay_cpu / elapsed / len(cameras), 2)
                                     if relay_pids and cameras else None),
        "relay_pid": relay_pid,
        "child_rss_mb": child_rss,
        "load1": open("/proc/loadavg").read().split()[0],
        "reader_errors": problems,
        "frigate_before": frig_before,
        "frigate_after": frigate_stats(),
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def baseline_cpu(seconds: float = 10.0) -> float:
    start = system_cpu()
    time.sleep(seconds)
    return utilisation((start, system_cpu()))


def summarise(results: list[dict]) -> None:
    print("\n== summary ==")
    if results and results[0]["mode"] == "real":
        print(f"{'cams':>5} {'sys%':>6} {'relay%':>7} {'consume%':>9} {'Mbit/s':>7} "
              f"{'RSS MB':>7} {'load':>5}")
        for r in results:
            relay = "-" if r["relay_cpu_pct"] is None else f"{r['relay_cpu_pct']:.1f}"
            mbit = sum(r["mbit_in_per_camera"].values())
            print(f"{r['cameras']:>5} {r['system_cpu_pct']:>6} {relay:>7} "
                  f"{r['consumer_cpu_pct']:>9} {mbit:>7.2f} {r['child_rss_mb']:>7} "
                  f"{r['load1']:>5}")
        return
    print(f"{'cams':>5} {'sys%':>6} {'publish%':>9} {'consume%':>9} {'per-cam%':>9} "
          f"{'Mbit/s':>7} {'RSS MB':>7} {'load':>5}")
    for r in results:
        print(f"{r['cameras']:>5} {r['system_cpu_pct']:>6} {r['publisher_cpu_pct']:>9} "
              f"{r['consumer_cpu_pct']:>9} {r['per_camera_cpu_pct']:>9} "
              f"{r['mbit_in']:>7.2f} {r['child_rss_mb']:>7} {r['load1']:>5}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Load-test the relay's publish and fan-out layers.")
    ap.add_argument("counts", nargs="*", type=int, default=[5, 8, 12],
                    help="concurrent streams per phase (synthetic mode only)")
    ap.add_argument("--seconds", type=int, default=45, help="measurement window per phase")
    ap.add_argument("--real", action="store_true",
                    help="measure live insta_* paths instead of publishing a clip")
    ap.add_argument("--readers", type=int, default=1,
                    help="readers per camera in --real (go2rtc-style fan-out)")
    ap.add_argument("--clip", help="raw .h264 or any ffmpeg-readable recording to publish")
    ap.add_argument("--pid", type=int, help="relay process to account CPU to (--real)")
    ap.add_argument("--json", default="loadtest.json", help="where to write the results")
    args = ap.parse_args()

    for binary in ("ffmpeg", "ffprobe"):
        if not shutil.which(binary):
            print(f"{binary} not found in PATH", file=sys.stderr)
            return 2

    workdir = os.path.join(tempfile.gettempdir(), "instavision-loadtest")
    os.makedirs(workdir, exist_ok=True)

    baseline = baseline_cpu()
    print(f"baseline (no test streams): system CPU {baseline:.1f}% "
          f"load {open('/proc/loadavg').read().split()[0]}", flush=True)

    results: list[dict] = []

    if args.real:
        try:
            live = live_paths()
        except (OSError, ValueError, urllib.error.URLError) as exc:
            print(f"cannot read MediaMTX's API at {MEDIAMTX_API}: {exc}", file=sys.stderr)
            print("check apiAddress/authInternalUsers in mediamtx.yml and "
                  "INSTAVISION_MEDIAMTX_API", file=sys.stderr)
            return 3
        if not live:
            status = relay_status()
            print("no live insta_* path is carrying bytes, so there is nothing to "
                  "measure.", file=sys.stderr)
            if status:
                print(f"relay reports {status.get('streaming')} of "
                      f"{status.get('expected')} cameras streaming", file=sys.stderr)
            print("open a camera in the app (or wait for the gateway to pick one up), "
                  "then re-run; use the default synthetic mode to measure the "
                  "publish/fan-out layers instead.", file=sys.stderr)
            return 2
        print(f"live cameras: {', '.join(f'{name} ({rate:.2f} Mbit/s)' for name, rate
                                         in sorted(live.items()))}", flush=True)
        relay_pid = args.pid or find_gateway()
        if relay_pid:
            print(f"accounting relay CPU to pid {relay_pid}", flush=True)
        else:
            print("relay process not found; pass --pid to report its CPU", flush=True)
        result = run_real(live, max(1, args.readers), args.seconds, relay_pid, baseline)
        results.append(result)
        print(json.dumps(result), flush=True)
    else:
        relay = load_relay()
        try:
            clip, origin, clip_seconds = clip_bytes(args.clip, workdir)
        except subprocess.CalledProcessError as exc:
            print(f"ffmpeg could not prepare the clip: {exc}", file=sys.stderr)
            return 2
        print(f"clip: {origin} ({len(clip) / 1024:.0f} KB, "
              f"{len(clip) * 8 / clip_seconds / 1e6:.2f} Mbit/s at real time)",
              flush=True)
        for count in args.counts:
            print(f"\n== {count} cameras ==", flush=True)
            result = run_phase(relay, count, args.seconds, clip, clip_seconds, baseline)
            results.append(result)
            print(json.dumps(result), flush=True)

    summarise(results)
    if args.json:
        with open(args.json, "w") as handle:
            json.dump(results, handle, indent=1)
        print(f"\nwritten to {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
