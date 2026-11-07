"""Voice input: VAD on synthetic audio, push-to-talk and echo suppression with fakes,
plus one real Whisper round trip on macOS-synthesised speech (skipped if unavailable)."""

import shutil
import subprocess
import time
from pathlib import Path

import numpy as np
import pytest

from app.config import Settings
from app.voice import service as svc_mod
from app.voice.input import BLOCK, SAMPLE_RATE, EnergyVAD, rms_db
from app.voice.intents import IntentClassifier, IntentName
from app.voice.stt_worker import confidence_from_segments
from app.voice.transcription import Transcriber, is_hallucination

rng = np.random.default_rng(0)


def noise(seconds: float, db: float) -> np.ndarray:
    n = int(seconds * SAMPLE_RATE)
    amp = 10 ** (db / 20)
    return (rng.standard_normal(n) * amp).astype(np.float32)


def blocks(audio: np.ndarray):
    for i in range(0, len(audio) - BLOCK + 1, BLOCK):
        yield audio[i : i + BLOCK]


def run_vad(audio: np.ndarray):
    vad = EnergyVAD()
    out = []
    for b in blocks(audio):
        out += vad.process(b)
    return out


def test_vad_segments_one_utterance():
    audio = np.concatenate([noise(1.5, -62), noise(0.8, -22), noise(1.2, -62)])
    ev = run_vad(audio)
    kinds = [k for k, _ in ev]
    assert kinds == ["start", "end"]
    seg = ev[1][1]
    assert seg is not None
    assert 0.8 <= len(seg) / SAMPLE_RATE <= 0.8 + 0.3 + 0.7 + 0.1  # speech + preroll + end hangover


def test_vad_never_passes_clicks_or_short_bursts_to_stt():
    for burst in (0.06, 0.2):
        ev = run_vad(np.concatenate([noise(1.0, -62), noise(burst, -20), noise(1.0, -62)]))
        assert all(audio is None for kind, audio in ev if kind == "end"), burst


def test_vad_noise_floor_adapts_to_a_loud_room():
    loud_room = noise(4.0, -40)  # hackathon hall hum
    vad = EnergyVAD()
    starts = sum(1 for b in blocks(loud_room) for k, _ in vad.process(b) if k == "start")
    assert starts == 0
    assert vad.floor_db > -45


def test_rms_db():
    assert rms_db(np.zeros(100, dtype=np.float32)) == -120.0
    assert abs(rms_db(np.full(480, 0.1, dtype=np.float32)) - (-20.0)) < 0.1


def test_hallucination_filter():
    assert is_hallucination("Thank you.") and is_hallucination(" you ") and is_hallucination("")
    assert not is_hallucination("Lock in, 25 minutes")


def test_confidence_from_segments():
    class Seg:
        def __init__(self, start, end, lp, ns):
            self.start, self.end, self.avg_logprob, self.no_speech_prob = start, end, lp, ns

    good = confidence_from_segments([Seg(0, 1, -0.1, 0.01)])
    bad = confidence_from_segments([Seg(0, 1, -0.1, 0.9)])
    assert 0.85 < good < 0.95 and bad < 0.15
    assert confidence_from_segments([]) == 0.0


class FakeCapture:
    def __init__(self, *a, **k):
        import queue

        self.queue = queue.Queue()

    def start(self):
        pass

    def stop(self):
        pass


class FakeTranscriber(Transcriber):
    engine = "fake"
    state = "ready"

    def __init__(self):
        self.calls = []

    def transcribe(self, audio, timeout_s=10.0):
        self.calls.append(len(audio))
        return {"text": "How am I doing?", "confidence": 0.9, "roundtrip_ms": 12.0, "audio_s": len(audio) / 16000}


class Speaker:
    def __init__(self, speaking):
        self.speaking = speaking

    def recently_spoke(self, within_s=0.4):
        return self.speaking


def make_service(monkeypatch, speaking=False, vad_engine="energy"):
    # These tests use loud noise bursts as stand-in speech, which only the loudness detector
    # accepts; the default Silero detector rightly ignores them (tests/test_vad.py feeds it
    # real synthetic speech instead).
    monkeypatch.setattr(svc_mod, "AudioCapture", FakeCapture)
    got = []
    s = svc_mod.VoiceService(Settings(vad_engine=vad_engine), emit=lambda k, d: got.append((k, d)), speaker=Speaker(speaking), transcriber=FakeTranscriber())
    return s, got


def feed(s, audio):
    for b in blocks(audio):
        s.capture.queue.put(b)


