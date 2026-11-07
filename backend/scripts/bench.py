"""Reproducible measurements for docs/PERFORMANCE.md. Prints markdown, saves JSON.

    uv run python scripts/bench.py all --image path/to/hand.jpg
    uv run python scripts/bench.py vision --image hand.jpg     # one section

Sections: startup, ws, vision, robot, stt, tts, intents. Every number is measured on
this machine at run time; sections that cannot run say why instead of printing zeros.
Nothing here touches real robot hardware.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
RESULTS: dict = {}


def stats(xs: list[float]) -> dict:
    v = sorted(xs)
    if not v:
        return {"n": 0}
    return {
        "n": len(v),
        "mean": round(statistics.fmean(v), 2),
        "p50": round(v[len(v) // 2], 2),
        "p95": round(v[min(len(v) - 1, int(len(v) * 0.95))], 2),
        "min": round(v[0], 2),
        "max": round(v[-1], 2),
    }


def row(name: str, s: dict, unit: str = "ms") -> str:
    if not s.get("n"):
        return f"| {name} | 0 | n/a | n/a | n/a |"
    return f"| {name} | {s['n']} | {s['p50']} {unit} | {s['p95']} {unit} | {s['max']} {unit} |"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ------------------------------------------------------------------ startup
def bench_startup(runs: int = 3) -> None:
    import urllib.request

    times = []
    for _ in range(runs):
        port = free_port()
        env = {**os.environ, "LOCKEDIN_DATA_DIR": tempfile.mkdtemp(), "LOCKEDIN_PORT": str(port)}
        t0 = time.perf_counter()
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level", "warning"],
            cwd=BACKEND, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            while time.perf_counter() - t0 < 30:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=0.5) as r:
                        if r.status == 200:
                            break
                except Exception:
                    time.sleep(0.02)
            times.append((time.perf_counter() - t0) * 1000)
        finally:
            proc.terminate()
            proc.wait(timeout=10)
    RESULTS["startup_ms"] = stats(times)
    print("\n### Backend startup (process spawn -> first 200 from /api/health)\n")
    print("| metric | n | p50 | p95 | max |\n|---|---|---|---|---|")
    print(row("cold start to healthy", RESULTS["startup_ms"]))


# ------------------------------------------------------------------ websocket
async def _ws(port: int, pings: int) -> None:
    import urllib.request

    import websockets

    rtts, deliveries = [], []
    async with websockets.connect(f"ws://127.0.0.1:{port}/ws", max_size=2**22) as ws:
        # drain the initial snapshot + replay
        t_end = time.monotonic() + 1.0
        while time.monotonic() < t_end:
            try:
                await asyncio.wait_for(ws.recv(), timeout=0.2)
            except TimeoutError:
                break
        for _ in range(pings):
            t0 = time.perf_counter()
            await ws.send(json.dumps({"type": "ping", "t": t0}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("type") == "pong":
                    rtts.append((time.perf_counter() - t0) * 1000)
                    break
        for _ in range(30):
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/session/progress", data=b'{"units": 1}', headers={"content-type": "application/json"}, method="POST"
            )
            t_send = time.time()
            await asyncio.to_thread(urllib.request.urlopen, req)
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("type") == "event" and msg["data"]["event_type"] == "session.progress":
                    deliveries.append((time.time() - msg["data"]["timestamp"]) * 1000)
                    break
            del t_send
    RESULTS["ws_ping_rtt_ms"] = stats(rtts)
    RESULTS["event_publish_to_client_ms"] = stats(deliveries)


def bench_ws(pings: int = 200) -> None:
    import urllib.request

    port = free_port()
    env = {**os.environ, "LOCKEDIN_DATA_DIR": tempfile.mkdtemp(), "LOCKEDIN_TTS_ENGINE": "none"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level", "warning"],
        cwd=BACKEND, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(300):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=0.5)
                break
            except Exception:
                time.sleep(0.05)
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/session/start", data=b'{"task": "bench", "duration_min": 60}', headers={"content-type": "application/json"}, method="POST"
        )
        urllib.request.urlopen(req)
        asyncio.run(_ws(port, pings))
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    print("\n### WebSocket (localhost, one dashboard client)\n")
    print("| metric | n | p50 | p95 | max |\n|---|---|---|---|---|")
    print(row("ping -> pong round trip", RESULTS["ws_ping_rtt_ms"]))
    print(row("event published -> received by client", RESULTS["event_publish_to_client_ms"]))


# ------------------------------------------------------------------ vision
def bench_vision(image: Path | None, frames: int = 300) -> None:
    import cv2
    import numpy as np

    from app.vision.hands import FaceObserver, HandTracker
    from app.vision.pipeline import VisionService

    models = BACKEND / "models"
    print("\n### Vision (MediaPipe Tasks, CPU, single process)\n")
    if image is None or not image.exists():
        print("Skipped: pass --image with a photo containing hands (live camera access was not available).")
        return
    img = cv2.imread(str(image))
    h, w = img.shape[:2]
    scale = 640 / max(h, w)
    img = cv2.resize(img, (int(w * scale), int(h * scale)))
    canvas = np.zeros((480, 640, 3), dtype=np.uint8)
    ih, iw = img.shape[:2]
    y0, x0 = (480 - ih) // 2, (640 - iw) // 2
    canvas[max(0, y0) : max(0, y0) + min(480, ih), max(0, x0) : max(0, x0) + min(640, iw)] = img[: min(480, ih), : min(640, iw)]
    rgb = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)

    tracker = HandTracker(models)
    face = FaceObserver(models)
    hand_ms, face_ms, detected = [], [], []
    for i in range(frames):
        t0 = time.perf_counter()
        hands = tracker.detect(rgb, (i + 1) * 33)
        hand_ms.append((time.perf_counter() - t0) * 1000)
        detected.append(len(hands))
        if i % 3 == 0:
            t1 = time.perf_counter()
            face.observe(rgb, (i + 1) * 33)
            face_ms.append((time.perf_counter() - t1) * 1000)
    blank = np.zeros_like(rgb)
    tracker2 = HandTracker(models)
    blank_ms = []
    for i in range(frames):
        t0 = time.perf_counter()
        tracker2.detect(blank, (i + 1) * 33)
        blank_ms.append((time.perf_counter() - t0) * 1000)

    svc = VisionService(models, emit=lambda ev: None)
    svc.hands_model, svc.face_model = tracker, face
    svc.last_hands = tracker.detect(rgb, (frames + 5) * 33)
    render_ms = []
    for _ in range(100):
        t0 = time.perf_counter()
        svc._render(canvas)
        render_ms.append((time.perf_counter() - t0) * 1000)
    tracker.close(), tracker2.close(), face.close()

    RESULTS["vision"] = {
        "image": image.name,
        "frame": "640x480",
        "hands_detected_mode": max(set(detected), key=detected.count),
        "hand_landmarker_ms": stats(hand_ms[10:]),
        "hand_landmarker_no_hands_ms": stats(blank_ms[10:]),
        "face_detector_ms": stats(face_ms[3:]),
        "overlay_render_and_jpeg_ms": stats(render_ms),
    }
    v = RESULTS["vision"]
    print(f"Input: `{image.name}` letterboxed to 640x480, {frames} frames, VIDEO running mode, hands found per frame: {v['hands_detected_mode']}.\n")
    print("| stage | n | p50 | p95 | max |\n|---|---|---|---|---|")
    print(row("hand landmarks (hands in frame)", v["hand_landmarker_ms"]))
    print(row("hand landmarks (empty frame)", v["hand_landmarker_no_hands_ms"]))
    print(row("face detection (every 3rd frame)", v["face_detector_ms"]))
    print(row("overlay render + JPEG encode", v["overlay_render_and_jpeg_ms"]))
    budget = v["hand_landmarker_ms"]["p50"] + v["face_detector_ms"]["p50"] / 3 + v["overlay_render_and_jpeg_ms"]["p50"]
    print(f"\nPer-frame processing budget at p50 ~ {budget:.1f} ms -> ~{1000 / budget:.0f} FPS ceiling before camera limits.")
    RESULTS["vision"]["per_frame_p50_budget_ms"] = round(budget, 2)


# ------------------------------------------------------------------ robot (mock + firmware model)
async def _robot(trials: int) -> None:
    from app.robot.commands import MotionPrimitive
    from app.robot.mock import MockRobotTransport
    from app.robot.zeus import ZeusRobotTransport

    sys.path.insert(0, str(BACKEND / "tests"))
    from fake_esp32 import MODE_APP, MODE_NONE, FakeESP32

    # Zeus driver against the firmware model on localhost: measures OUR software path
    # (driver -> WebSocket -> firmware model), not Wi-Fi or the real Arduino.
    server = await FakeESP32("modern").start()
    t = ZeusRobotTransport("127.0.0.1", server.port)
    await t.connect()
    await asyncio.sleep(0.3)
    move_ms, stop_ms = [], []
    for _ in range(trials):
        t0 = time.perf_counter()
        r = await t.move(MotionPrimitive.FORWARD, 40, 2.0)
        assert r.sent
        while server.arduino.mode != MODE_APP:
            await asyncio.sleep(0.001)
        move_ms.append((time.perf_counter() - t0) * 1000)
        await asyncio.sleep(0.15)
        t0 = time.perf_counter()
        await t.stop("bench")
        while server.arduino.mode != MODE_NONE:
            await asyncio.sleep(0.001)
        stop_ms.append((time.perf_counter() - t0) * 1000)
        await asyncio.sleep(0.2)
    await t.disconnect()
    await server.close()
    RESULTS["zeus_model_move_to_parsed_ms"] = stats(move_ms)
    RESULTS["zeus_model_stop_to_parsed_ms"] = stats(stop_ms)

    m = MockRobotTransport(latency_s=0.03)
    acks = []
    m.callbacks.on_ack = lambda info: acks.append(info["latency_ms"])
    await m.connect()
    for _ in range(trials):
        await m.move(MotionPrimitive.BACKWARD, 50, 0.1)
        await asyncio.sleep(0.15)
    await m.disconnect()
    RESULTS["mock_ack_ms"] = stats(acks)


def bench_robot(trials: int = 40) -> None:
    asyncio.run(_robot(trials))
    print("\n### Robot command path (software only: firmware model on localhost, no Wi-Fi, no Arduino)\n")
    print("| metric | n | p50 | p95 | max |\n|---|---|---|---|---|")
    print(row("Zeus driver move() -> firmware model in APP mode", RESULTS["zeus_model_move_to_parsed_ms"]))
    print(row("Zeus driver stop() -> firmware model in MODE_NONE", RESULTS["zeus_model_stop_to_parsed_ms"]))
    print(row("mock robot ack (configured 30 ms latency)", RESULTS["mock_ack_ms"]))


# ------------------------------------------------------------------ STT + intents
PHRASES = [
    ("Locked in, I have twenty five minutes to finish calculus.", "start_session"),
    ("Lock in.", "start_session"),
    ("Start a twenty five minute session.", "start_session"),
    ("Pause.", "pause"),
    ("Resume.", "resume"),
    ("Stop.", "stop_motion"),
    ("Emergency stop.", "emergency_stop"),
    ("Give me my phone.", "release"),
    ("Okay, give my phone back.", "release"),
    ("How much time is left?", "time_left"),
    ("How am I doing?", "status"),
    ("What should I do next?", "next_step"),
    ("I finished a problem.", "progress"),
    ("I finished two problems.", "progress"),
    ("Make it harder.", "harder"),
    ("Stop roasting me.", "tone_down"),
    ("End the session.", "end_session"),
    ("Chaos mode.", "set_personality"),
]
NEGATIVES = ["The weather is nice today.", "Can you pass me the charger?", "I think the answer is forty two."]
VOICES = ["Samantha", "Daniel", "Karen"]


def bench_stt() -> None:
    import numpy as np

    from app.voice.intents import IntentClassifier
    from app.voice.transcription import WhisperTranscriber

    print("\n### Speech-to-text + intent accuracy (synthetic speech)\n")
    model = BACKEND / "models" / "whisper" / "base.en" / "model.bin"
    if not (shutil.which("say") and shutil.which("ffmpeg") and model.exists()):
        print("Skipped: needs macOS `say`, ffmpeg and the local Whisper model.")
        return
    tmp = Path(tempfile.mkdtemp())
    tr = WhisperTranscriber("base.en", "int8", BACKEND / "models")
    t0 = time.time()
    while tr.state == "loading" and time.time() - t0 < 120:
        time.sleep(0.1)
    if tr.state != "ready":
        print(f"Skipped: Whisper failed to load ({tr.error}).")
        return
    clf = IntentClassifier()
    lat, correct, total, rows, false_act = [], 0, 0, [], 0
    available = subprocess.run(["say", "-v", "?"], capture_output=True, text=True).stdout
    voices = [v for v in VOICES if v in available] or ["Samantha"]
    for voice in voices:
        for text, expected in PHRASES + [(n, "unknown") for n in NEGATIVES]:
            aiff, raw = tmp / "a.aiff", tmp / "a.raw"
            subprocess.run(["say", "-v", voice, "-o", str(aiff), text], check=True)
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(aiff), "-ar", "16000", "-ac", "1", "-f", "s16le", str(raw)], check=True)
            audio = np.frombuffer(raw.read_bytes(), dtype=np.int16).astype(np.float32) / 32768.0
            out = tr.transcribe(audio)
            if out is None:
                rows.append((voice, text, "<none>", "-", expected))
                total += 1
                continue
            lat.append(out["roundtrip_ms"])
            intent = clf.classify(out["text"], out["confidence"])
            got = intent.name.value
            accepted = got != "unknown" and intent.confidence >= 0.45
            ok = (got == expected and accepted) or (expected == "unknown" and not accepted)
            if expected == "unknown" and accepted:
                false_act += 1
            correct += ok
            total += 1
            if not ok:
                rows.append((voice, text, out["text"], got, expected))
    tr.close()
    RESULTS["stt"] = {
        "engine": "faster-whisper base.en int8 (worker process)",
        "voices": voices,
        "utterances": total,
        "intent_accuracy": round(correct / total, 3),
        "negatives_acted_on": false_act,
        "negatives_total": len(NEGATIVES) * len(voices),
        "stt_roundtrip_ms": stats(lat),
        "errors": rows,
    }
    s = RESULTS["stt"]
    print(f"{s['utterances']} utterances ({len(PHRASES)} commands + {len(NEGATIVES)} non-commands) x voices {', '.join(voices)}; audio rendered by macOS `say`, fed directly to Whisper (no microphone).\n")
    print(f"- End-to-end intent accuracy: **{s['intent_accuracy'] * 100:.1f}%** ({correct}/{total})")
    print(f"- Non-commands acted on: {false_act}/{s['negatives_total']}")
    print("\n| metric | n | p50 | p95 | max |\n|---|---|---|---|---|")
    print(row("Whisper round trip (send audio -> text)", s["stt_roundtrip_ms"]))
    if rows:
        print("\nMisses:\n")
        for v, said, heard, got, exp in rows:
            print(f"- [{v}] said “{said}” -> heard “{heard}” -> {got} (expected {exp})")


def bench_tts() -> None:
    print("\n### Text-to-speech (macOS `say`, synthesis to file)\n")
    if not shutil.which("say"):
        print("Skipped: `say` not available.")
        return
    lines = ["Bro. We literally just started.", "Twenty-five minutes. Let's see if you actually do it.", "Fine. Robot privileges revoked."]
    times = []
    tmp = Path(tempfile.mkdtemp())
    for _ in range(3):
        for line in lines:
            t0 = time.perf_counter()
            subprocess.run(["say", "-o", str(tmp / "x.aiff"), line], check=True)
            times.append((time.perf_counter() - t0) * 1000)
    RESULTS["tts_say_synthesis_ms"] = stats(times)
    print("| metric | n | p50 | p95 | max |\n|---|---|---|---|---|")
    print(row("`say -o` full synthesis of a one-line reply", RESULTS["tts_say_synthesis_ms"]))
    print("\nThis bounds time-to-first-audio from above; live playback starts streaming earlier.")


def bench_intents(n: int = 2000) -> None:
    from app.voice.intents import IntentClassifier

    clf = IntentClassifier()
    texts = [p for p, _ in PHRASES]
    t0 = time.perf_counter()
    for i in range(n):
        clf.classify(texts[i % len(texts)])
    per = (time.perf_counter() - t0) * 1000 / n
    RESULTS["intent_classify_ms_mean"] = round(per, 4)
    print(f"\n### Deterministic intent classifier\n\nMean {per:.3f} ms per utterance over {n} classifications (no network, no model).")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("section", choices=["all", "startup", "ws", "vision", "robot", "stt", "tts", "intents"])
    ap.add_argument("--image", type=Path)
    ap.add_argument("--out", type=Path, default=BACKEND.parent / "docs" / "benchmarks")
    args = ap.parse_args()
    print(f"# LOCKED IN benchmarks · {time.strftime('%Y-%m-%d %H:%M')} · {platform.platform()} · {platform.machine()} · Python {platform.python_version()}")
    try:
        import psutil

        print(f"CPU: {platform.processor() or 'unknown'} · {psutil.cpu_count()} cores · {round(psutil.virtual_memory().total / 2**30)} GB RAM")
    except Exception:
        pass
    run = {
        "startup": bench_startup,
        "ws": bench_ws,
        "vision": lambda: bench_vision(args.image),
        "robot": bench_robot,
        "stt": bench_stt,
        "tts": bench_tts,
        "intents": bench_intents,
    }
    for name, fn in run.items():
        if args.section in ("all", name):
            fn()
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"results-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps({"machine": platform.platform(), "results": RESULTS}, indent=2))
    print(f"\nSaved {path.relative_to(BACKEND.parent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
