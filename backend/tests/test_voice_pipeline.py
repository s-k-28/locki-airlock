"""The microphone-to-reply path through the production voice service and runtime: real Whisper
on a spoken command (when the model is on this machine), the microphone failing to open,
silence, a camera approach turning into a spoken warning, and shutdown while Whisper is busy.
Only the microphone is replaced, by audio handed to the service's speech-to-text queue; the
speaker is the recording TTS (no sound)."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import threading
import time
import wave

import numpy as np
import pytest

from app.intelligence.state_machine import State
from app.runtime import LockedInRuntime, VisionEvent
from app.telemetry.events import EventType
from app.voice import service as voice_service
from app.voice.service import VoiceService
from app.voice.transcription import Transcriber, WhisperTranscriber

from .conftest import make_runtime, wait_for


def spoken(text: str, tmp_path) -> np.ndarray:
    """A macOS voice saying ``text``, 16 kHz mono float32, like the microphone's blocks."""
    wav = tmp_path / "said.wav"
    subprocess.run(["say", "-o", str(wav), "--file-format=WAVE", "--data-format=LEI16@16000", "--", text], check=True, capture_output=True, timeout=30)
    with wave.open(str(wav)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768


def stt_only(svc: VoiceService) -> None:
    """Start the service's speech-to-text worker without opening a microphone."""
    svc.running = True
    svc._stt_thread = threading.Thread(target=svc._stt_loop, name="voice-stt", daemon=True)
    svc._stt_thread.start()


class FakeWhisper(Transcriber):
    engine = "fake"

    def __init__(self, text: str = "", delay_s: float = 0.0) -> None:
        self.state = "ready"
        self.text = text
        self.delay_s = delay_s
        self.started = threading.Event()
        self.closed = False

    def transcribe(self, audio, timeout_s: float = 10.0):
        self.started.set()
        time.sleep(self.delay_s)
        return {"text": self.text, "confidence": 0.9, "latency_ms": 400.0}

    def close(self) -> None:
        self.closed = True


@pytest.mark.skipif(shutil.which("say") is None, reason="needs macOS `say` to speak the command")
async def test_real_whisper_hears_a_spoken_command_and_the_robot_answers(settings, tts, tmp_path):
    if not (settings.models_dir / "whisper" / settings.whisper_model / "model.bin").exists():
        pytest.skip("Whisper model not downloaded (scripts/download_models.py)")
    rt = await make_runtime(settings, tts)
    tr = WhisperTranscriber(settings.whisper_model, settings.whisper_compute_type, settings.models_dir)
    svc = VoiceService(settings, emit=rt._voice_emit, speaker=rt.speaker, transcriber=tr)
    try:
        assert await wait_for(lambda: tr.state != "loading", timeout=60)
        assert tr.state == "ready", tr.error
        stt_only(svc)
        svc._submit(spoken("Lock me in for 25 minutes.", tmp_path))
        assert await wait_for(lambda: rt.voice_turns and rt.sessions.active, timeout=30)
        turn = rt.voice_turns[-1]
        assert "lock me in" in turn["text"].lower() and "25" in turn["text"]
        assert turn["intent"]["intent"] == "start_session" and turn["stt_ms"] > 0
        assert rt.sessions.current.planned_duration_s == 1500
        assert await wait_for(lambda: turn["replies"] and turn["replies"][0]["text"] in tts.spoken, timeout=5)
    finally:
        svc.running = False
        if svc._stt_thread is not None:
            svc._stt_thread.join(timeout=3)
        tr.close()
        await rt.shutdown()


async def test_silence_or_a_whisper_hallucination_is_not_a_command(settings, tts):
    rt = await make_runtime(settings, tts)
    svc = VoiceService(settings, emit=rt._voice_emit, speaker=rt.speaker, transcriber=FakeWhisper("Thank you."))
    try:
        stt_only(svc)
        svc._submit(np.zeros(16000, dtype=np.float32))
        assert await wait_for(lambda: any(e.event_type == EventType.VOICE_COMMAND_REJECTED for e in rt.bus.history()), timeout=3)
        rejected = [e for e in rt.bus.history() if e.event_type == EventType.VOICE_COMMAND_REJECTED]
        assert rejected[-1].payload["reason"] == "no speech recognised"
        assert not rt.voice_turns and tts.spoken == []
    finally:
        svc.running = False
        await rt.shutdown()


async def test_a_microphone_that_will_not_open_is_reported_not_fatal(settings, tts, monkeypatch):
    class BrokenMic:
        def __init__(self, device=None):
            self.device = device

        def start(self):
            raise RuntimeError("Error opening InputStream: Invalid number of channels [PaErrorCode -9998]")

        def stop(self):
            pass

    monkeypatch.setattr(voice_service, "AudioCapture", BrokenMic)
    monkeypatch.setattr(voice_service, "make_transcriber", lambda settings: FakeWhisper())
    rt = LockedInRuntime(settings, persist_settings=False, tts_engine=tts, enable_vision=False, enable_voice=True)
    await rt.start()
    try:
        r = await rt.start_voice()
        assert not r.ok and "microphone failed to start" in r.message and "PaErrorCode" in r.message
        assert [e for e in rt.bus.history() if e.event_type == EventType.VOICE_ERROR]
        assert rt.voice_state() == "off"
        typed = await rt.handle_utterance("lock me in")  # typing still works without a mic
        assert typed.ok and rt.sessions.active
    finally:
        await rt.shutdown()


async def test_shutdown_while_whisper_is_busy_returns_promptly(settings, tts):
    rt = await make_runtime(settings, tts)
    slow = FakeWhisper("lock me in", delay_s=1.5)
    svc = VoiceService(settings, emit=rt._voice_emit, speaker=rt.speaker, transcriber=slow)
    rt.voice = svc
    stt_only(svc)
    svc._submit(np.zeros(16000, dtype=np.float32))
    assert await asyncio.to_thread(slow.started.wait, 3)
    assert svc.transcribing and rt.voice_state() == "transcribing"
    t0 = time.monotonic()
    await asyncio.wait_for(rt.shutdown(), timeout=10)
    assert time.monotonic() - t0 < 5
    assert slow.closed and not svc.running


async def test_a_camera_approach_becomes_a_spoken_warning(rt):
    """Through the runtime's own vision event handler, as the camera thread delivers it."""
    await rt.start_session("essay", duration_min=25, speak=False)
    await rt.handle_vision_event(VisionEvent("approaching", {"track": 1}, confidence=0.7, frame_mono=time.monotonic()))
    assert rt.sm.state == State.POSSIBLE_PHONE_REACH
    warned = [e for e in rt.bus.history() if e.event_type == EventType.VOICE_RESPONSE_GENERATED and e.payload["category"] == "reach_warning"]
    assert len(warned) == 1
    assert not [e for e in rt.bus.history() if e.event_type == EventType.ROBOT_MOTION_REQUESTED]  # words only


async def test_the_same_command_twice_does_one_thing(rt):
    await rt.start_session("essay", duration_min=25, speak=False)
    first = await rt.handle_utterance("pause", source="voice")
    second = await rt.handle_utterance("pause", source="voice")
    assert first.ok and rt.sessions.paused
    assert rt.sessions.paused and rt.sm.state == State.PAUSED
    assert len([e for e in rt.bus.history() if e.event_type == EventType.SESSION_PAUSED]) == 1
    assert second.reply != first.reply or not second.ok
