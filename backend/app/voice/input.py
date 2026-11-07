"""Microphone capture and voice-activity detection (hands-free modes only; push-to-talk
does not need one).

Two detectors with the same interface. ``SileroVAD`` (default since Milestone 5) is a
small neural model that ignores clicks, hums and other non-speech. ``EnergyVAD`` is the
Milestone 4 loudness detector with an adaptive noise floor and hysteresis: it also opens
for loud non-speech, which Whisper's output (and the hallucination filter in
transcription.py) then has to absorb. It stays as the fallback and for tests.
"""

from __future__ import annotations

import logging
import math
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
BLOCK = 480  # 30 ms
BLOCK_S = BLOCK / SAMPLE_RATE


def rms_db(block: np.ndarray) -> float:
    if block.size == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(np.square(block.astype(np.float64)))))
    return 20 * math.log10(max(rms, 1e-6))


@dataclass
class VADConfig:
    start_over_floor_db: float = 12.0
    min_speech_db: float = -46.0
    start_blocks: int = 3  # ~90 ms above threshold opens an utterance
    end_ms: int = 650  # silence that closes it
    preroll_ms: int = 300
    max_utterance_s: float = 8.0
    min_utterance_ms: int = 240  # of loud audio, excluding pre-roll and trailing silence
    # Noise floor = this percentile of block levels over the last floor_window_s. Speech
    # has gaps between syllables, so a low percentile tracks the room, not the talker,
    # and stationary noise (a loud hall) raises the floor within a few seconds.
    floor_percentile: float = 10.0
    floor_window_s: float = 3.0


class EnergyVAD:
    """Feed float32 blocks in [-1, 1]; get ("start", None) / ("end", audio) events."""

    def __init__(self, cfg: VADConfig | None = None):
        self.cfg = cfg or VADConfig()
        self.floor_db = -60.0
        self._hist: deque[float] = deque(maxlen=max(10, int(self.cfg.floor_window_s / BLOCK_S)))
        self.in_speech = False
        self._above = 0
        self._silence_blocks = 0
        self._loud_blocks = 0
        self._pre: deque[np.ndarray] = deque(maxlen=max(1, int(self.cfg.preroll_ms / 1000 / BLOCK_S)))
        self._buf: list[np.ndarray] = []
        self.last_db = -120.0

    def reset(self) -> None:
        self.in_speech = False
        self._above = 0
        self._silence_blocks = 0
        self._loud_blocks = 0
        self._pre.clear()
        self._buf = []

    def threshold_db(self) -> float:
        return max(self.cfg.min_speech_db, self.floor_db + self.cfg.start_over_floor_db)

    def process(self, block: np.ndarray) -> list[tuple[str, np.ndarray | None]]:
        cfg = self.cfg
        db = rms_db(block)
        self.last_db = db
        self._hist.append(db)
        self.floor_db = float(np.percentile(self._hist, cfg.floor_percentile)) if len(self._hist) >= 10 else min(self._hist)
        out: list[tuple[str, np.ndarray | None]] = []
        loud = db >= self.threshold_db()
        if not self.in_speech:
            self._pre.append(block)
            self._above = self._above + 1 if loud else 0
            if self._above >= cfg.start_blocks:
                self.in_speech = True
                self._buf = list(self._pre)
                self._silence_blocks = 0
                self._loud_blocks = self._above
                out.append(("start", None))
            return out
        self._buf.append(block)
        self._silence_blocks = 0 if loud else self._silence_blocks + 1
        self._loud_blocks += 1 if loud else 0
        duration = len(self._buf) * BLOCK_S
        if self._silence_blocks * BLOCK_S * 1000 >= cfg.end_ms or duration >= cfg.max_utterance_s:
            audio = np.concatenate(self._buf) if self._buf else np.zeros(0, dtype=np.float32)
            speech_s = self._loud_blocks * BLOCK_S
            self.reset()
            if speech_s * 1000 >= cfg.min_utterance_ms:
                out.append(("end", audio))
            else:
                out.append(("end", None))
        return out


