"""Local neural voice: Kokoro-82M v1.0 through kokoro-onnx (ONNX Runtime, CPU), offline.

Why it exists: the Fish Audio character voice is only available for lines generated while
online (the cached voice pack). A reply the language model writes on the robot's offline
Wi-Fi has no clip, and the macOS voice sounds like a screen reader. Kokoro renders a short
sentence in a few hundred milliseconds on this Mac (docs/MODELS.md) and sounds like a
person. It is NOT the Fish Audio voice: every line is labelled with the voice that
actually spoke it, and the voice was picked as the closest Kokoro preset to the Fish clips
by measured pitch and pace, not by ear.

Fixed lines are rendered once and kept on disk (they contain no user data). Generated
replies are rendered on demand and kept only in memory (a small LRU): they can contain
what the user said.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import threading
import time
import wave
from collections import OrderedDict
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_MODEL = "kokoro-v1.0.int8.onnx"
DEFAULT_VOICE = "af_bella"


class KokoroTTS:
    """Same rendering interface as ``CachedSystemTTS`` (clip_for / render / ensure / prewarm),
    so the voice router plays its clips through the same in-process output."""

    name = "kokoro"
    DYNAMIC_KEEP = 32

    def __init__(self, models_dir: Path, cache_dir: Path, voice: str = DEFAULT_VOICE, speed: float = 1.0, model_file: str = DEFAULT_MODEL):
        self.model_path = Path(models_dir) / "kokoro" / model_file
        self.voices_path = Path(models_dir) / "kokoro" / "voices-v1.0.bin"
        self.cache_dir = Path(cache_dir)
        self.voice = voice
        self.speed = speed
        self.state = "not loaded"
        self.error: str | None = None
        self.load_ms: float | None = None
        self._k = None
        self._lock = threading.Lock()  # one render at a time (one ONNX session)
        self._demand = 0
        self._pcm: dict[str, tuple] = {}
        self._dynamic: OrderedDict[str, tuple] = OrderedDict()
        self.renders = 0
        self.render_ms: list[float] = []
        self.prewarmed = 0
        self.prewarm_cancelled = False
        if not self.model_path.exists() or not self.voices_path.exists():
            self.state, self.error = "unavailable", f"Kokoro model not downloaded ({self.model_path.name}); scripts/download_models.py"

    # ------------------------------------------------------------------ lifecycle
    def load(self) -> bool:
        """Blocking (~1 s); call from a worker thread at startup."""
        if self.state == "ready":
            return True
        if self.state == "unavailable":
            return False
        t0 = time.perf_counter()
        try:
            from kokoro_onnx import Kokoro

            k = Kokoro(str(self.model_path), str(self.voices_path))
            if self.voice not in k.get_voices():
                raise ValueError(f"unknown Kokoro voice {self.voice!r}")
            k.create("Ready.", voice=self.voice, speed=self.speed)  # first inference allocates
        except Exception as exc:
            self.state, self.error = "error", f"Kokoro failed to load: {exc}"
            log.warning(self.error)
            return False
        self._k = k
        self.load_ms = round((time.perf_counter() - t0) * 1000)
        self.state = "ready"
        log.info("Kokoro voice %s ready in %s ms", self.voice, self.load_ms)
        return True

    def available(self) -> bool:
        return self.state == "ready"

    def set_voice(self, voice: str | None = None, speed: float | None = None) -> None:
        changed = (voice is not None and voice != self.voice) or (speed is not None and abs(speed - self.speed) > 1e-6)
        if voice is not None:
            self.voice = voice
        if speed is not None:
            self.speed = speed
        if changed:
            self._pcm.clear()
            with self._lock:
                self._dynamic.clear()

    # ------------------------------------------------------------------ rendering
    def _path(self, text: str) -> Path:
        key = hashlib.sha1(f"{self.model_path.name}|{self.voice}|{self.speed:.2f}|{text}".encode()).hexdigest()[:16]
        return self.cache_dir / f"{key}.wav"

    def _synth(self, text: str):
        import numpy as np

        t0 = time.perf_counter()
        with self._lock:
            audio, sr = self._k.create(text, voice=self.voice, speed=self.speed)
        ms = (time.perf_counter() - t0) * 1000
        self.renders += 1
        self.render_ms = (self.render_ms + [ms])[-50:]
        pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
        return pcm, int(sr)

    def clip_for(self, text: str):
        """(pcm, sample_rate) if already rendered, else None. Never blocks on a render."""
        item = self._pcm.get(text)
        if item is not None:
            return item
        item = self._dynamic.get(text)
        if item is not None:
            return item
        path = self._path(text)
        if path.exists():
            try:
                with wave.open(str(path)) as w:
                    import numpy as np

                    item = (np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16), w.getframerate())
                self._pcm[text] = item
                return item
            except Exception:
                return None
        return None

    def _keep(self, text: str, clip) -> None:
        self._dynamic[text] = clip
        self._dynamic.move_to_end(text)
        while len(self._dynamic) > self.DYNAMIC_KEEP:
            self._dynamic.popitem(last=False)

    def render(self, text: str, persist: bool = False):
        """Blocking: render a line someone is waiting for. Generated lines stay in memory only."""
        if not self.available():
            return None
        self._demand += 1
        try:
            clip = self._synth(text)
        finally:
            self._demand -= 1
        if persist:
            self._write(text, clip)
            self._pcm[text] = clip
        else:
            self._keep(text, clip)
        return clip

    def _write(self, text: str, clip) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self._path(text)
        part = path.with_name(path.name + ".part")
        try:
            with wave.open(str(part), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(clip[1])
                w.writeframes(clip[0].tobytes())
            os.replace(part, path)
        except Exception as exc:
            log.debug("could not cache Kokoro clip: %s", exc)
            with contextlib.suppress(OSError):
                os.unlink(part)

    def ensure(self, texts: list[str]) -> int:
        """Blocking (worker thread): render lines likely to be needed soon, in memory."""
        done = 0
        for text in texts:
            if not self.available() or self.prewarm_cancelled:
                break
            if self.clip_for(text) is not None:
                continue
            while self._demand and not self.prewarm_cancelled:
                time.sleep(0.03)
            try:
                self._keep(text, self._synth(text))
                done += 1
            except Exception as exc:
                log.debug("Kokoro render ahead failed: %s", exc)
        return done

    def prewarm(self, lines: list[str]) -> None:
        """Blocking (worker thread): fixed lines, rendered once and kept on disk."""
        if not self.available():
            return
        self.prewarm_cancelled = False
        for text in lines:
            if self.prewarm_cancelled:
                return
            if self.clip_for(text) is not None:
                self.prewarmed += 1
                continue
            while self._demand and not self.prewarm_cancelled:
                time.sleep(0.03)
            try:
                clip = self._synth(text)
            except Exception as exc:
                log.debug("Kokoro prewarm failed for %r: %s", text, exc)
                continue
            self._write(text, clip)
            self._pcm[text] = clip
            self.prewarmed += 1

    def describe(self) -> dict:
        ms = sorted(self.render_ms)
        return {
            "engine": self.name,
            "state": self.state,
            "error": self.error,
            "available": self.available(),
            "model": self.model_path.name,
            "voice": self.voice,
            "speed": self.speed,
            "load_ms": self.load_ms,
            "renders": self.renders,
            "render_p50_ms": round(ms[len(ms) // 2], 1) if ms else None,
            "prewarmed": self.prewarmed,
            "label": f"Kokoro {self.voice} (local, not the Fish Audio voice)",
        }
