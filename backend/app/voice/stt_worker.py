"""Speech-to-text worker process.

Two engines, same protocol. whisper.cpp (pywhispercpp, Metal) is the default since
Milestone 5: on this Mac it transcribed the 93-utterance test set in 121 ms (p50) with
0.4% word errors, against 423 ms and 2.4% for faster-whisper with the same base.en
weights and prompt (docs/MODELS.md). faster-whisper stays as the fallback.

faster-whisper imports PyAV, which bundles its own libavdevice. OpenCV bundles a
different copy, and loading both into one macOS process produces duplicate
Objective-C classes ("may cause spurious casting failures and mysterious crashes").
Running Whisper here, in a spawned process, keeps the two libraries apart. It also
keeps CTranslate2's CPU work off the event loop.

Protocol over a multiprocessing Pipe:
  parent -> {"op": "transcribe", "id": n, "pcm": <int16 bytes>, "sr": 16000}
  child  -> {"op": "ready", "load_ms": ...} | {"op": "result", "id": n, ...} | {"op": "error", ...}
"""

from __future__ import annotations

import math
import time

# Biases decoding toward the command vocabulary without forcing it.
INITIAL_PROMPT = (
    "Locked in. Lock in, 25 minutes. Pause. Resume. Stop. Emergency stop. Give me my phone. "
    "How am I doing? How much time is left? I finished a problem. Make it harder. Stop roasting me."
)


def confidence_from_segments(segments) -> float:
    """Duration-weighted exp(avg_logprob), discounted by no_speech_prob."""
    total, acc = 0.0, 0.0
    for s in segments:
        dur = max(0.05, float(s.end) - float(s.start))
        p = math.exp(float(s.avg_logprob))
        if float(s.no_speech_prob) > 0.5:
            p *= 1.0 - float(s.no_speech_prob)
        acc += p * dur
        total += dur
    return round(acc / total, 3) if total else 0.0


def _faster_whisper(model_name: str, compute_type: str, model_dir: str | None):
    import numpy as np
    from faster_whisper import WhisperModel

    model = WhisperModel(model_dir if model_dir else model_name, device="cpu", compute_type=compute_type, cpu_threads=4)

    def run(audio):
        segments, _info = model.transcribe(
            audio,
            language="en",
            beam_size=1,
            vad_filter=False,
            condition_on_previous_text=False,
            initial_prompt=INITIAL_PROMPT,
            without_timestamps=True,
        )
        segs = list(segments)
        return " ".join(s.text.strip() for s in segs).strip(), confidence_from_segments(segs)

    run(np.zeros(16000, dtype=np.float32))  # warm up so the first real command is not slow
    return run


def _whisper_cpp(model_path: str, use_gpu: bool):
    import numpy as np
    from pywhispercpp.model import Model

    model = Model(model_path, redirect_whispercpp_logs_to=None, n_threads=4, print_progress=False, print_realtime=False,
                  context_params={"use_gpu": use_gpu})

    def run(audio):
        segs = model.transcribe(audio.astype(np.float32), language="en", no_context=True, single_segment=True,
                                initial_prompt=INITIAL_PROMPT, extract_probability=True)
        text = " ".join(s.text.strip() for s in segs).strip()
        probs = [float(s.probability) for s in segs if not math.isnan(float(s.probability))]
        # Geometric mean of the token probabilities: the same quantity as faster-whisper's
        # exp(avg_logprob), so the command-confidence gates mean the same thing.
        return text, round(sum(probs) / len(probs), 3) if probs else 0.0

    run(np.zeros(16000, dtype=np.float32))
    return run


def worker_main(conn, model_name: str, compute_type: str, model_dir: str | None, engine: str = "faster-whisper", use_gpu: bool = True) -> None:
    import numpy as np

    t0 = time.perf_counter()
    try:
        run = _whisper_cpp(model_dir, use_gpu) if engine == "whisper.cpp" else _faster_whisper(model_name, compute_type, model_dir)
    except Exception as exc:
        conn.send({"op": "error", "error": f"could not load {engine} model {model_name!r}: {exc}"})
        return
    conn.send({"op": "ready", "load_ms": round((time.perf_counter() - t0) * 1000), "model": model_name, "engine": engine})
    while True:
        try:
            msg = conn.recv()
        except (EOFError, OSError):
            return
        if msg.get("op") == "stop":
            return
        if msg.get("op") != "transcribe":
            continue
        t1 = time.perf_counter()
        try:
            audio = np.frombuffer(msg["pcm"], dtype=np.int16).astype(np.float32) / 32768.0
            text, confidence = run(audio)
            conn.send(
                {
                    "op": "result",
                    "id": msg["id"],
                    "text": text,
                    "confidence": confidence,
                    "latency_ms": round((time.perf_counter() - t1) * 1000, 1),
                    "audio_s": round(len(audio) / 16000, 2),
                }
            )
        except Exception as exc:
            conn.send({"op": "error", "id": msg.get("id"), "error": str(exc)})
