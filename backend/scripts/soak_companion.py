"""Software soak of the Milestone 5 companion: the real local language model, whisper.cpp
and Kokoro running for N minutes inside the real runtime, with scripted conversation and
simulated perception. Output is silent (a stub records when playback would start).

    .venv/bin/python scripts/soak_companion.py --minutes 20

For response times use `scripts/run_report.py` on the run's telemetry file: it sees every
turn; this script's own summary only counts turns whose audio started while it waited.

Every 25-50 s a sentence from a pool of natural lines is spoken by a macOS voice and goes
through whisper.cpp; every 40-70 s a perception event (confirmed reach, possible reach,
camera blip) arrives the way the camera thread delivers it. Memory is sampled each minute.
This is not the live hardware soak (robot camera, real microphone, a person): see
docs/MILESTONE5_VERIFICATION.md, H8.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import statistics
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
os.environ.setdefault("HF_HUB_OFFLINE", "1")

import numpy as np  # noqa: E402
import psutil  # noqa: E402

from app.config import RuntimeSettings, Settings  # noqa: E402
from app.intelligence.state_machine import State  # noqa: E402
from app.runtime import LockedInRuntime, VisionEvent  # noqa: E402
from app.telemetry.events import EventSource, EventType  # noqa: E402
from app.voice.transcription import WhisperTranscriber  # noqa: E402

LINES = [
    "I have a chemistry exam on Friday so I really need to get through these notes.",
    "how much time is left",
    "can you give me ten more minutes, I'm finally getting it",
    "why do you keep doing that",
    "honestly this is kind of annoying",
    "can you be quiet for a bit",
    "you can talk again",
    "I just finished the second chapter",
    "wait what was I supposed to be working on",
    "do you actually see me right now",
    "I need to grab some water, hold on a sec",
    "okay I'm back",
    "be honest, how am I doing so far",
    "what's the point of this anyway",
    "tell me something to keep me going",
]


class SilentOut:
    last_start_mono: float | None = None
    last_latency_ms: float | None = None

    async def play(self, pcm, sample_rate: int, device=None, volume: float = 1.0) -> None:
        self.last_start_mono = time.monotonic()
        await asyncio.sleep(len(pcm) / sample_rate + 0.05)


def say(text: str, tmp: Path) -> np.ndarray:
    wav = tmp / "u.wav"
    subprocess.run(["say", "-v", random.choice(["Samantha", "Daniel", "Reed (English (US))"]), "-o", str(wav), "--file-format=WAVE",
                    "--data-format=LEI16@16000", "--", text], check=True, capture_output=True, timeout=30)
    with wave.open(str(wav)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768


def tree_rss_mb() -> float:
    me = psutil.Process()
    total = me.memory_info().rss
    for c in me.children(recursive=True):
        try:
            total += c.memory_info().rss
        except psutil.Error:
            pass
    return round(total / 2**20, 1)


async def main_async(args) -> int:
    rng = random.Random(args.seed)
    settings = Settings(data_dir=ROOT / "data", robot_driver="mock", mock_autoconnect=True, offline=True, llm_provider="local")
    rs = RuntimeSettings()
    rt = LockedInRuntime(settings, runtime_settings=rs, persist_settings=False, enable_vision=False, enable_voice=False)
    rt.speaker.engine.audio = SilentOut()
    await rt.start()
    stt = WhisperTranscriber(settings.whisper_model, settings.whisper_compute_type, settings.models_dir, engine=settings.stt_engine)
    t0 = time.monotonic()
    while (stt.state == "loading" or rt.companion.llm is None or rt.companion.llm.state == "loading") and time.monotonic() - t0 < 180:
        await asyncio.sleep(0.2)
    rt.vision_status = {"running": True, "camera": "live"}
    await rt.start_session("chemistry notes", duration_min=max(25, args.minutes + 5), speak=False)
    start = time.monotonic()
    end = start + args.minutes * 60
    next_line, next_event, next_mem = start + 5, start + 20, start
    mem, turns, errors = [], [], []
    with tempfile.TemporaryDirectory() as td:
        while time.monotonic() < end:
            now = time.monotonic()
            try:
                if now >= next_mem:
                    llm_mem = await asyncio.to_thread(rt.companion.llm.memory, 2.0) if rt.companion.llm else {}
                    mem.append({"t_min": round((now - start) / 60, 1), "process_tree_rss_mb": tree_rss_mb(), "llm": llm_mem,
                                "loop_lag_ms": round(rt.watchdog.max_lag_s * 1000, 1)})
                    next_mem = now + 60
                if now >= next_line:
                    text = rng.choice(LINES)
                    audio = await asyncio.to_thread(say, text, Path(td))  # not on the event loop
                    rt.bus.publish(EventType.VOICE_SPEECH_STARTED, EventSource.VOICE, {"mode": "push_to_talk"})
                    await asyncio.sleep(len(audio) / 16000)
                    heard = time.monotonic()
                    rt._stt_busy(True)
                    try:
                        res = await asyncio.to_thread(stt.transcribe, audio)
                    finally:
                        rt._stt_busy(False)
                    r = await rt.handle_utterance(res["text"] if res else "", source="voice", stt_confidence=(res or {}).get("confidence", 0.0),
                                                  stt_ms=(res or {}).get("roundtrip_ms"), heard_mono=heard)
                    turn = rt.voice_turns[-1] if rt.voice_turns else {}
                    for _ in range(750):  # up to 15 s: a shorter wait drops exactly the slowest turns
                        if turn.get("reply_to_audio_ms") is not None or not r.reply:
                            break
                        await asyncio.sleep(0.02)
                    turns.append({"said": text, "heard": (res or {}).get("text"), "route": turn.get("route"), "ok": r.ok,
                                  "to_audio_ms": turn.get("reply_to_audio_ms"), "reply": r.reply, "source": turn.get("reply_source")})
                    next_line = time.monotonic() + rng.uniform(25, 50)
                if now >= next_event:
                    kind = rng.choice(["confirmed", "confirmed", "approaching", "blip"])
                    if kind == "blip":
                        await rt.handle_vision_event(VisionEvent("camera_disconnected", {}))
                        await asyncio.sleep(1.0)
                        await rt.handle_vision_event(VisionEvent("camera_connected", {}))
                    else:
                        await rt.handle_vision_event(VisionEvent(kind, {"track": rng.randint(1, 999)}, confidence=rng.uniform(0.3, 0.95), frame_mono=time.monotonic()))
                        await asyncio.sleep(2.0)
                        if rt.sm.state in (State.PHONE_REACH_CONFIRMED, State.POSSIBLE_PHONE_REACH):
                            rt.sm.state = State.SESSION_ACTIVE  # the simulated hand leaves
                    next_event = time.monotonic() + rng.uniform(40, 70)
                if not rt.sessions.active:  # "I'm done" or a release ends it: start another
                    await rt.reset() if rt.sm.state == State.EMERGENCY_STOP else None
                    if rt.sm.state == State.RELEASED:
                        rt.sm.state = State.IDLE
                    await rt.start_session("chemistry notes", duration_min=30, speak=False)
            except Exception as exc:  # recorded, the soak goes on
                errors.append(f"{type(exc).__name__}: {exc}")
            await asyncio.sleep(0.5)
    decisions = list(rt.behavior.decisions)
    llm_desc = rt.companion.llm.describe() if rt.companion.llm else {}
    stt.close()
    await rt.shutdown()

    def stats(xs):
        xs = sorted(x for x in xs if x is not None)
        return {"n": len(xs), "p50": round(statistics.median(xs), 1), "p90": round(xs[int(0.9 * (len(xs) - 1))], 1), "max": round(xs[-1], 1)} if xs else None

    out = {
        "minutes": args.minutes,
        "turns": len(turns),
        "model_turns_to_audio_ms": stats([t["to_audio_ms"] for t in turns if t["route"] == "converse"]),
        "parser_turns_to_audio_ms": stats([t["to_audio_ms"] for t in turns if t["route"] == "reflex"]),
        "unanswered_turns": sum(1 for t in turns if not t["reply"]),
        "llm": {k: llm_desc.get(k) for k in ("calls", "failures", "cancelled", "p50_ms", "prefill")},
        "decisions_recent_window": {"spoke": sum(d.speak for d in decisions), "silent": sum(not d.speak for d in decisions)},
        "memory": mem,
        "memory_growth_mb": round(mem[-1]["process_tree_rss_mb"] - mem[0]["process_tree_rss_mb"], 1) if len(mem) > 1 else None,
        "errors": errors,
        "transcript": turns,
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    print(json.dumps({k: v for k, v in out.items() if k not in ("transcript", "memory")}, indent=1))
    path = ROOT / "docs" / "benchmarks" / f"m5-soak-{args.minutes}min-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(out, indent=1))
    print(f"saved {path.relative_to(ROOT)}")
    return 1 if errors else 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--minutes", type=float, default=20)
    p.add_argument("--seed", type=int, default=5)
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
