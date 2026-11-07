"""Real-device voice checks (docs/AUDIO_TESTS.md A2, A6, and spoken commands). Plays sound out
loud: run it at the laptop, with the robot's speaker connected.

    .venv/bin/python scripts/voice_loop_check.py --list
    .venv/bin/python scripts/voice_loop_check.py --output "JLab GO Party"            # all checks
    .venv/bin/python scripts/voice_loop_check.py --output "JLab GO Party" --only speaker

1. speaker  The robot's cached Fish Audio line plays on the chosen output while the Mac's
            microphone records. Whisper must hear the line in the recording (proof the sound
            physically came out), and cross-correlating the recording with the clip gives the
            acoustic latency from "playback started" to "sound at the mic" (includes Bluetooth).
2. echo     The worst case for self-triggering: the robot saying "Emergency stop. I'm not
            moving." through the speaker. Reports what Whisper hears and whether LOCKED IN's echo
            filter would drop it (score >= 0.6) instead of acting on it.
3. commands Command phrases, spoken by a macOS voice through the chosen output, recorded by the
            real microphone, recognised by the same Whisper worker the backend uses, then
            classified. Synthetic speech: proof of the real mic + recogniser + intent path, not of
            accuracy on a human voice.

Nothing is sent anywhere. Results are printed and written to data/voice_tests/voice-loop-<time>.json.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import numpy as np  # noqa: E402

from app.config import RuntimeSettingsStore, Settings  # noqa: E402

SR = 16000
ROBOT_LINE = "Nice try."
ECHO_LINE = "Emergency stop. I'm not moving."
COMMANDS = [
    ("Lock me in for twenty five minutes", "start_session"),
    ("How am I doing?", "status"),
    ("How much longer?", "time_left"),
    ("Roast me", "set_personality"),
    ("Hold on a sec", "pause"),
    ("Okay I'm back", "resume"),
    ("Stop", "stop_motion"),
    ("Can I have my phone back?", "release"),
    ("Who are you?", "about"),
    ("Be quiet", "mute"),
]


class Recorder:
    """Microphone recording with the time of its first sample, for alignment."""

    def __init__(self, device):
        import sounddevice as sd

        self._chunks: list[np.ndarray] = []
        self.t_first: float | None = None
        self._lock = threading.Lock()

        def cb(indata, frames, time_info, status):  # noqa: ARG001
            now = time.monotonic()
            with self._lock:
                if self.t_first is None:
                    self.t_first = now - frames / SR
                self._chunks.append(indata[:, 0].copy())

        self.stream = sd.InputStream(samplerate=SR, channels=1, dtype="float32", device=device, callback=cb, blocksize=256)

    def __enter__(self):
        self.stream.start()
        return self

    def __exit__(self, *exc):
        self.stream.stop()
        self.stream.close()

    def audio(self) -> np.ndarray:
        with self._lock:
            return np.concatenate(self._chunks) if self._chunks else np.zeros(0, np.float32)


def to_16k(pcm: np.ndarray, sr: int) -> np.ndarray:
    x = pcm.astype(np.float32)
    if x.ndim > 1:
        x = x.mean(axis=1)
    if np.abs(x).max() > 1.5:
        x = x / 32768.0
    if sr == SR:
        return x
    t_out = np.arange(int(len(x) * SR / sr)) / SR
    return np.interp(t_out, np.arange(len(x)) / sr, x).astype(np.float32)


def onset_s(recording: np.ndarray, clip: np.ndarray) -> tuple[float, float]:
    """(seconds into the recording where the clip starts, normalised correlation peak)."""
    n = len(recording) + len(clip)
    nfft = 1 << (n - 1).bit_length()
    r = np.fft.irfft(np.fft.rfft(recording, nfft) * np.conj(np.fft.rfft(clip, nfft)), nfft)[: len(recording)]
    i = int(np.argmax(np.abs(r)))
    norm = float(np.abs(r[i]) / (np.linalg.norm(clip) * np.linalg.norm(recording[i : i + len(clip)]) + 1e-9))
    return i / SR, norm


def db(x: np.ndarray) -> float:
    return round(20 * np.log10(max(float(np.sqrt(np.mean(x**2))) if len(x) else 0.0, 1e-9)), 1)


def robot_clip(text: str):
    """The robot's cached Fish Audio clip for ``text`` in the saved voice (else the local voice)."""
    from app.voice.audio_cache import AudioCache
    from app.voice.fish_audio import FishAudioVoice
    from app.voice.personality import voice_pack
    from app.voice.tts import CachedSystemTTS, SpeechRequest
    from app.voice.usage import UsageLedger

    settings = Settings()
    p = RuntimeSettingsStore(settings.data_dir / "runtime_settings.json").load().voice_profile
    fish = FishAudioVoice(None, AudioCache(settings.data_dir / "tts_cache" / "fish_audio"), UsageLedger(settings.data_dir / "tts_usage.json"))
    cue = next((c for _cat, t, c in voice_pack() if t == text), None)
    clip = fish.cached(fish.text_for(SpeechRequest(text, cue), p), p)
    if clip is not None:
        return clip, "Cached Fish Audio"
    return CachedSystemTTS(settings.data_dir / "tts_cache").render(text), "Local voice"


