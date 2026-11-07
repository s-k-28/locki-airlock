"""Speech output: engines, the voice router and the priority queue.

Engines:
* ``NullTTS``: text only (the dashboard still shows every line).
* ``SystemTTS`` / ``CachedSystemTTS``: the operating system's voice (``say`` on macOS,
  ``espeak`` on Linux, SAPI on Windows): free, offline. Fixed lines are pre-rendered.
* ``VoiceRouter``: follows the active voice profile. With Fish Audio selected it plays a
  cached Fish Audio clip, generates one live only when the operator allowed it (and the
  network, the key and the budget agree), and otherwise falls back to the local voice:
  Kokoro (kokoro_tts.py, offline neural voice) when it is loaded, else the system voice.
  Safety lines never wait for the network.

``Speaker`` is the single voice. Priorities:

* P0 (0) stop, e-stop, release, mute: cuts whatever is playing, clears everything else.
* P1 (1) essential status ("time's up", "robot disconnected").
* P2 (2) answers to the user.
* P3 (3) jokes, celebrations, ambient remarks: at most one waits, short shelf life.

An interrupt bumps an epoch; a line queued before it can never play after it, so an
outdated joke cannot follow an emergency stop. Every line ends up in ``history`` with the
source that actually spoke it (cached Fish Audio, live Fish Audio, local voice, text only).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import platform
import shutil
import threading
import time
from abc import ABC, abstractmethod
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..telemetry.events import EventBus, EventSource, EventType

log = logging.getLogger(__name__)

P0_SAFETY = 0
P1_ESSENTIAL = 1
P2_REPLY = 2
P3_AMBIENT = 3
PRIORITY_SAFETY = P0_SAFETY
PRIORITY_ESSENTIAL = P1_ESSENTIAL
PRIORITY_REPLY = P2_REPLY
PRIORITY_AMBIENT = P3_AMBIENT
PRIORITY_NAMES = {0: "P0", 1: "P1", 2: "P2", 3: "P3"}
# How long a line may wait before it is no longer worth saying.
MAX_AGE_S = {P0_SAFETY: 10.0, P1_ESSENTIAL: 8.0, P2_REPLY: 6.0, P3_AMBIENT: 4.0}
DUPLICATE_WINDOW_S = 2.0
DEVICE_RETRY_S = 15.0

# Where a spoken line actually came from (shown in the dashboard).
SOURCE_LABELS = {
    "fish_cache": "Cached Fish Audio",
    "fish_live": "Live Fish Audio",
    "system_cache": "Local voice (cached)",
    "system_live": "Local voice",
    "kokoro_cache": "Local Kokoro voice (pre-rendered)",
    "kokoro_live": "Local Kokoro voice (rendered now)",
    "text_only": "Text only",
    "none": "Not spoken",
}


class AudioDeviceError(RuntimeError):
    """The output device refused to play (unplugged Bluetooth speaker, busy device...)."""


@dataclass
class SpeechRequest:
    text: str  # what is shown and what the local voice says
    cue: str | None = None  # delivery hint a cloud voice may use ("excited"); never shown
    priority: int = P2_REPLY
    category: str = ""
    cacheable: bool = False  # a fixed template line: may be stored on disk
    allow_live: bool = True  # P0 lines never wait for a network round trip
    # What this line answers, for latency: {"kind": "turn" | "reach" | ..., "id": ..., "t0": monotonic}
    trace: dict | None = None


@dataclass
class SpeechResult:
    source: str
    detail: str = ""
    first_audio_ms: float | None = None
    # When the audio really started (time.monotonic(), just after the output stream started)
    # and the output device's reported latency on top of that (Bluetooth adds ~0.1-0.3 s).
    started_mono: float | None = None
    output_latency_ms: float | None = None


class TTSEngine(ABC):
    name = "base"

    @abstractmethod
    def available(self) -> bool: ...

    @abstractmethod
    async def speak(self, text: str) -> None:
        """Speak and return when finished. Must be cancellable (task.cancel())."""

    async def speak_request(self, req: SpeechRequest) -> SpeechResult:
        await self.speak(req.text)
        return SpeechResult(self.name)

    def describe(self) -> dict:
        return {"engine": self.name, "available": self.available()}


class NullTTS(TTSEngine):
    name = "text-only"

    def available(self) -> bool:
        return True

    async def speak(self, text: str) -> None:
        await asyncio.sleep(0)

    async def speak_request(self, req: SpeechRequest) -> SpeechResult:
        await asyncio.sleep(0)
        return SpeechResult("text_only")


class AudioOut:
    """In-process playback through PortAudio: ~10 ms to first sound, volume applied here so
    cached clips stay volume-independent, cancellable, device errors mapped."""

    last_start_mono: float | None = None
    last_latency_ms: float | None = None

    async def play(self, pcm, sample_rate: int, device: str | int | None = None, volume: float = 1.0) -> None:
        import numpy as np
        import sounddevice as sd

        data = pcm
        if abs(volume - 1.0) > 1e-3:
            data = np.clip(pcm.astype(np.float32) * volume, -32768, 32767).astype(np.int16)
        try:
            sd.play(data, sample_rate, device=device if device not in ("", None) else None)
        except Exception as exc:
            raise AudioDeviceError(f"output device {device or 'default'} unavailable: {exc}") from exc
        self.last_start_mono = time.monotonic()
        try:
            self.last_latency_ms = round(float(sd.get_stream().latency) * 1000, 1)
        except Exception:
            self.last_latency_ms = None
        try:
            await asyncio.sleep(len(data) / sample_rate + 0.05)
        except asyncio.CancelledError:
            sd.stop()
            raise


def parse_volume_settings(text: str) -> dict | None:
    """``osascript -e 'get volume settings'`` -> {"volume": 16, "muted": True}. volume is None
    when the device has no software volume ("missing value")."""
    fields = {}
    for part in text.split(","):
        key, _, value = part.strip().partition(":")
        fields[key.strip()] = value.strip()
    if "output muted" not in fields and "output volume" not in fields:
        return None
    vol = fields.get("output volume", "")
    return {"volume": int(vol) if vol.isdigit() else None, "muted": fields.get("output muted") == "true"}


async def system_output_volume() -> dict | None:
    """macOS volume and mute switch of the *default* output device; None elsewhere or on error.
    A muted Bluetooth speaker still accepts audio, so playback alone never reveals it."""
    if platform.system() != "Darwin" or shutil.which("osascript") is None:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", "get volume settings", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
    except OSError:
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=3.0)
    except TimeoutError:
        proc.kill()
        return None
    return parse_volume_settings(out.decode(errors="ignore"))


class SystemTTS(TTSEngine):
    """Uses the OS speech command in a subprocess so it never blocks the loop."""

    name = "system"

    def __init__(self, voice: str | None = None, rate_wpm: int = 190, output_device: str | None = None):
        self.voice = voice
        self.rate_wpm = rate_wpm
        self.output_device = output_device
        self.system = platform.system()

    def available(self) -> bool:
        if self.system == "Darwin":
            return shutil.which("say") is not None
        if self.system == "Linux":
            return shutil.which("espeak-ng") is not None or shutil.which("espeak") is not None
        if self.system == "Windows":
            return shutil.which("powershell") is not None
        return False

    def command(self, text: str) -> list[str]:
        if self.system == "Darwin":
            cmd = ["say", "-r", str(self.rate_wpm)]
            if self.voice:
                cmd += ["-v", self.voice]
            if self.output_device:
                cmd += ["-a", self.output_device]
            return cmd + ["--", text]
        if self.system == "Linux":
            exe = shutil.which("espeak-ng") or shutil.which("espeak") or "espeak"
            return [exe, "-s", str(self.rate_wpm), text]
        safe = text.replace("'", "''")
        script = (
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            f"$s.Rate = {max(-10, min(10, (self.rate_wpm - 180) // 15))}; $s.Speak('{safe}')"
        )
        return ["powershell", "-NoProfile", "-Command", script]

    async def speak(self, text: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            *self.command(text), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        try:
            _, err = await proc.communicate()
            if proc.returncode not in (0, None):
                raise RuntimeError(f"speech command failed ({proc.returncode}): {err.decode(errors='ignore')[:200]}")
        except asyncio.CancelledError:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=0.5)
                except TimeoutError:
                    proc.kill()
            raise

    async def speak_request(self, req: SpeechRequest) -> SpeechResult:
        await self.speak(req.text)
        return SpeechResult("system_live")

    def describe(self) -> dict:
        return {**super().describe(), "voice": self.voice, "rate_wpm": self.rate_wpm, "output_device": self.output_device}


class CachedSystemTTS(SystemTTS):
    """macOS `say` has ~0.75-1.5 s of fixed startup per call (measured; docs/PERFORMANCE.md).
    Fixed template lines are rendered once to WAV in the background and played in-process
    through PortAudio, which starts in ~10 ms and can target the selected output device
    (e.g. the Bluetooth speaker). Lines containing live numbers are rendered on demand, or
    ahead of time with ``ensure`` when the runtime can predict them ("24 minutes left.").

    A render someone is waiting for goes first: the background prewarm pauses between lines
    while one runs (a concurrent `say` adds about 0.5 s; measured 2026-11-01)."""

    name = "system-cached"
    DYNAMIC_KEEP = 16  # recent live-number lines kept in memory (not on disk)

    def __init__(self, cache_dir, voice: str | None = None, rate_wpm: int = 190, output_device: str | None = None):
        super().__init__(voice=voice, rate_wpm=rate_wpm, output_device=output_device)
        from pathlib import Path

        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._pcm: dict[str, tuple] = {}
        self.hits = 0
        self.misses = 0
        self.prewarmed = 0
        self.rendered_ahead = 0
        self.last_start_ms: float | None = None
        self.prewarm_cancelled = False
        self._dynamic: OrderedDict[str, tuple] = OrderedDict()
        self._lock = threading.Lock()
        self._demand = 0  # on-demand renders in progress; the prewarm waits for them

    def _path(self, text: str):
        import hashlib

        key = hashlib.sha1(f"{self.voice}|{self.rate_wpm}|{text}".encode()).hexdigest()[:16]
        return self.cache_dir / f"{key}.wav"

    def _load(self, text: str):
        import wave

        import numpy as np

        if text in self._pcm:
            return self._pcm[text]
        path = self._path(text)
        if not path.exists():
            return None
        with wave.open(str(path)) as w:
            data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
            item = (data, w.getframerate())
        self._pcm[text] = item
        return item

    def clip_for(self, text: str):
        """(pcm, sample_rate) if this line was pre-rendered, else None. Never blocks on `say`."""
        item = self._pcm.get(text)
        if item is None:
            with self._lock:
                item = self._dynamic.get(text)
        return item

    def _say_to(self, text: str, path: str) -> None:
        import subprocess

        cmd = ["say", "-r", str(self.rate_wpm)] + (["-v", self.voice] if self.voice else [])
        cmd += ["--file-format=WAVE", "--data-format=LEI16@22050", "-o", path, "--", text]
        subprocess.run(cmd, check=True, capture_output=True, timeout=20)

    def _render_tmp(self, text: str):
        import os
        import tempfile
        import wave

        import numpy as np

        fd, tmp = tempfile.mkstemp(suffix=".wav", prefix="lockedin-say-")
        os.close(fd)
        try:
            self._say_to(text, tmp)
            with wave.open(tmp) as w:
                return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16), w.getframerate()
        finally:
            with contextlib.suppress(OSError):
                os.unlink(tmp)

    def _keep(self, text: str, clip) -> None:
        with self._lock:
            self._dynamic[text] = clip
            self._dynamic.move_to_end(text)
            while len(self._dynamic) > self.DYNAMIC_KEEP:
                self._dynamic.popitem(last=False)

    def _wait_for_demand(self) -> None:
        while self._demand and not self.prewarm_cancelled:
            time.sleep(0.05)

    def render(self, text: str):
        """Blocking: render one line someone is waiting for. Kept in memory (a few lines) so
        asking again is instant; background renders pause until it is done."""
        if self.system != "Darwin" or not self.available():
            return None
        with self._lock:
            self._demand += 1
        try:
            clip = self._render_tmp(text)
        finally:
            with self._lock:
                self._demand -= 1
        self._keep(text, clip)
        return clip

    def ensure(self, texts: list[str]) -> int:
        """Blocking; run in a worker thread. Render lines with live numbers that will probably
        be asked for soon, so they start in ~10 ms instead of waiting 1.4-2 s for `say`.
        Returns how many lines were rendered."""
        if self.system != "Darwin" or not self.available():
            return 0
        done = 0
        for text in texts:
            if self.clip_for(text) is not None:
                continue
            self._wait_for_demand()
            if self.prewarm_cancelled:
                break
            try:
                self._keep(text, self._render_tmp(text))
            except Exception as exc:
                log.debug("render ahead failed for %r: %s", text, exc)
                continue
            done += 1
        self.rendered_ahead += done
        return done

    def prewarm(self, lines: list[str]) -> None:
        """Blocking; run in a worker thread. Lines are rendered in the order given."""
        import os

        if self.system != "Darwin" or not self.available():
            return
        self.prewarm_cancelled = False
        for text in lines:
            self._wait_for_demand()
            if self.prewarm_cancelled:  # backend shutting down: don't hold the exit for `say`
                return
            path = self._path(text)
            if not path.exists():
                part = path.with_name(path.name + ".part")  # a killed render leaves no half clip
                try:
                    self._say_to(text, str(part))
                    os.replace(part, path)
                except Exception as exc:
                    log.debug("prewarm failed for %r: %s", text, exc)
                    with contextlib.suppress(OSError):
                        os.unlink(part)
                    continue
            try:
                self._load(text)
                self.prewarmed += 1
            except Exception as exc:
                log.debug("could not decode cached line %r: %s", text, exc)

    async def speak(self, text: str) -> None:
        item = self.clip_for(text)
        if item is not None:
            try:
                import sounddevice as sd
            except Exception:
                sd = None
            if sd is not None:
                data, sr = item
                self.hits += 1
                t0 = time.perf_counter()
                sd.play(data, sr, device=self.output_device or None)
                self.last_start_ms = round((time.perf_counter() - t0) * 1000, 1)
                try:
                    await asyncio.sleep(len(data) / sr + 0.08)
                except asyncio.CancelledError:
                    sd.stop()
                    raise
                return
        self.misses += 1
        await super().speak(text)

    async def speak_request(self, req: SpeechRequest) -> SpeechResult:
        cached = self.clip_for(req.text) is not None
        await self.speak(req.text)
        return SpeechResult("system_cache" if cached else "system_live")

    def describe(self) -> dict:
        return {
            **super().describe(),
            "cache_hits": self.hits,
            "cache_misses": self.misses,
            "prewarmed": self.prewarmed,
            "rendered_ahead": self.rendered_ahead,
            "last_start_ms": self.last_start_ms,
        }


class VoiceRouter(TTSEngine):
    """Speaks with the active voice profile (see module docstring).

    ``profile`` returns the current ``VoiceProfile``; ``cloud`` is a ``FishAudioVoice`` (or
    None). Switching profile or preset takes effect on the next line, no restart."""

    name = "router"

    def __init__(
        self,
        profile: Callable[[], Any],
        system: TTSEngine,
        cloud: Any = None,
        audio: AudioOut | None = None,
        output_device: Callable[[], str | int | None] = lambda: None,
        neural=None,
    ):
        self.profile = profile
        self.system = system
        self.cloud = cloud
        self.audio = audio or AudioOut()
        self.output_device = output_device
        self.neural = neural  # KokoroTTS or None
        self.last_fallback: str | None = None

    def available(self) -> bool:
        return True

    async def speak(self, text: str) -> None:
        await self.speak_request(SpeechRequest(text))

    async def speak_request(self, req: SpeechRequest) -> SpeechResult:
        p = self.profile()
        if p.provider == "none":
            await asyncio.sleep(0)
            return SpeechResult("text_only", "voice provider: text only")
        note = ""
        if p.provider == "fish_audio":
            # A cached clip needs neither the key nor the network: only live generation does
            # (live_refusal() says "no Fish Audio API key" for that case).
            if self.cloud is None:
                note = "Fish Audio unavailable"
            elif not p.reference_id:
                note = "Fish Audio: no voice selected"
            else:
                tts_text = self.cloud.text_for(req, p)
                clip = self.cloud.cached(tts_text, p)
                if clip is not None:
                    await self.audio.play(clip[0], clip[1], self.output_device(), p.volume)
                    return self._started(SpeechResult("fish_cache"))
                reason = self.cloud.live_refusal(req, p)
                if reason is None:
                    t0 = time.monotonic()
                    try:
                        clip = await self.cloud.generate(tts_text, p, persist=req.cacheable, purpose="live")
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        note = f"Fish Audio unavailable: {getattr(exc, 'message', None) or exc}"
                    else:
                        first = round((time.monotonic() - t0) * 1000, 1)
                        await self.audio.play(clip[0], clip[1], self.output_device(), p.volume)
                        return self._started(SpeechResult("fish_live", first_audio_ms=first))
                else:
                    note = f"not cached; {reason}"
        self.last_fallback = note or None
        result = await self._system(req, p)
        if note:
            result.detail = note
        return result

    def _started(self, result: SpeechResult) -> SpeechResult:
        result.started_mono = getattr(self.audio, "last_start_mono", None)
        result.output_latency_ms = getattr(self.audio, "last_latency_ms", None)
        return result

    async def _system(self, req: SpeechRequest, p) -> SpeechResult:
        neural = self.neural
        if neural is not None and getattr(p, "local_voice", "kokoro") == "kokoro" and neural.available():
            clip = neural.clip_for(req.text)
            source, render_ms = "kokoro_cache", None
            if clip is None:
                source = "kokoro_live"
                t0 = time.monotonic()
                try:
                    clip = await asyncio.to_thread(neural.render, req.text, req.cacheable)
                except Exception as exc:
                    log.debug("Kokoro render failed: %s", exc)
                    clip = None
                render_ms = round((time.monotonic() - t0) * 1000, 1)
            if clip is not None:
                await self.audio.play(clip[0], clip[1], self.output_device(), p.volume)
                return self._started(SpeechResult(source, first_audio_ms=render_ms))
        sys_engine = self.system
        if isinstance(sys_engine, CachedSystemTTS):
            clip = sys_engine.clip_for(req.text)
            source, render_ms = "system_cache", None
            if clip is None:
                source = "system_live"
                t0 = time.monotonic()
                try:
                    clip = await asyncio.to_thread(sys_engine.render, req.text)
                except Exception as exc:
                    log.debug("say render failed: %s", exc)
                    clip = None
                render_ms = round((time.monotonic() - t0) * 1000, 1)
            if clip is not None:
                await self.audio.play(clip[0], clip[1], self.output_device(), p.volume)
                return self._started(SpeechResult(source, first_audio_ms=render_ms))
        if sys_engine.available() and not isinstance(sys_engine, NullTTS):
            return await sys_engine.speak_request(req)
        await asyncio.sleep(0)
        return SpeechResult("text_only", "no local voice available")

    def describe(self) -> dict:
        p = self.profile()
        return {
            "engine": f"router:{p.provider}",
            "available": True,
            "provider": p.provider,
            "system": self.system.describe(),
            "neural": None if self.neural is None else self.neural.describe(),
            "cloud": None if self.cloud is None else self.cloud.describe(),
            "last_fallback": self.last_fallback,
        }


@dataclass(order=True)
class _Utterance:
    priority: int
    seq: int
    req: SpeechRequest = field(compare=False)
    created: float = field(compare=False, default=0.0)
    max_age_s: float = field(compare=False, default=6.0)
    epoch: int = field(compare=False, default=0)
    voice: str | None = field(compare=False, default=None)
    not_before: float = field(compare=False, default=0.0)  # a comedic beat before the line


def _being_cancelled() -> bool:
    """True when the *current* task has been asked to cancel (vs. a child task we awaited)."""
    task = asyncio.current_task()
    return bool(task and task.cancelling())


def _echo_words(s: str) -> list[str]:
    import re

    return re.findall(r"[a-z0-9']+", re.sub(r"\[[^\]]*\]", " ", s.lower()))


def echo_split(heard: str, spoken: str) -> tuple[float, str]:
    """(how much of ``heard`` is the robot's own line ``spoken``, 0..1; the heard words that are
    not in it). Two measures, the larger counts:

    * share: the heard words the robot just said ("emergency stop" over the robot's own
      "Emergency stop. I'm not moving." is all echo);
    * coverage: the robot's line found in what was heard, for lines of 3+ words. Whisper adds
      words in the quiet before and after a recording ("I found one too. Emergency stop. I'm
      not moving.": share 0.56, coverage 1.0), which share alone misses (measured on the
      real JLab speaker, docs/AUDIO_TESTS.md A6). One- and two-word lines ("Stopped.") never
      use coverage, so the user's own "stop" is never mistaken for them.
    """
    h, sp = _echo_words(heard), _echo_words(spoken)
    if not h or not sp:
        return 0.0, heard.strip()
    sps, hs = set(sp), set(h)
    share = sum(1 for w in h if w in sps) / len(h)
    coverage = sum(1 for w in sps if w in hs) / len(sps) if len(sps) >= 3 else 0.0
    residual = " ".join(w for w in h if w not in sps)
    return max(share, coverage), residual


def echo_similarity(heard: str, spoken: str) -> float:
    """Share of ``heard`` that is the robot's line (see ``echo_split``). 1.0 = pure echo."""
    return echo_split(heard, spoken)[0]


