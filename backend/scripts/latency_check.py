"""End-to-end response time on this Mac, from the end of the user's sentence to the moment
the robot's voice starts playing, through the production pieces (docs/MILESTONE5_VERIFICATION.md):

    spoken sentence (macOS voice, 16 kHz) -> whisper.cpp worker -> runtime turn (parser or
    local language model, with the context prefilled while the sentence was "being said")
    -> voice (cached Fish clip, or Kokoro rendered now) -> output

    .venv/bin/python scripts/latency_check.py [--vision-load] [--audible]

By default the output is a stub that records when playback would start and plays nothing
(PortAudio itself adds ~10 ms; the speaker adds its own latency, measured separately). With
--audible it plays through the real output device: ask first, someone has to listen.
--vision-load runs the camera pipeline's per-frame work (JPEG decode + hand landmarks + face)
at 15 fps on synthetic frames throughout, as during a session.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "backend" / "tests"))
os.environ.setdefault("HF_HUB_OFFLINE", "1")

import numpy as np  # noqa: E402

from app.config import RuntimeSettings, Settings  # noqa: E402
from app.runtime import LockedInRuntime, VisionEvent  # noqa: E402
from app.telemetry.events import EventSource, EventType  # noqa: E402
from app.voice.transcription import WhisperTranscriber  # noqa: E402

TURNS = [
    ("Lock me in for 25 minutes.", "parser"),
    ("I have a calculus test tomorrow, so keep me off my phone.", "model"),
    ("How much time do I have left?", "parser"),
    ("Why do you keep doing that?", "model"),
    ("Can you be quiet for a bit?", "model"),
    ("Honestly I'm almost done with this problem set, like two more questions.", "model"),
    ("Okay, what did I say I was studying for again?", "model"),
    ("Give me my phone back.", "parser"),
]


class SilentOut:
    """Stands in for AudioOut: records when playback would start, plays nothing."""

    last_start_mono: float | None = None
    last_latency_ms: float | None = None

    async def play(self, pcm, sample_rate: int, device=None, volume: float = 1.0) -> None:
        self.last_start_mono = time.monotonic()
        self.last_latency_ms = None
        await asyncio.sleep(len(pcm) / sample_rate + 0.05)


def say(text: str, tmp: Path) -> np.ndarray:
    wav = tmp / "u.wav"
    subprocess.run(["say", "-v", "Samantha", "-o", str(wav), "--file-format=WAVE", "--data-format=LEI16@16000", "--", text],
                   check=True, capture_output=True, timeout=30)
    with wave.open(str(wav)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768


def vision_load(stop: threading.Event, frames: list) -> None:
    import cv2
    from fake_mjpeg import test_card

    from app.vision.hands import FaceObserver, HandTracker

    models = ROOT / "backend" / "models"
    tracker, face = HandTracker(models), FaceObserver(models)
    jpegs = [test_card(i) for i in range(30)]
    i = 0
    while not stop.is_set():
        t0 = time.perf_counter()
        rgb = cv2.cvtColor(cv2.imdecode(np.frombuffer(jpegs[i % 30], np.uint8), cv2.IMREAD_COLOR), cv2.COLOR_BGR2RGB)
        tracker.detect(rgb, 1000 + i * 66)
        if i % 3 == 0:
            face.observe(rgb, 1000 + i * 66)
        frames.append((time.perf_counter() - t0) * 1000)
        i += 1
        time.sleep(max(0.0, 1 / 15 - (time.perf_counter() - t0)))
    tracker.close(), face.close()


async def main_async(args) -> int:
    settings = Settings(data_dir=ROOT / "data", robot_driver="mock", mock_autoconnect=True, offline=True, llm_provider="local",
                        stt_gpu_priority=args.stt_priority)
    rs = RuntimeSettings()
    rs.coach.min_seconds_between_interventions = 20.0
    rt = LockedInRuntime(settings, runtime_settings=rs, persist_settings=False, enable_vision=False, enable_voice=False)
    if not args.audible:
        rt.speaker.engine.audio = SilentOut()
    await rt.start()
    stt = WhisperTranscriber(settings.whisper_model, settings.whisper_compute_type, settings.models_dir, engine=settings.stt_engine)
    t0 = time.monotonic()
    while (stt.state == "loading" or rt.companion.llm is None or rt.companion.llm.state == "loading" or (rt.kokoro and rt.kokoro.state != "ready"))\
            and time.monotonic() - t0 < 180:
        await asyncio.sleep(0.2)
    print(f"ready in {time.monotonic() - t0:.1f}s: stt={stt.describe()['engine']} {stt.state}, llm={rt.companion.llm.state if rt.companion.llm else None}, "
          f"kokoro={rt.kokoro.state if rt.kokoro else None}")
    if rt._kokoro_task is not None:
        await rt._kokoro_task  # fixed lines pre-rendered, as in a real start
    rt.vision_status = {"running": True, "camera": "live"}
    stop, frames = threading.Event(), []
    th = None
    if args.vision_load:
        th = threading.Thread(target=vision_load, args=(stop, frames), daemon=True)
        th.start()
    rows = []
    with tempfile.TemporaryDirectory() as td:
        for i, (text, path) in enumerate(TURNS):
            audio = say(text, Path(td))
            if i == 3:  # something to ask about: a confirmed reach (as the camera thread would report it)
                await rt.handle_vision_event(VisionEvent("confirmed", {"track": 5}, confidence=0.9, frame_mono=time.monotonic()))
                await asyncio.sleep(3.0)
                rt.sm.state = __import__("app.intelligence.state_machine", fromlist=["State"]).State.SESSION_ACTIVE
            # Push-to-talk goes down; the sentence is "spoken" for its real duration.
            rt.bus.publish(EventType.VOICE_SPEECH_STARTED, EventSource.VOICE, {"mode": "push_to_talk"})
            await asyncio.sleep(len(audio) / 16000)
            heard = time.monotonic()  # the user stopped talking (button up)
            rt._stt_busy(True)  # as VoiceService does around each transcription
            try:
                res = await asyncio.to_thread(stt.transcribe, audio)
            finally:
                rt._stt_busy(False)
            stt_ms = res["roundtrip_ms"] if res else None
            r = await rt.handle_utterance(res["text"] if res else "", source="voice", stt_confidence=res.get("confidence", 0.0) if res else 0.0,
                                          stt_ms=stt_ms, heard_mono=heard)
            turn = rt.voice_turns[-1]
            for _ in range(200):
                if turn.get("reply_to_audio_ms") is not None:
                    break
                await asyncio.sleep(0.02)
            # Wait for the reply to finish "playing" before the next sentence.
            for _ in range(500):
                if not rt.speaker.speaking and not rt.speaker._items:
                    break
                await asyncio.sleep(0.02)
            dlg = r.data.get("dialogue") or {}
            last = rt.speaker.history[-1] if rt.speaker.history else {}
            row = {
                "said": text, "heard": res["text"] if res else None, "expected_path": path, "route": turn.get("route"),
                "stt_ms": stt_ms, "speech_end_to_playback_ms": turn.get("reply_to_audio_ms"), "reply_source": turn.get("reply_source"),
                "reply": r.reply, "llm_first_token_ms": (dlg.get("llm") or {}).get("first_token_ms"), "llm_total_ms": (dlg.get("llm") or {}).get("total_ms"),
                "streamed": dlg.get("streamed"), "voice_render_ms": next((h.get("first_audio_ms") for h in reversed(rt.speaker.history) if h.get("text") and r.reply and h["text"] in (r.reply or "")), None),
                "action": (r.data.get("action_request") or {}).get("name"), "last_source": last.get("source_label"),
                "prefilled": (dlg.get("llm") or {}).get("prefilled"), "problems": dlg.get("problems"), "retried": dlg.get("retried"),
            }
            rows.append(row)
            print(f"{row['route'] or '-':8s} {row['speech_end_to_playback_ms']!s:>8} ms  STT {stt_ms!s:>6}  LLM first {row['llm_first_token_ms']!s:>7} "
                  f"prefilled {row['prefilled']!s:5s} {row['reply_source'] or '':34s} | {text[:40]!r} -> {(r.reply or '')[:60]!r} {row['problems'] or ''}")
    stop.set()
    if th is not None:
        th.join()
    mem = await asyncio.to_thread(rt.companion.llm.memory, 2.0) if rt.companion.llm else {}
    stt.close()
    import psutil

    rss = round(psutil.Process().memory_info().rss / 2**20, 1)
    await rt.shutdown()

    def stats(xs):
        xs = sorted(x for x in xs if x is not None)
        if not xs:
            return None
        return {"n": len(xs), "p50": round(statistics.median(xs), 1), "max": round(xs[-1], 1), "min": round(xs[0], 1)}

    swap = subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True).stdout.strip()
    out = {
        "swap": swap,  # this Mac pages heavily when other apps are open; it shows in Kokoro's render time
        "audible": args.audible,
        "stt_gpu_priority": args.stt_priority,
        "vision_load": args.vision_load,
        "vision_frame_ms": stats(frames) if frames else None,
        "parser_turns": stats([r["speech_end_to_playback_ms"] for r in rows if r["route"] == "reflex"]),
        "model_turns": stats([r["speech_end_to_playback_ms"] for r in rows if r["route"] == "converse"]),
        "stt_ms": stats([r["stt_ms"] for r in rows]),
        "memory": {"backend_rss_mb": rss, "llm_worker": mem},
        "rows": rows,
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    print(json.dumps({k: v for k, v in out.items() if k != "rows"}, indent=1))
    path = ROOT / "docs" / "benchmarks" / f"m5-latency-{'audible' if args.audible else 'silent'}{'-vision' if args.vision_load else ''}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(out, indent=1))
    print(f"saved {path.relative_to(ROOT)}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--vision-load", action="store_true")
    p.add_argument("--audible", action="store_true", help="play through the real output device (ask first)")
    p.add_argument("--stt-priority", action="store_true", help="pause the model's prefill while Whisper transcribes (A/B; off by default)")
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
