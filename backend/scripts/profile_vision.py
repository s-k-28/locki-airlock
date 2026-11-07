"""Profile the vision pipeline on real camera frames held in memory only.

    uv run python scripts/profile_vision.py --frames 240
    uv run python scripts/profile_vision.py --frames 240 --skip-gpu

1. Captures N frames from the camera into RAM. Frames are never written anywhere and are
   dropped when the script ends; only numbers are printed. For a useful run, someone sits at
   the laptop and moves a hand around, including toward the bottom-right phone zone.
2. Replays the SAME frames, with their original timestamps, through
   VisionService.process_frame in several configurations, each with fresh models.
3. Prints per-stage p50/p95, total per frame, and agreement with the baseline: frames where
   the same number of hands was found and the wrist moved < 2 % of the frame. A faster
   configuration only counts if it sees the same hands.

Configurations: baseline (dashboard showing the stream: overlay rendered every frame),
no stream (overlay every 15th frame), face off, marker tracking on (zone follows the
robot), and the MediaPipe GPU delegate for hands, if this platform supports it.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from collections import deque
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import numpy as np  # noqa: E402

from app.config import Settings  # noqa: E402
from app.vision.hands import HandTracker  # noqa: E402
from app.vision.pipeline import VisionService  # noqa: E402
from app.vision.robot_tracker import MarkerObservation, ZoneAnchor  # noqa: E402
from app.vision.webcam import CameraSource  # noqa: E402

ZONE = [[0.62, 0.55], [0.95, 0.55], [0.95, 0.97], [0.62, 0.97]]


def capture(n: int, source: str) -> tuple[list[tuple[np.ndarray, float]], bool, float]:
    cam = CameraSource(source)
    cam.start()
    frames: list[tuple[np.ndarray, float]] = []
    last = 0
    t0 = time.monotonic()
    try:
        while len(frames) < n and time.monotonic() - t0 < n / 5 + 10:
            item = cam.read_latest(last, timeout=1.0)
            if item is None:
                continue
            fid, bgr, cap_t = item
            last = fid
            if time.monotonic() - t0 < 1.0:
                continue  # auto-exposure settling
            frames.append((bgr.copy(), cap_t))
    finally:
        cam.stop()
    return frames, cam.mirror, time.monotonic() - t0


def q(xs, p):
    v = sorted(xs)
    return None if not v else round(v[min(len(v) - 1, int(len(v) * p))], 2)


def run_config(name: str, frames, mirrored: bool, models_dir: Path, *, stream: bool, face: bool, markers: bool, delegate: str = "cpu"):
    svc = VisionService(models_dir=models_dir, emit=lambda *a, **k: None)
    svc.load_models()
    if delegate != "cpu":
        svc.hands_model = HandTracker(models_dir, delegate=delegate)
    if not face:
        svc.face_model = None
    svc._stage = {k: deque(maxlen=len(frames)) for k in svc._stage}
    svc.stream_clients = 1 if stream else 0
    if markers:
        corners = np.array([[40.0, 40.0], [100.0, 40.0], [100.0, 100.0], [40.0, 100.0]])
        anchor = ZoneAnchor.attach(ZONE, [MarkerObservation(7, corners, 640, 480)]).to_dict()
        svc.configure(zone=ZONE, zone_mode="marker", anchor=anchor, marker_ids=[7, 8])
    else:
        svc.configure(zone=ZONE, zone_mode="static")
    totals, hands_out = [], []
    for i, (bgr, cap_t) in enumerate(frames):
        t = time.perf_counter()
        svc.process_frame(i + 1, bgr, cap_t, mirrored)
        totals.append((time.perf_counter() - t) * 1000)
        hands_out.append([h.landmarks[0] for h in svc.last_hands])
    stages = {k: (q(list(v), 0.5), q(list(v), 0.95), len(v)) for k, v in svc._stage.items() if v}
    if svc.hands_model:
        svc.hands_model.close()
    return {"name": name, "totals": totals, "stages": stages, "hands": hands_out}


def agreement(base, other) -> tuple[float, float, int, int]:
    """(share of frames that agree, mean wrist distance, frames with fewer hands, with more)."""
    same, deltas, fewer, more = 0, [], 0, 0
    for a, b in zip(base["hands"], other["hands"], strict=True):
        if len(a) != len(b):
            fewer += len(b) < len(a)
            more += len(b) > len(a)
            continue
        if not a:
            same += 1
            continue
        d = max(min(np.hypot(x1 - x2, y1 - y2) for (x2, y2) in b) for (x1, y1) in a)
        deltas.append(d)
        if d < 0.02:
            same += 1
    return same / len(base["hands"]), (statistics.fmean(deltas) if deltas else 0.0), fewer, more


def live(seconds: float, source: str, models_dir: Path) -> None:
    """The real camera and processing threads, as in the backend, but with no web server:
    compares a stream viewer (overlay rendered and encoded every frame) with none."""
    print(f"## Live pipeline without the web server ({seconds:.0f} s per setting)\n")
    print("| setting | capture FPS | processing FPS | hands p50/p95 | total p50/p95 | capture→result p50/p95 | dropped |")
    print("|---|---|---|---|---|---|---|")
    for viewers in (0, 1):
        svc = VisionService(models_dir=models_dir, emit=lambda *a, **k: None)
        svc.stream_clients = viewers
        svc.start(source)
        time.sleep(seconds)
        st = svc.snapshot()["stats"]
        svc.stop()
        print(
            f"| {'stream viewer' if viewers else 'no viewer'} | {st['capture_fps']} | {st['processing_fps']} | {st['hand_ms_p50']}/{st['hand_ms_p95']} | "
            f"{st['total_ms_p50']}/{st['total_ms_p95']} | {st['capture_to_result_ms_p50']}/{st['capture_to_result_ms_p95']} | {st['frames_dropped']} |"
        )


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--frames", type=int, default=240)
    p.add_argument("--source", default="0")
    p.add_argument("--skip-gpu", action="store_true")
    p.add_argument("--reps", type=int, default=3)
    p.add_argument("--live", type=float, default=0, help="seconds per setting for the live (threaded) comparison instead of replay")
    a = p.parse_args()
    models_dir = Settings().models_dir
    if a.live:
        live(a.live, a.source, models_dir)
        return

    frames, mirrored, took = capture(a.frames, a.source)
    if not frames:
        print("no frames captured (camera busy or no permission)")
        sys.exit(1)
    h, w = frames[0][0].shape[:2]
    span = frames[-1][1] - frames[0][1]
    print(f"## Vision profile ({len(frames)} frames {w}x{h}, captured at {len(frames) / max(span, 1e-6):.1f} FPS, held in memory only)\n")

    configs = [
        ("baseline (stream shown)", dict(stream=True, face=True, markers=False)),
        ("no stream client", dict(stream=False, face=True, markers=False)),
        ("face off", dict(stream=True, face=False, markers=False)),
        ("marker tracking on", dict(stream=True, face=True, markers=True)),
    ]
    if not a.skip_gpu:
        configs.append(("hands on GPU delegate", dict(stream=True, face=True, markers=False, delegate="gpu")))
    # Each configuration runs --reps times, in a rotating order, so warm-up and thermal drift
    # do not land on one configuration.
    runs: dict[str, list[dict]] = {name: [] for name, _ in configs}
    failed: dict[str, str] = {}
    for rep in range(a.reps):
        for k in range(len(configs)):
            name, kw = configs[(k + rep) % len(configs)]
            if name in failed:
                continue
            try:
                runs[name].append(run_config(name, frames, mirrored, models_dir, **kw))
            except Exception as exc:  # e.g. a delegate this platform cannot run
                failed[name] = f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}"
    for name, why in failed.items():
        print(f"- {name}: could not run ({why})")
    base = runs[configs[0][0]][0]
    with_hands = sum(1 for hs in base["hands"] if hs)
    print(f"Frames with at least one hand (baseline): {with_hands}/{len(frames)} · {a.reps} runs per configuration\n")
    stage_names = ["markers", "to_rgb", "hands", "reach", "face", "render"]
    print("| config | total p50 (runs) | total p95 (runs) | " + " | ".join(f"{s} p50/p95" for s in stage_names) + " | same hands as baseline |")
    print("|---|---|---|" + "---|" * len(stage_names) + "---|")
    for name, _ in configs:
        rs = runs[name]
        if not rs:
            continue
        p50s = [q(r["totals"], 0.5) for r in rs]
        p95s = [q(r["totals"], 0.95) for r in rs]
        cells = []
        for s in stage_names:
            samples = [r["stages"][s] for r in rs if s in r["stages"]]
            if not samples:
                cells.append("—")
                continue
            n = samples[0][2]
            cells.append(f"{statistics.median(x[0] for x in samples):.2f}/{statistics.median(x[1] for x in samples):.2f}" + (f" (n={n})" if n < len(frames) else ""))
        ags = [agreement(base, r) for r in rs]
        print(
            f"| {name} | {statistics.median(p50s):.1f} ({min(p50s):.1f}-{max(p50s):.1f}) | {statistics.median(p95s):.1f} ({min(p95s):.1f}-{max(p95s):.1f}) | "
            + " | ".join(cells)
            + f" | {min(x[0] for x in ags):.1%} (wrist Δ ≤ {max(x[1] for x in ags):.4f}; fewer hands in {max(x[2] for x in ags)}, more in {max(x[3] for x in ags)} frames) |"
        )
    print("\nTimes in ms on this machine; stages not run every frame show n. Frames freed on exit.")
    frames.clear()


if __name__ == "__main__":
    main()
