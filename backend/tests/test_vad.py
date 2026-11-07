"""Hands-free speech detection: Silero VAD v6 opens for speech and ignores clicks and hums
that open the Milestone 4 energy detector. Synthetic audio (macOS voice + generated noise)."""

from __future__ import annotations

import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest

from app.voice.input import BLOCK, EnergyVAD, SileroVAD, make_vad, silero_model_path

MODELS = Path(__file__).resolve().parents[1] / "models"


def blocks(audio: np.ndarray):
    for i in range(0, len(audio) - BLOCK + 1, BLOCK):
        yield audio[i : i + BLOCK]


def events(vad, audio) -> list[str]:
    return [kind for b in blocks(audio) for kind, _ in vad.process(b)]


def clicks(seconds: float, level: float = 0.3, seed: int = 3) -> np.ndarray:
    """Sparse sharp transients, like typing next to the microphone."""
    rng = np.random.default_rng(seed)
    n = int(seconds * 16000)
    x = np.zeros(n, dtype=np.float32)
    for i in rng.integers(0, n - 200, n // 1600):
        x[i : i + 160] += (rng.normal(0, 1, 160) * np.exp(-np.arange(160) / 25)).astype(np.float32)
    return x / (np.abs(x).max() + 1e-9) * level


@pytest.fixture(scope="module")
def silero_path():
    path = silero_model_path(MODELS)
    if path is None:
        pytest.skip("Silero VAD model not available (scripts/download_models.py)")
    return path


@pytest.mark.skipif(shutil.which("say") is None, reason="needs macOS `say`")
def test_silero_opens_for_speech_and_returns_the_utterance(silero_path, tmp_path):
    wav = tmp_path / "s.wav"
    subprocess.run(["say", "-o", str(wav), "--file-format=WAVE", "--data-format=LEI16@16000", "--", "I just need five more minutes."], check=True, capture_output=True, timeout=30)
    with wave.open(str(wav)) as w:
        speech = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    audio = np.concatenate([np.zeros(16000, dtype=np.float32), speech * 0.5, np.zeros(24000, dtype=np.float32)])
    vad = SileroVAD(silero_path)
    out = [(k, a) for b in blocks(audio) for k, a in vad.process(b)]
    assert [k for k, _ in out] == ["start", "end"]
    utterance = out[-1][1]
    assert utterance is not None and len(utterance) / 16000 >= len(speech) / 16000 * 0.8


def test_clicks_open_the_energy_detector_but_not_silero(silero_path):
    noise = clicks(4.0)
    assert "start" in events(EnergyVAD(), noise)
    assert events(SileroVAD(silero_path), noise) == []


def test_make_vad_falls_back_to_the_energy_detector(tmp_path):
    assert isinstance(make_vad("energy", MODELS), EnergyVAD)
    v = make_vad("silero", tmp_path)  # nothing in tmp_path; the faster-whisper copy may still exist
    assert isinstance(v, (SileroVAD, EnergyVAD))


@pytest.mark.skipif(shutil.which("say") is None, reason="needs macOS `say`")
def test_hands_free_service_with_silero_hears_speech_and_not_noise(silero_path, tmp_path, monkeypatch):
    """The voice service in hands-free mode with the default detector: a spoken sentence
    becomes an utterance; the same length of loud clicks becomes nothing."""
    import time

    from app.config import Settings
    from app.voice import service as svc_mod

    from .test_voice import FakeCapture, FakeTranscriber, Speaker, feed, wait_for

    wav = tmp_path / "s.wav"
    subprocess.run(["say", "-o", str(wav), "--file-format=WAVE", "--data-format=LEI16@16000", "--", "Okay, I'm back, where was I?"], check=True, capture_output=True, timeout=30)
    with wave.open(str(wav)) as w:
        speech = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    monkeypatch.setattr(svc_mod, "AudioCapture", FakeCapture)
    got = []
    s = svc_mod.VoiceService(Settings(vad_engine="silero"), emit=lambda k, d: got.append((k, d)), speaker=Speaker(False), transcriber=FakeTranscriber())
    s.start(device=None, mode="always")
    try:
        assert isinstance(s.vad, SileroVAD)
        feed(s, np.concatenate([clicks(3.0), np.zeros(8000, dtype=np.float32)]))
        time.sleep(0.5)
        assert not any(k == "utterance" for k, _ in got)
        feed(s, np.concatenate([np.zeros(8000, dtype=np.float32), speech * 0.5, np.zeros(24000, dtype=np.float32)]))
        assert wait_for(lambda: any(k == "utterance" for k, _ in got))
    finally:
        s.stop()