class Speaker:
    """The single voice (see module docstring for the priority rules)."""

    def __init__(
        self,
        engine: TTSEngine,
        bus: EventBus,
        fallback: TTSEngine | None = None,
        clock: Callable[[], float] = time.monotonic,
        voice_label: Callable[[], str | None] = lambda: None,
    ):
        self.engine = engine
        self.fallback = fallback
        self.bus = bus
        self._clock = clock
        self._voice_label = voice_label
        self._items: list[_Utterance] = []
        self._wake = asyncio.Event()
        self._seq = 0
        self.epoch = 0
        # One epoch per priority: an interrupt that keeps P0/P1 invalidates only P2/P3 lines
        # queued before it (including one the worker has already picked up).
        self._epochs = [0, 0, 0, 0]
        self._current: asyncio.Task | None = None
        self._current_utt: _Utterance | None = None
        self._worker: asyncio.Task | None = None
        self.muted = False
        self.speaking = False
        self.speaking_until: float = 0.0
        self.last_latency_ms: float | None = None
        self.spoken = 0
        self.dropped = 0
        self.history: deque[dict] = deque(maxlen=40)
        self._recent: deque[tuple[str, float]] = deque(maxlen=4)
        self._audio_down_until = 0.0
        self.audio_error: str | None = None

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._run(), name="tts-worker")

    async def close(self) -> None:
        self.stop_all("shutdown")
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None

    # ------------------------------------------------------------ queueing
    def say(
        self,
        text: str,
        *,
        priority: int = P2_REPLY,
        category: str = "",
        max_age_s: float | None = None,
        force: bool = False,
        cue: str | None = None,
        cacheable: bool = False,
        delay_s: float = 0.0,
        trace: dict | None = None,
    ) -> bool:
        """Queue a line. Returns False if it will not be spoken (muted, duplicate).
        ``force`` lines (stop / e-stop acknowledgements) are spoken even while muted."""
        if not text:
            return False
        now = self._clock()
        # Lines the local language model wrote never go to a cloud voice: they can carry what
        # the user said, and the runtime makes no external AI calls (Milestone 5).
        live_ok = priority != P0_SAFETY and not category.startswith("generated")
        req = SpeechRequest(text, cue, priority, category, cacheable, allow_live=live_ok, trace=trace)
        if self.muted and not force:
            self._log(req, "muted", source="none", at=now)
            return False
        if any(t == text and now - at < DUPLICATE_WINDOW_S for t, at in self._recent_queued()):
            self._log(req, "duplicate", source="none", at=now)
            return False
        if priority == P0_SAFETY:
            self._interrupt_below(P0_SAFETY, f"{category or 'safety'} line")
        else:
            cur = self._current_utt
            if cur is not None and cur.priority == P3_AMBIENT and priority < P3_AMBIENT and self._current is not None:
                self._current.cancel()  # an answer or status beats a joke
            if priority == P3_AMBIENT:
                for old in [u for u in self._items if u.priority == P3_AMBIENT]:
                    self._items.remove(old)  # one joke waits at most: the newest
                    self._log(old.req, "replaced", source="none", at=now)
        self._seq += 1
        age = MAX_AGE_S.get(priority, 6.0) if max_age_s is None else max_age_s
        self._items.append(
            _Utterance(priority, self._seq, req, now, age + delay_s, self._epochs[min(priority, 3)], self._voice_label(), now + max(0.0, delay_s))
        )
        self._items.sort()
        self._wake.set()
        return True

    def _recent_queued(self):
        out = [(u.req.text, u.created) for u in self._items]
        if self._current_utt is not None:
            out.append((self._current_utt.req.text, self._current_utt.created))
        out.extend(self._recent)
        return out

    def _interrupt_below(self, keep: int, reason: str) -> None:
        """Stop the current line and drop queued lines with priority > ``keep``."""
        self.epoch += 1
        for p in range(max(keep + 1, 0), 4):
            self._epochs[p] += 1
        now = self._clock()
        for u in [u for u in self._items if u.priority > keep]:
            self._items.remove(u)
            self._log(u.req, f"cancelled ({reason})", source="none", at=now)
        cur = self._current_utt
        if self._current is not None and cur is not None and cur.priority > keep:
            self._current.cancel()

    def stop_all(self, reason: str) -> None:
        """Mute / e-stop / stop: silence now, forget everything queued."""
        self._interrupt_below(-1, reason)

    def interrupt(self, reason: str = "interrupt") -> None:
        """Stop and drop everything except P0 safety lines."""
        self._interrupt_below(P0_SAFETY, reason)

    def barge_in(self) -> None:
        """The user started talking (push-to-talk): stop answers and jokes, keep status."""
        self._interrupt_below(P1_ESSENTIAL, "user started talking")

    def recently_spoke(self, within_s: float = 0.8) -> bool:
        """For echo handling: True while speaking and for a short tail afterwards."""
        return self.speaking or self._clock() < self.speaking_until + within_s

    def echo_score(self, heard: str, within_s: float = 3.0) -> float:
        """How much of ``heard`` is the robot's own recent speech (0..1)."""
        return self.echo_split(heard, within_s)[0]

    def echo_split(self, heard: str, within_s: float = 3.0) -> tuple[float, str]:
        """(echo score, the rest of what was heard) against the robot's recent lines."""
        now = self._clock()
        candidates = [t for t, ended in self._recent if now - ended <= within_s]
        if self._current_utt is not None:
            candidates.append(self._current_utt.req.text)
        return max((echo_split(heard, t) for t in candidates), key=lambda r: r[0], default=(0.0, heard.strip()))

    # ------------------------------------------------------------ worker
    async def _run(self) -> None:
        while True:
            while not self._items:
                self._wake.clear()
                await self._wake.wait()
            now = self._clock()
            ready = [u for u in self._items if u.not_before <= now]
            if not ready:  # only lines still in their comedic beat: wait, but wake for anything new
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=min(u.not_before for u in self._items) - now)
                continue
            utt = ready[0]
            self._items.remove(utt)
            if utt.epoch < self._epochs[min(utt.priority, 3)]:
                self._log(utt.req, "superseded", source="none", at=now)
                continue
            if now - utt.created > utt.max_age_s:
                self.dropped += 1
                self._log(utt.req, "stale", source="none", at=now)
                continue
            await self._speak_one(utt)

    async def _speak_one(self, utt: _Utterance) -> None:
        engine = self.engine if self.engine.available() else (self.fallback or NullTTS())
        self.speaking = True
        self._current_utt = utt
        started = self._clock()
        req = utt.req
        self.bus.publish(
            EventType.VOICE_TTS_STARTED,
            EventSource.VOICE,
            {"text": req.text, "engine": engine.name, "category": req.category, "priority": PRIORITY_NAMES.get(req.priority)},
        )
        outcome, source, detail, first = "finished", "text_only", "", None
        audio_start, out_latency = None, None
        try:
            if self._clock() < self._audio_down_until:
                source, detail = "text_only", f"audio output unavailable: {self.audio_error}"
            else:
                result = await self._run_engine(engine, req)
                source, detail, first = result.source, result.detail, result.first_audio_ms
                audio_start, out_latency = result.started_mono, result.output_latency_ms
        except asyncio.CancelledError:
            if _being_cancelled():
                raise  # the worker itself is shutting down, not just this line
            outcome = "interrupted"
        except AudioDeviceError as exc:
            outcome, detail = "device error", str(exc)
            self.audio_error = str(exc)
            self._audio_down_until = self._clock() + DEVICE_RETRY_S
            self.bus.publish(EventType.VOICE_ERROR, EventSource.VOICE, {"error": str(exc), "action": f"text only for {DEVICE_RETRY_S:.0f} s"})
        except Exception as exc:
            log.warning("TTS engine %s failed: %s", engine.name, exc)
            outcome, detail = "error", str(exc)
            if self.fallback is not None and engine is not self.fallback and self.fallback.available():
                try:
                    result = await self._run_engine(self.fallback, req)
                    source, outcome = result.source, f"fallback ({self.fallback.name})"
                except asyncio.CancelledError:
                    if _being_cancelled():
                        raise
                    outcome = "interrupted"
                except Exception:
                    pass
        finally:
            ended = self._clock()
            self._current = None
            self._current_utt = None
            self.speaking = False
            self.speaking_until = ended
            self.spoken += 1
            self.last_latency_ms = round((ended - started) * 1000, 1)
            self._recent.append((req.text, ended))
            if audio_start is None and source not in ("text_only", "none") and first is not None:
                audio_start = started + first / 1000.0
            trace = req.trace or {}
            to_audio = None if audio_start is None or trace.get("t0") is None else round((audio_start - trace["t0"]) * 1000, 1)
            self._log(
                req, outcome, source=source, at=ended, detail=detail, voice=utt.voice, queued_ms=round((started - utt.created) * 1000, 1),
                first_audio_ms=first, duration_ms=self.last_latency_ms, trace=trace.get("kind"), to_audio_ms=to_audio, output_latency_ms=out_latency,
            )
            self.bus.publish(
                EventType.VOICE_TTS_FINISHED,
                EventSource.VOICE,
                {
                    "engine": engine.name, "outcome": outcome, "duration_ms": self.last_latency_ms, "category": req.category,
                    "source": source, "source_label": SOURCE_LABELS.get(source, source), "detail": detail, "text": req.text,
                    "trace": {k: v for k, v in trace.items() if k != "t0"} or None, "to_audio_ms": to_audio, "output_latency_ms": out_latency,
                },
            )

    async def _run_engine(self, engine: TTSEngine, req: SpeechRequest) -> SpeechResult:
        self._current = asyncio.create_task(engine.speak_request(req))
        result = await self._current
        if result is None:  # engines written before SpeechResult existed
            result = SpeechResult(engine.name)
        if self._audio_down_until and result.source != "text_only":
            self._audio_down_until = 0.0
            self.audio_error = None
        return result

    # ------------------------------------------------------------ reporting
    def _log(self, req: SpeechRequest, outcome: str, *, source: str, at: float, **extra) -> None:
        self.history.append(
            {
                "at": time.time(),
                "text": req.text,
                "category": req.category,
                "priority": PRIORITY_NAMES.get(req.priority, str(req.priority)),
                "source": source,
                "source_label": SOURCE_LABELS.get(source, source),
                "outcome": outcome,
                **{k: v for k, v in extra.items() if v not in (None, "")},
            }
        )

    def output_status(self) -> dict:
        """What the last line actually did (the real output status, not the configured one)."""
        last = next((h for h in reversed(self.history) if h.get("source") not in ("none", None)), None)
        return {
            "error": self.audio_error if self._clock() < self._audio_down_until else None,
            "last_source": None if last is None else last.get("source_label"),
            "last_outcome": None if last is None else last.get("outcome"),
            "last_at": None if last is None else last.get("at"),
            "last_output_latency_ms": None if last is None else last.get("output_latency_ms"),
        }

    def snapshot(self) -> dict:
        return {
            **self.engine.describe(),
            "fallback": None if self.fallback is None else self.fallback.name,
            "muted": self.muted,
            "speaking": self.speaking,
            "current": None if self._current_utt is None else {"text": self._current_utt.req.text, "priority": PRIORITY_NAMES.get(self._current_utt.priority)},
            "spoken": self.spoken,
            "dropped_stale": self.dropped,
            "queue": len(self._items),
            "audio_error": self.audio_error if self._clock() < self._audio_down_until else None,
            "history": list(self.history)[-20:],
        }
