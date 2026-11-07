"""Pluggable speech-to-text in a worker process. Default: whisper.cpp (Metal); fallback:
faster-whisper (CPU). Same Whisper base.en weights family, same prompt (stt_worker.py)."""

from __future__ import annotations

import importlib.util
import logging
import multiprocessing as mp
import re
import threading
import time
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

# Whisper's well-known outputs for silence/noise. Never act on these.
HALLUCINATIONS = {
    "", "you", "thank you", "thanks", "thank you for watching", "thanks for watching", "bye", "okay",
    "subtitles by the amaraorg community", "please subscribe", "so", "uh", "um", "hmm",
}


def is_hallucination(text: str) -> bool:
    t = re.sub(r"[^a-z ]", "", text.lower()).strip()
    return t in HALLUCINATIONS or len(t) < 2


class Transcriber:
    engine = "none"
    state = "unavailable"
    error: str | None = None

    def transcribe(self, audio: np.ndarray, timeout_s: float = 10.0) -> dict | None:
        return None

    def close(self) -> None:
        pass

    def describe(self) -> dict:
        return {"engine": self.engine, "state": self.state, "error": self.error}


class WhisperTranscriber(Transcriber):
    engine = "faster-whisper"

    def __init__(self, model: str = "base.en", compute_type: str = "int8", models_dir: Path | None = None, engine: str = "faster-whisper", use_gpu: bool = True):
        self.model = model
        self.compute_type = compute_type
        self.engine = engine
        self.use_gpu = use_gpu
        if engine == "whisper.cpp":
            ggml = models_dir / "whispercpp" / f"ggml-{model}.bin" if models_dir else None
            self.model_dir = str(ggml) if ggml and ggml.exists() else None
        else:
            local = models_dir / "whisper" / model if models_dir else None
            self.model_dir = str(local) if local and (local / "model.bin").exists() else None
        self.state = "loading"
        self.error = None
        self.load_ms: float | None = None
        self.last_latency_ms: float | None = None
        self._lock = threading.Lock()
        self._next_id = 0
        ctx = mp.get_context("spawn")
        self._conn, child = ctx.Pipe()
        from .stt_worker import worker_main

        self._proc = ctx.Process(target=worker_main, args=(child, model, compute_type, self.model_dir, engine, use_gpu), daemon=True, name="stt-worker")
        self._proc.start()
        threading.Thread(target=self._await_ready, daemon=True, name="stt-ready").start()

    def _await_ready(self) -> None:
        try:
            if self._conn.poll(180):
                msg = self._conn.recv()
                if msg.get("op") == "ready":
                    self.state = "ready"
                    self.load_ms = msg.get("load_ms")
                    log.info("Whisper %s ready in %s ms", self.model, self.load_ms)
                    return
                self.state, self.error = "error", msg.get("error", "unknown error")
            else:
                self.state, self.error = "error", "timed out loading the Whisper model (is it downloaded?)"
        except Exception as exc:
            self.state, self.error = "error", str(exc)
        log.warning("STT unavailable: %s", self.error)

    def transcribe(self, audio: np.ndarray, timeout_s: float = 10.0) -> dict | None:
        if self.state != "ready":
            return None
        pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
        with self._lock:
            self._next_id += 1
            rid = self._next_id
            t0 = time.perf_counter()
            self._conn.send({"op": "transcribe", "id": rid, "pcm": pcm, "sr": 16000})
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                if not self._conn.poll(max(0.0, deadline - time.monotonic())):
                    break
                msg = self._conn.recv()
                if msg.get("id") != rid:
                    continue
                if msg.get("op") == "error":
                    log.warning("transcription failed: %s", msg.get("error"))
                    return None
                msg["roundtrip_ms"] = round((time.perf_counter() - t0) * 1000, 1)
                self.last_latency_ms = msg["roundtrip_ms"]
                return msg
        log.warning("transcription timed out after %.1fs", timeout_s)
        return None

    def close(self) -> None:
        try:
            self._conn.send({"op": "stop"})
        except Exception:
            pass
        self._proc.join(timeout=2)
        if self._proc.is_alive():
            self._proc.terminate()

    def describe(self) -> dict:
        return {
            "engine": self.engine,
            "model": self.model,
            "state": self.state,
            "error": self.error,
            "load_ms": self.load_ms,
            "last_latency_ms": self.last_latency_ms,
            "local_model": self.model_dir is not None,
            "gpu": self.use_gpu if self.engine == "whisper.cpp" else False,
        }


def make_transcriber(settings) -> Transcriber:
    if settings.stt_engine == "none":
        t = Transcriber()
        t.error = "speech-to-text disabled (LOCKEDIN_STT_ENGINE=none); typed commands still work"
        return t
    if settings.stt_engine == "whisper.cpp":
        ggml = settings.models_dir / "whispercpp" / f"ggml-{settings.whisper_model}.bin"
        if importlib.util.find_spec("pywhispercpp") is not None and ggml.exists():
            return WhisperTranscriber(settings.whisper_model, settings.whisper_compute_type, settings.models_dir, engine="whisper.cpp",
                                      use_gpu=settings.whisper_cpp_gpu)
        log.warning("whisper.cpp unavailable (%s); using faster-whisper", "no weights at " + str(ggml) if not ggml.exists() else "pywhispercpp not installed")
    # find_spec, not import: importing here would load PyAV into this process (see stt_worker.py).
    if importlib.util.find_spec("faster_whisper") is None:
        t = Transcriber()
        t.error = "faster-whisper not installed; install with `uv sync --extra voice`"
        return t
    return WhisperTranscriber(settings.whisper_model, settings.whisper_compute_type, settings.models_dir)
