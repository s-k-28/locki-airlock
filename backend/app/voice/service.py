"""Voice input service: microphone -> VAD / push-to-talk -> Whisper -> runtime.

Modes:
  push_to_talk  audio between ptt_start() and ptt_stop() (+ a short tail) is transcribed;
                the most reliable mode in a loud hackathon hall
  wake_word     VAD segments everything; the runtime acts only on utterances that start
                with "locked in"/"lock in" (or arrive inside a short follow-up window)
  always        VAD segments everything and every utterance is interpreted

Echo handling: while the robot is speaking (and ~400 ms after), the hands-free modes keep
listening but mark what they hear as ``during_playback``. The runtime then drops anything
that matches the robot's own line (its echo through the speaker) and takes only stop /
mute / release / pause / "be serious" / "stop roasting me" from it, so the user can talk
over the robot without it reacting to itself. Push-to-talk always listens.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable

import numpy as np

from .input import BLOCK_S, AudioCapture, LevelMeter, make_vad, rms_db
from .transcription import Transcriber, is_hallucination, make_transcriber

log = logging.getLogger(__name__)

PTT_TAIL_S = 0.25
MIC_STALL_S = 3.0  # no blocks for this long = the input device is gone
PTT_MAX_S = 10.0
SILENT_INPUT_S = 2.5


class VoiceService:
    def __init__(self, settings, emit: Callable[[str, dict], None], speaker=None, transcriber: Transcriber | None = None,
                 on_transcribing: Callable[[bool], None] | None = None):
        self.settings = settings
        self.emit = emit
        self.speaker = speaker
        self.transcriber = transcriber
        self.capture: AudioCapture | None = None
        self.vad = make_vad(getattr(settings, "vad_engine", "silero"), getattr(settings, "models_dir", None))
        self.meter = LevelMeter()
        self.mode = "push_to_talk"
        self.device = None
        self.running = False
        self._thread: threading.Thread | None = None
        self._stt_q: queue.Queue[np.ndarray] = queue.Queue(maxsize=4)
        self._stt_thread: threading.Thread | None = None
        self._ptt_active = False
        self._ptt_buf: list[np.ndarray] = []
        self._ptt_stop_at: float | None = None
        self._zero_since: float | None = None
        self.input_warning: str | None = None
        self.utterances = 0
        self.dropped_echo_blocks = 0  # blocks heard while the robot spoke (checked for echo, not dropped)
        self._last_block_at: float | None = None
        self.stalled = False
        self._seg_overlap = False
        self.last_text: str | None = None
        self.transcribing = False  # Whisper is working on an utterance (dashboard: "thinking")
        self.on_transcribing = on_transcribing  # tells the language model worker to leave the GPU to Whisper

    # ------------------------------------------------------------------ control
    def start(self, device=None, mode: str = "push_to_talk") -> None:
        if self.running:
            self.stop()
        self.device = device
        self.mode = mode
        if self.transcriber is None:
            self.transcriber = make_transcriber(self.settings)
        self.capture = AudioCapture(device)
        self.capture.start()  # raises if the device cannot be opened
        self.running = True
        self.vad.reset()
        self._thread = threading.Thread(target=self._run, name="voice-input", daemon=True)
        self._thread.start()
        self._stt_thread = threading.Thread(target=self._stt_loop, name="voice-stt", daemon=True)
        self._stt_thread.start()

    def stop(self) -> None:
        self.running = False
        if self.capture is not None:
            self.capture.stop()
            self.capture = None
        for t in (self._thread, self._stt_thread):
            if t is not None:
                t.join(timeout=2)
        self._thread = self._stt_thread = None
        self._ptt_active = False

    def shutdown(self) -> None:
        self.stop()
        if self.transcriber is not None:
            self.transcriber.close()

    def set_mode(self, mode: str) -> None:
        if mode != self.mode:
            self.mode = mode
            self.vad.reset()
            self._ptt_active = False

    def ptt_start(self) -> None:
        if self.mode != "push_to_talk":
            return
        self._ptt_buf = []
        self._ptt_stop_at = None
        self._ptt_active = True
        self.emit("speech_started", {"mode": "push_to_talk"})

    def ptt_stop(self) -> None:
        if self._ptt_active and self._ptt_stop_at is None:
            self._ptt_stop_at = time.monotonic() + PTT_TAIL_S

    def level(self) -> dict:
        return {"rms_db": round(self.meter.get(), 1), "speaking": self.vad.in_speech or self._ptt_active}

    def snapshot(self) -> dict:
        return {
            "running": self.running,
            "mode": self.mode,
            "device": self.device,
            "ptt_active": self._ptt_active,
            "listening": self.vad.in_speech,
            "transcribing": self.transcribing,
            "stt": self.transcriber.describe() if self.transcriber else {"engine": "none", "state": "unavailable"},
            "utterances": self.utterances,
            "dropped_echo_blocks": self.dropped_echo_blocks,
            "noise_floor_db": round(self.vad.floor_db, 1),
            "vad": type(self.vad).__name__,
            "speech_prob": round(getattr(self.vad, "last_prob", 0.0), 2) if hasattr(self.vad, "last_prob") else None,
            "warning": self.input_warning,
            "stalled": self.stalled,
            "reason": None if self.running else "voice input not started",
        }

    # ------------------------------------------------------------------ threads
    def _run(self) -> None:
        cap = self.capture
        while self.running and cap is not None:
            try:
                block = cap.queue.get(timeout=0.2)
            except queue.Empty:
                self._maybe_finish_ptt()
                self._check_stalled()
                continue
            self._last_block_at = time.monotonic()
            if self.stalled:
                self.stalled = False
                self.input_warning = None
                self.emit("recovered", {"message": "microphone is delivering audio again"})
            db = rms_db(block)
            self.meter.set(db)
            self._check_silent_input(block)
            if self.mode == "push_to_talk":
                if self._ptt_active:
                    self._ptt_buf.append(block)
                    if len(self._ptt_buf) * BLOCK_S > PTT_MAX_S:
                        self._ptt_stop_at = time.monotonic()
                self._maybe_finish_ptt()
                continue
            during = self.speaker is not None and self.speaker.recently_spoke()
            if during:
                self.dropped_echo_blocks += 1
            for kind, audio in self.vad.process(block):
                if kind == "start":
                    self._seg_overlap = during
                    self.emit("speech_started", {"mode": self.mode})
                elif kind == "end":
                    self.emit("speech_ended", {"mode": self.mode, "kept": audio is not None})
                    if audio is not None:
                        self._submit(audio, during_playback=self._seg_overlap or during)
                    self._seg_overlap = False
            if during and self.vad.in_speech:
                self._seg_overlap = True

    def _maybe_finish_ptt(self) -> None:
        if self._ptt_active and self._ptt_stop_at is not None and time.monotonic() >= self._ptt_stop_at:
            self._ptt_active = False
            audio = np.concatenate(self._ptt_buf) if self._ptt_buf else np.zeros(0, dtype=np.float32)
            self._ptt_buf = []
            self.emit("speech_ended", {"mode": "push_to_talk", "seconds": round(len(audio) / 16000, 2)})
            if len(audio) / 16000 >= 0.3:
                self._submit(audio)
            else:
                self.emit("rejected", {"reason": "push-to-talk clip too short", "seconds": round(len(audio) / 16000, 2)})

    def _check_stalled(self) -> None:
        """No audio blocks at all for a while: the input device went away (Bluetooth mic off,
        USB unplugged). Say so once instead of silently hearing nothing."""
        if self._last_block_at is None or self.stalled:
            return
        if time.monotonic() - self._last_block_at > MIC_STALL_S:
            self.stalled = True
            self.input_warning = "Microphone stopped delivering audio (unplugged or switched off?). Pick the input again in Settings > Voice."
            self.emit("error", {"error": self.input_warning})

    def _check_silent_input(self, block: np.ndarray) -> None:
        if np.any(block):
            self._zero_since = None
            self.input_warning = None
            return
        now = time.monotonic()
        if self._zero_since is None:
            self._zero_since = now
        elif now - self._zero_since > SILENT_INPUT_S and self.input_warning is None:
            self.input_warning = (
                "Microphone delivers exact silence: on macOS grant Microphone access to the app running the backend."
            )
            self.emit("error", {"error": self.input_warning})

    def _submit(self, audio: np.ndarray, during_playback: bool = False) -> None:
        try:
            self._stt_q.put_nowait((audio, during_playback))
        except queue.Full:
            self.emit("rejected", {"reason": "speech-to-text busy; utterance dropped"})

    def _stt_loop(self) -> None:
        while self.running:
            try:
                audio, during_playback = self._stt_q.get(timeout=0.3)
            except queue.Empty:
                continue
            tr = self.transcriber
            if tr is None or tr.state != "ready":
                self.emit("rejected", {"reason": f"speech-to-text {getattr(tr, 'state', 'unavailable')}: {getattr(tr, 'error', '') or 'still loading'}"})
                continue
            self.transcribing = True
            if self.on_transcribing is not None:
                self.on_transcribing(True)
            try:
                result = tr.transcribe(audio)
            finally:
                self.transcribing = False
                if self.on_transcribing is not None:
                    self.on_transcribing(False)
            if result is None:
                self.emit("rejected", {"reason": "transcription failed or timed out"})
                continue
            text = (result.get("text") or "").strip()
            if is_hallucination(text):
                self.emit("rejected", {"reason": "no speech recognised", "text": text})
                continue
            self.utterances += 1
            self.last_text = text
            self.emit(
                "utterance",
                {
                    "text": text,
                    "confidence": float(result.get("confidence", 0.0)),
                    "latency_ms": result.get("roundtrip_ms", result.get("latency_ms")),
                    "audio_s": result.get("audio_s"),
                    "during_playback": during_playback,
                },
            )
