"""Real speaker / microphone checks for docs/AUDIO_TESTS.md. Run with the backend stopped
(it needs the audio devices). Plays sound: only run it when you are at the laptop.

    uv run python scripts/audio_check.py --list
    uv run python scripts/audio_check.py --mic [--input N] [--seconds 3]
    uv run python scripts/audio_check.py --speaker [--output NAME]        # plays a cached line
    uv run python scripts/audio_check.py --echo [--output NAME] [--input N]

--echo plays a cached voice-pack line on the speaker while recording the microphone, then
transcribes the recording with the local Whisper model and reports whether LOCKED IN's
self-trigger filter would drop it (echo score >= 0.6) and which command it would have looked
like. Nothing is saved; recordings stay in memory.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import numpy as np  # noqa: E402

from app.config import RuntimeSettingsStore, Settings  # noqa: E402

ECHO_LINE = "Emergency stop. I'm not moving."  # the worst case: the robot saying a stop word


def list_devices() -> None:
    import sounddevice as sd

    din, dout = sd.default.device
    for i, d in enumerate(sd.query_devices()):
        kind = ("in " if d["max_input_channels"] else "   ") + ("out" if d["max_output_channels"] else "   ")
        mark = (" <- default input" if i == din else "") + (" <- default output" if i == dout else "")
        print(f"{i:2} {kind}  {d['name']:40} {int(d['default_samplerate'])} Hz{mark}")


def cached_clip(text: str):
    """The cached Fish Audio clip for ``text`` in the saved voice, else the local voice."""
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
        return clip, "cached Fish Audio"
    return CachedSystemTTS(settings.data_dir / "tts_cache").render(text), "local voice (not cached in Fish)"


def mic(device, seconds: float) -> None:
    from app.voice.input import measure_input_level

    print(f"Recording {seconds:.0f} s from {device if device is not None else 'the default input'}: talk normally…")
    r = measure_input_level(device, seconds)
    print(f"RMS {r['rms_db']:.1f} dBFS · peak {r['peak_db']:.1f} dBFS · speech detected: {r['speech_detected']}")
    if r["rms_db"] < -80:
        print("Exact silence: grant Microphone access to this terminal (System Settings > Privacy & Security).")


def speaker(output) -> None:
    import sounddevice as sd

    (pcm, sr), source = cached_clip("Nice try.")
    t0 = time.perf_counter()
    sd.play(pcm, sr, device=output)
    start_ms = (time.perf_counter() - t0) * 1000
    lat = getattr(sd.get_stream(), "latency", 0) or 0
    sd.wait()
    print(f"Played “Nice try.” ({source}) on {output or 'the default output'}: stream start {start_ms:.1f} ms + device latency {lat * 1000:.0f} ms")
    print("Bluetooth adds its own delay on top (often 100-300 ms): film the laptop key press and listen if it matters.")


def echo(output, input_dev) -> None:
    import sounddevice as sd

    from app.voice.intents import IntentClassifier
    from app.voice.transcription import WhisperTranscriber
    from app.voice.tts import echo_similarity

    settings = Settings()
    (pcm, sr), source = cached_clip(ECHO_LINE)
    print(f"Playing “{ECHO_LINE}” ({source}) on {output or 'default output'} and recording {input_dev if input_dev is not None else 'default input'}…")
    rec_s = len(pcm) / sr + 1.0
    rec = sd.rec(int(rec_s * 16000), samplerate=16000, channels=1, dtype="float32", device=input_dev)
    time.sleep(0.2)
    sd.play(pcm, sr, device=output)
    sd.wait()
    level = 20 * np.log10(max(float(np.sqrt(np.mean(rec[:, 0] ** 2))), 1e-9))
    tr = WhisperTranscriber(models_dir=settings.models_dir)
    end = time.monotonic() + 120
    while tr.state == "loading" and time.monotonic() < end:
        time.sleep(0.1)
    heard = ((tr.transcribe(rec[:, 0], timeout_s=30) or {}).get("text") or "").strip()
    tr.close()
    score = echo_similarity(heard, ECHO_LINE) if heard else 0.0
    intent = IntentClassifier().classify(heard).name.value if heard else "-"
    print(f"Mic level during playback: {level:.1f} dBFS")
    print(f"Whisper heard: “{heard}” → looks like command: {intent}")
    if not heard:
        print("Nothing intelligible reached the mic: no self-trigger risk at this volume and placement.")
    elif score >= 0.6:
        print(f"Echo score {score:.2f} ≥ 0.6: LOCKED IN would drop this as its own voice. OK.")
    else:
        print(f"Echo score {score:.2f} < 0.6: the filter would NOT recognise it. Lower the volume, move the speaker away from the mic, or use push-to-talk.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--mic", action="store_true")
    ap.add_argument("--speaker", action="store_true")
    ap.add_argument("--echo", action="store_true")
    ap.add_argument("--input", type=int, default=None, help="input device index (see --list)")
    ap.add_argument("--output", default=None, help="output device name or index")
    ap.add_argument("--seconds", type=float, default=3.0)
    a = ap.parse_args()
    out = int(a.output) if a.output and a.output.isdigit() else a.output
    if a.list or not (a.mic or a.speaker or a.echo):
        list_devices()
    if a.mic:
        mic(a.input, a.seconds)
    if a.speaker:
        speaker(out)
    if a.echo:
        echo(out, a.input)
    return 0


if __name__ == "__main__":
    sys.exit(main())