def spoken(text: str) -> tuple[np.ndarray, int]:
    """A macOS voice saying ``text`` (the stand-in for the user)."""
    with tempfile.TemporaryDirectory() as tmp:
        aiff, wav = Path(tmp) / "s.aiff", Path(tmp) / "s.wav"
        subprocess.run(["say", "-o", str(aiff), "--", text], check=True)
        subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@22050", "-c", "1", str(aiff), str(wav)], check=True)
        with wave.open(str(wav)) as w:
            return np.frombuffer(w.readframes(w.getnframes()), np.int16).copy(), w.getframerate()


def play_and_record(pcm, sr, output, input_dev, gain: float = 1.0):
    import sounddevice as sd

    data = pcm if gain == 1.0 else np.clip(pcm.astype(np.float32) * gain, -32768, 32767).astype(np.int16)
    with Recorder(input_dev) as rec:
        time.sleep(0.4)
        sd.play(data, sr, device=output)
        t_play = time.monotonic()
        lat = getattr(sd.get_stream(), "latency", None)
        sd.wait()
        time.sleep(0.6)
    return rec.audio(), rec.t_first, t_play, lat


def transcriber(settings):
    from app.voice.transcription import WhisperTranscriber

    tr = WhisperTranscriber(settings.whisper_model, settings.whisper_compute_type, settings.models_dir)
    end = time.monotonic() + 120
    while tr.state == "loading" and time.monotonic() < end:
        time.sleep(0.1)
    if tr.state != "ready":
        raise SystemExit(f"Whisper not ready: {tr.error}")
    return tr


def check_speaker(output, input_dev, tr) -> dict:
    (pcm, sr), source = robot_clip(ROBOT_LINE)
    rec, t_first, t_play, lat = play_and_record(pcm, sr, output, input_dev)
    clip16 = to_16k(pcm, sr)
    start_s, peak = onset_s(rec, clip16)
    acoustic_ms = round((t_first + start_s - t_play) * 1000, 1) if t_first is not None else None
    heard = ((tr.transcribe(rec, timeout_s=30) or {}).get("text") or "").strip()
    return {
        "line": ROBOT_LINE, "voice": source, "output": output or "default", "mic_level_db": db(rec),
        "heard": heard, "heard_ok": "nice try" in heard.lower(),
        "device_latency_ms": None if lat is None else round(lat * 1000, 1),
        "acoustic_latency_ms": acoustic_ms if peak >= 0.2 else None, "correlation": round(peak, 2),
    }


SAVE_DIR: Path | None = None  # --save: keep the raw recordings (WAV) for offline comparison