def silero_model_path(models_dir: Path | None = None) -> Path | None:
    """Silero VAD v6 (ONNX, MIT): backend/models/vad/ (scripts/download_models.py copies it
    there), else the copy faster-whisper ships. Found without importing faster-whisper, which
    would load PyAV into this process (see stt_worker.py)."""
    import importlib.util

    if models_dir is not None and (models_dir / "vad" / "silero_vad_v6.onnx").exists():
        return models_dir / "vad" / "silero_vad_v6.onnx"
    spec = importlib.util.find_spec("faster_whisper")
    if spec is None or not spec.submodule_search_locations:
        return None
    path = Path(list(spec.submodule_search_locations)[0]) / "assets" / "silero_vad_v6.onnx"
    return path if path.exists() else None


class SileroVAD:
    """Speech detector for the hands-free modes: Silero VAD v6, a small neural model, instead
    of loudness. Measured on synthetic mixtures (docs/MODELS.md): keyboard-like clicks opened
    the energy detector 14-19 times in 20 and Silero 0 times; at louder noise Silero still
    found every utterance (energy: 17-18 of 20); ~0.12 ms of CPU per 32 ms frame.

    Same interface and settings as ``EnergyVAD`` (events, pre-roll, end silence, minimum
    speech), with hysteresis on the probability: an utterance opens at ``start_prob`` and stays
    open while the probability is above ``end_prob``. It does not tell the user's voice from
    other people's: that is what push-to-talk and the wake phrase are for."""

    FRAME = 512  # 32 ms at 16 kHz: the model's frame

    def __init__(self, model_path: Path, cfg: VADConfig | None = None, start_prob: float = 0.5, end_prob: float = 0.35):
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        self.sess = ort.InferenceSession(str(model_path), sess_options=opts, providers=["CPUExecutionProvider"])
        self.cfg = cfg or VADConfig()
        self.start_prob = start_prob
        self.end_prob = end_prob
        self.floor_db = -60.0
        self.last_db = -120.0
        self.last_prob = 0.0
        self._pre: deque[np.ndarray] = deque(maxlen=max(1, int(self.cfg.preroll_ms / 1000 / BLOCK_S)))
        self._hist: deque[float] = deque(maxlen=max(10, int(self.cfg.floor_window_s / BLOCK_S)))
        self.reset()

    def reset(self) -> None:
        self.in_speech = False
        self._above = 0
        self._silence_blocks = 0
        self._speech_blocks = 0
        self._buf: list[np.ndarray] = []
        self._pre.clear()
        self._pending = np.zeros(0, dtype=np.float32)
        self._h = np.zeros((1, 1, 128), dtype=np.float32)
        self._c = np.zeros((1, 1, 128), dtype=np.float32)
        self._ctx = np.zeros(64, dtype=np.float32)

    def threshold_db(self) -> float:  # for the dashboard meter, like EnergyVAD
        return max(self.cfg.min_speech_db, self.floor_db + self.cfg.start_over_floor_db)

    def _probability(self, block: np.ndarray) -> float:
        self._pending = np.concatenate([self._pending, block.astype(np.float32)])
        while len(self._pending) >= self.FRAME:
            frame, self._pending = self._pending[: self.FRAME], self._pending[self.FRAME :]
            x = np.concatenate([self._ctx, frame])[None, :]
            prob, self._h, self._c = self.sess.run(None, {"input": x, "h": self._h, "c": self._c})
            self._ctx = frame[-64:]
            self.last_prob = float(np.asarray(prob).reshape(-1)[0])
        return self.last_prob

    def process(self, block: np.ndarray) -> list[tuple[str, np.ndarray | None]]:
        cfg = self.cfg
        db = rms_db(block)
        self.last_db = db
        self._hist.append(db)
        self.floor_db = float(np.percentile(self._hist, cfg.floor_percentile)) if len(self._hist) >= 10 else min(self._hist)
        p = self._probability(block)
        out: list[tuple[str, np.ndarray | None]] = []
        if not self.in_speech:
            self._pre.append(block)
            self._above = self._above + 1 if p >= self.start_prob else 0
            if self._above >= cfg.start_blocks:
                self.in_speech = True
                self._buf = list(self._pre)
                self._silence_blocks = 0
                self._speech_blocks = self._above
                out.append(("start", None))
            return out
        self._buf.append(block)
        speech = p >= self.end_prob
        self._silence_blocks = 0 if speech else self._silence_blocks + 1
        self._speech_blocks += 1 if speech else 0
        duration = len(self._buf) * BLOCK_S
        if self._silence_blocks * BLOCK_S * 1000 >= cfg.end_ms or duration >= cfg.max_utterance_s:
            audio = np.concatenate(self._buf) if self._buf else np.zeros(0, dtype=np.float32)
            speech_ms = self._speech_blocks * BLOCK_S * 1000
            self.in_speech = False
            self._above = 0
            self._buf = []
            self._pre.clear()
            out.append(("end", audio if speech_ms >= cfg.min_utterance_ms else None))
        return out


