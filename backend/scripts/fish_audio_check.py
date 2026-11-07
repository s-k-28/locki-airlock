"""Real Fish Audio check, measured. Run only after the user approved the generation request.

    uv run python scripts/fish_audio_check.py                          # free lookups only
    uv run python scripts/fish_audio_check.py --generate --play        # + ONE generation request

Free (no generation): GET /model/{id} (the voice exists, is a trained TTS voice) and
GET /wallet/self/api-credit (credit before and after).
--generate: one POST /v1/tts of --text with --model, streamed so the first audio bytes are
timed separately from the whole clip.
--play: plays the clip on the configured output device and times the playback start.
The clip is transcribed with the local Whisper model to check that every word was spoken
(profanity included). It is saved to data/voice_tests/ (git-ignored) to listen again.
The API key is read from .env and never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import re
import sys
import time
import wave
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import numpy as np  # noqa: E402

from app.config import RuntimeSettingsStore, Settings  # noqa: E402
from app.voice.fish_audio import FishAudioClient, FishAudioError, estimate_usd, voice_summary  # noqa: E402
from app.voice.usage import utf8_bytes  # noqa: E402

DEFAULT_TEXT = "Bro, we literally just started. Get your ass back to work."


def words(s: str) -> list[str]:
    return re.findall(r"[a-z']+", re.sub(r"\[[^\]]*\]", " ", s.lower()))


def transcribe(pcm: np.ndarray, sr: int, models_dir: Path) -> tuple[str | None, float | None]:
    from app.voice.transcription import WhisperTranscriber

    tr = WhisperTranscriber(models_dir=models_dir)
    t_end = time.monotonic() + 120
    while tr.state == "loading" and time.monotonic() < t_end:
        time.sleep(0.1)
    if tr.state != "ready":
        return None, None
    x = pcm.astype(np.float32) / 32768.0
    n16 = int(len(x) * 16000 / sr)
    x16 = np.interp(np.linspace(0, len(x) - 1, n16), np.arange(len(x)), x).astype(np.float32)
    res = tr.transcribe(x16, timeout_s=30)
    tr.close()
    return (res or {}).get("text"), (res or {}).get("roundtrip_ms")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--voice", default=None, help="reference_id (default: the saved voice profile)")
    ap.add_argument("--model", default="s2.1-pro-free")
    ap.add_argument("--text", default=DEFAULT_TEXT)
    ap.add_argument("--generate", action="store_true", help="make ONE real generation request")
    ap.add_argument("--play", action="store_true", help="play the clip on the output device")
    a = ap.parse_args()

    settings = Settings()
    rs = RuntimeSettingsStore(settings.data_dir / "runtime_settings.json").load()
    voice = a.voice or rs.voice_profile.reference_id
    if not settings.fish_audio_api_key:
        print("FISH_AUDIO_API_KEY is not set in .env")
        return 1
    client = FishAudioClient(settings.fish_audio_api_key, base_url=settings.fish_audio_base_url)
    print(f"## Fish Audio check · voice {voice} · model {a.model}\n")

    try:
        m = await client.get_model(voice)
        v = voice_summary(m)
        print(f"Voice (GET /model, free): {v['title']} by {v['author']} · type {v['type']} · state {v['state']} · {v['visibility']} · {', '.join(v['languages'])}")
    except FishAudioError as exc:
        print(f"Voice lookup failed: {exc.message}")
        return 1
    try:
        before = (await client.get_api_credit()).get("credit")
        print(f"API credit before (free): {before}")
    except FishAudioError as exc:
        before = None
        print(f"Credit lookup failed: {exc.message}")
    if not a.generate:
        print("\nNo generation requested (--generate).")
        return 0

    s = rs.voice_profile.fish
    settings_used = {"latency": s.latency, "speed": s.speed, "temperature": s.temperature, "top_p": s.top_p, "normalize": s.normalize}
    timings: dict = {}
    nbytes = utf8_bytes(a.text)
    try:
        pcm, sr = await client.tts(a.text, voice, a.model, settings_used, timeout=60, timings=timings)
    except FishAudioError as exc:
        print(f"\nGeneration FAILED: {exc.message} (kind {exc.kind})")
        return 1
    dur = len(pcm) / sr
    print("\n| Measure | Value |\n|---|---|")
    print(f"| Request | success, HTTP 200, {timings['bytes']} bytes of WAV |")
    print(f"| Text sent | {nbytes} UTF-8 bytes (Fish bills bytes), est. ${estimate_usd(nbytes, a.model)} on {a.model} |")
    print(f"| First audio bytes | {timings['first_byte_ms']} ms after the request |")
    print(f"| Whole clip received (= first playable audio for cached / non-streamed playback) | {timings['total_ms']} ms |")
    print(f"| Audio length | {dur:.2f} s at {sr} Hz (real-time factor {timings['total_ms'] / 1000 / dur:.2f}) |")

    out_dir = settings.data_dir / "voice_tests"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{a.model}.wav"
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())

    if a.play:
        import sounddevice as sd

        device = rs.voice.output_device or None
        t0 = time.perf_counter()
        sd.play(pcm, sr, device=device)
        start_ms = (time.perf_counter() - t0) * 1000
        latency = getattr(sd.get_stream(), "latency", None)
        sd.wait()
        lat = f" + {latency * 1000:.0f} ms device output latency" if latency else ""
        print(f"| Playback start | {start_ms:.1f} ms to start the stream{lat} (device: {device or 'system default'}) |")
        print(f"| Time to first audible audio (non-streamed) | ~{timings['total_ms'] + start_ms + (latency or 0) * 1000:.0f} ms |")

    text, stt_ms = transcribe(pcm, sr, settings.models_dir)
    if text is None:
        print("| Local transcription | unavailable (Whisper model not loaded) |")
    else:
        ratio = difflib.SequenceMatcher(None, words(a.text), words(text)).ratio()
        prof = [w for w in ("ass", "shit", "fuck", "damn", "hell") if w in words(a.text)]
        heard = [w for w in prof if w in words(text) or any(w in t for t in words(text))]
        print(f"| Local Whisper transcript | “{text.strip()}” ({stt_ms} ms) |")
        print(f"| Word match vs the text sent | {ratio:.0%} |")
        if prof:
            print(f"| Profanity spoken | {', '.join(heard) or 'NOT heard'} of {', '.join(prof)} |")

    try:
        after = (await client.get_api_credit()).get("credit")
        print(f"| API credit before → after | {before} → {after} |")
    except FishAudioError as exc:
        print(f"| API credit after | lookup failed: {exc.message} |")
    print(f"\nClip saved: {out.relative_to(settings.data_dir.parent)}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