def save(name: str, audio: np.ndarray) -> None:
    if SAVE_DIR is None:
        return
    SAVE_DIR.mkdir(parents=True, exist_ok=True)
    with wave.open(str(SAVE_DIR / f"{name}.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())


def check_echo(output, input_dev, tr) -> dict:
    from app.voice.intents import IntentClassifier
    from app.voice.tts import echo_similarity

    (pcm, sr), source = robot_clip(ECHO_LINE)
    rec, *_ = play_and_record(pcm, sr, output, input_dev)
    save("echo", rec)
    heard = ((tr.transcribe(rec, timeout_s=30) or {}).get("text") or "").strip()
    score = echo_similarity(heard, ECHO_LINE) if heard else 0.0
    intent = IntentClassifier().classify(heard).name.value if heard else None
    return {
        "line": ECHO_LINE, "voice": source, "heard": heard, "mic_level_db": db(rec), "looks_like": intent,
        "echo_score": round(score, 2), "dropped_as_echo": (not heard) or score >= 0.6,
    }


def check_commands(output, input_dev, tr) -> list[dict]:
    from app.voice.intents import IntentClassifier

    clf = IntentClassifier()
    out = []
    for i, (text, expected) in enumerate(COMMANDS):
        pcm, sr = spoken(text)
        rec, *_ = play_and_record(pcm, sr, output, input_dev, gain=0.8)
        save(f"cmd{i:02d}-{expected}", rec)
        res = tr.transcribe(rec, timeout_s=30) or {}
        heard = (res.get("text") or "").strip()
        intent = clf.classify(heard, float(res.get("confidence", 1.0))) if heard else None
        got = intent.name.value if intent else None
        out.append({
            "said": text, "heard": heard, "stt_ms": res.get("roundtrip_ms"), "intent": got, "expected": expected,
            "confidence": None if intent is None else round(intent.confidence, 2), "ok": got == expected, "mic_level_db": db(rec),
        })
        print(f"  {'OK ' if got == expected else 'MISS'} said “{text}” → heard “{heard}” → {got} ({res.get('roundtrip_ms')} ms STT)")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--output", default=None, help="output device name (e.g. 'JLab GO Party'); default = system output")
    ap.add_argument("--input", type=int, default=None, help="input device index; default = system input (keep the Mac's mic)")
    ap.add_argument("--only", choices=["speaker", "echo", "commands"], default=None)
    ap.add_argument("--save", default=None, help="also keep the raw recordings (WAV) in this folder")
    a = ap.parse_args()
    global SAVE_DIR
    SAVE_DIR = Path(a.save) if a.save else None
    import sounddevice as sd

    if a.list:
        for i, d in enumerate(sd.query_devices()):
            print(i, d["name"], "in" if d["max_input_channels"] else "  ", "out" if d["max_output_channels"] else "")
        return 0
    in_name = sd.query_devices(a.input if a.input is not None else sd.default.device[0])["name"]
    if a.output and in_name.lower() == a.output.lower():
        print(f"Refusing: the input is the speaker's own microphone ({in_name}); macOS would switch it to a low-quality call profile.")
        return 2
    settings = Settings()
    tr = transcriber(settings)
    results: dict = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "output": a.output or "default", "input": in_name, "whisper": settings.whisper_model}
    try:
        if a.only in (None, "speaker"):
            r = results["speaker"] = check_speaker(a.output, a.input, tr)
            print(f"A2 speaker: {r['voice']} “{r['line']}” on {r['output']} → mic heard “{r['heard']}” ({'OK' if r['heard_ok'] else 'NOT HEARD'}), "
                  f"acoustic latency {r['acoustic_latency_ms']} ms (device reports {r['device_latency_ms']} ms), mic {r['mic_level_db']} dBFS")
        if a.only in (None, "echo"):
            r = results["echo"] = check_echo(a.output, a.input, tr)
            print(f"A6 echo: heard “{r['heard']}” (looks like {r['looks_like']}), echo score {r['echo_score']} → "
                  f"{'dropped as the robot’s own voice' if r['dropped_as_echo'] else 'WOULD NOT be recognised as echo'}")
        if a.only in (None, "commands"):
            print("Commands through the real microphone (macOS voice as the speaker):")
            rows = results["commands"] = check_commands(a.output, a.input, tr)
            ok = sum(r["ok"] for r in rows)
            stt = sorted(r["stt_ms"] for r in rows if r["stt_ms"] is not None)
            results["commands_summary"] = {"correct": ok, "total": len(rows), "stt_ms_p50": stt[len(stt) // 2] if stt else None, "stt_ms_max": stt[-1] if stt else None}
            print(f"  {ok}/{len(rows)} recognised as intended; STT p50 {results['commands_summary']['stt_ms_p50']} ms, max {results['commands_summary']['stt_ms_max']} ms")
    finally:
        tr.close()
    out_dir = settings.data_dir / "voice_tests"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"voice-loop-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(results, indent=2))
    print(f"Saved {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