def make_vad(engine: str = "silero", models_dir: Path | None = None):
    """Silero when asked for and available, else the energy detector (logged)."""
    if engine == "silero":
        path = silero_model_path(models_dir)
        if path is not None:
            try:
                return SileroVAD(path)
            except Exception as exc:
                log.warning("Silero VAD unavailable (%s); using the energy detector", exc)
        else:
            log.warning("Silero VAD model not found; using the energy detector")
    return EnergyVAD()


class AudioCapture:
    """sounddevice input stream -> bounded queue of float32 blocks."""

    def __init__(self, device: int | str | None = None):
        self.device = device
        self.queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=400)
        self._stream = None
        self.overflows = 0
        self.dropped = 0
        self.started_at: float | None = None

    def start(self) -> None:
        import sounddevice as sd

        def callback(indata, frames, t, status):
            if status and status.input_overflow:
                self.overflows += 1
            block = indata[:, 0].copy()
            try:
                self.queue.put_nowait(block)
            except queue.Full:
                self.dropped += 1

        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE, blocksize=BLOCK, channels=1, dtype="float32", device=self.device, callback=callback
        )
        self._stream.start()
        self.started_at = time.monotonic()

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            finally:
                self._stream = None


def measure_input_level(device: int | str | None, seconds: float = 2.0) -> dict:
    """Record briefly and report levels. All-zero input on macOS usually means the
    app running the backend has no microphone permission."""
    import sounddevice as sd

    frames = int(seconds * SAMPLE_RATE)
    audio = sd.rec(frames, samplerate=SAMPLE_RATE, channels=1, dtype="float32", device=device)
    sd.wait()
    a = audio[:, 0]
    blocks = [a[i : i + BLOCK] for i in range(0, len(a) - BLOCK + 1, BLOCK)]
    dbs = [rms_db(b) for b in blocks] or [-120.0]
    peak = float(np.max(np.abs(a))) if a.size else 0.0
    vad = EnergyVAD()
    speech = any(kind == "start" for b in blocks for kind, _ in vad.process(b))
    silent = peak == 0.0
    return {
        "rms_db": round(float(np.median(dbs)), 1),
        "peak_db": round(20 * math.log10(max(peak, 1e-6)), 1),
        "speech_detected": speech,
        "all_zero": silent,
        "hint": "Input is exactly zero: grant Microphone access to the app running the backend (System Settings > Privacy & Security > Microphone)." if silent else None,
        "device": device,
    }


class LevelMeter:
    """Thread-safe latest level for the dashboard VU meter."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._db = -120.0

    def set(self, db: float) -> None:
        with self._lock:
            self._db = db

    def get(self) -> float:
        with self._lock:
            return self._db