def wait_for(pred, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def test_push_to_talk_transcribes_the_held_clip(monkeypatch):
    s, got = make_service(monkeypatch)
    s.start(device=None, mode="push_to_talk")
    try:
        feed(s, noise(0.5, -62))  # before the button: ignored
        assert wait_for(lambda: s.capture.queue.empty())
        time.sleep(0.05)
        s.ptt_start()
        feed(s, noise(1.0, -25))
        time.sleep(0.15)
        s.ptt_stop()
        assert wait_for(lambda: any(k == "utterance" for k, _ in got))
        utt = next(d for k, d in got if k == "utterance")
        assert utt["text"] == "How am I doing?" and utt["latency_ms"] == 12.0
        assert 0.9 < s.transcriber.calls[0] / 16000 < 1.2
    finally:
        s.stop()


def test_hands_free_mode_marks_speech_heard_while_robot_speaks(monkeypatch):
    # Not dropped any more: the runtime filters echo and takes only barge-in commands from it.
    s, got = make_service(monkeypatch, speaking=True)
    s.start(device=None, mode="always")
    try:
        feed(s, np.concatenate([noise(0.8, -62), noise(1.0, -22), noise(1.0, -62)]))
        assert wait_for(lambda: any(k == "utterance" for k, _ in got))
        utt = next(d for k, d in got if k == "utterance")
        assert utt["during_playback"] is True
        assert s.dropped_echo_blocks > 50
    finally:
        s.stop()


def test_hands_free_mode_transcribes_when_quiet(monkeypatch):
    s, got = make_service(monkeypatch, speaking=False)
    s.start(device=None, mode="always")
    try:
        feed(s, np.concatenate([noise(0.8, -62), noise(1.0, -22), noise(1.0, -62)]))
        assert wait_for(lambda: any(k == "utterance" for k, _ in got))
    finally:
        s.stop()


def test_silent_input_warns_about_permission(monkeypatch):
    monkeypatch.setattr(svc_mod, "SILENT_INPUT_S", 0.2)
    s, got = make_service(monkeypatch)
    s.start(device=None, mode="push_to_talk")
    try:
        for _ in range(3):
            feed(s, np.zeros(SAMPLE_RATE // 4, dtype=np.float32))
            time.sleep(0.12)
        assert wait_for(lambda: any(k == "error" for k, _ in got))
        assert "Microphone" in s.snapshot()["warning"]
    finally:
        s.stop()


MODEL = Path(__file__).resolve().parents[1] / "models" / "whisper" / "base.en" / "model.bin"


@pytest.mark.skipif(not (shutil.which("say") and shutil.which("ffmpeg") and MODEL.exists()), reason="needs macOS say, ffmpeg and the local Whisper model")
def test_real_whisper_round_trip_on_synthesised_speech(tmp_path):
    from app.voice.transcription import WhisperTranscriber

    aiff, wav = tmp_path / "cmd.aiff", tmp_path / "cmd.wav"
    subprocess.run(["say", "-v", "Samantha", "-o", str(aiff), "Locked in, give me my phone."], check=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(aiff), "-ar", "16000", "-ac", "1", "-f", "s16le", str(wav)], check=True)
    audio = np.frombuffer(wav.read_bytes(), dtype=np.int16).astype(np.float32) / 32768.0
    tr = WhisperTranscriber("base.en", "int8", MODEL.parents[2])
    try:
        assert wait_for(lambda: tr.state != "loading", timeout=120)
        assert tr.state == "ready", tr.error
        out = tr.transcribe(audio)
        assert out is not None
        intent = IntentClassifier().classify(out["text"], out["confidence"])
        assert intent.name == IntentName.RELEASE, out
    finally:
        tr.close()


@pytest.mark.skipif(not shutil.which("say"), reason="macOS say not available")
def test_tts_cache_prerenders_fixed_lines(tmp_path):
    from app.voice.tts import CachedSystemTTS

    tts = CachedSystemTTS(tmp_path, rate_wpm=190)
    tts.prewarm(["Nice try."])
    assert tts.prewarmed == 1
    data, sr = tts._load("Nice try.")
    assert sr == 22050 and 0.3 < len(data) / sr < 2.0
    assert tts._load("not cached") is None


def test_a_microphone_that_goes_quiet_at_the_device_is_reported_and_recovers(monkeypatch):
    monkeypatch.setattr(svc_mod, "MIC_STALL_S", 0.3)
    s, got = make_service(monkeypatch, speaking=False)
    s.start(device=None, mode="always")
    try:
        feed(s, noise(0.3, -62))
        assert wait_for(lambda: any(k == "error" and "stopped delivering" in d["error"] for k, d in got), timeout=3)
        assert s.snapshot()["stalled"] is True
        feed(s, noise(0.2, -62))
        assert wait_for(lambda: any(k == "recovered" for k, _ in got), timeout=3)
        assert s.snapshot()["stalled"] is False and s.input_warning is None
    finally:
        s.stop()
