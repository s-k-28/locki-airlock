"""Write WAV files for the listening test (docs/MILESTONE5_VERIFICATION.md, H3): the same
generated replies in the Kokoro voices that measured closest to the Fish Audio character,
int8 and fp32, next to a few cached Fish Audio clips. Plays nothing; listen with Quick Look
or `afplay` and pick by ear.

    .venv/bin/python scripts/voice_samples.py [--out data/voice_samples]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODELS = ROOT / "backend" / "models"
LINES = [
    "Calculus tomorrow? Then the phone can wait. Forty minutes, go.",
    "That's twice in five minutes. The test is tomorrow, not the group chat.",
    "Fine, it's yours. Come back when the important thing is done.",
]
VARIANTS = [("af_bella", "int8"), ("af_bella", "fp32"), ("af_river", "int8"), ("af_heart", "int8")]


def write(path: Path, audio, sr: int) -> None:
    import numpy as np

    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "data" / "voice_samples"))
    args = ap.parse_args()
    from kokoro_onnx import Kokoro

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    models = {q: Kokoro(str(MODELS / "kokoro" / f"kokoro-v1.0{'.int8' if q == 'int8' else ''}.onnx"), str(MODELS / "kokoro" / "voices-v1.0.bin"))
              for q in ("int8", "fp32")}
    for voice, q in VARIANTS:
        for i, text in enumerate(LINES, 1):
            audio, sr = models[q].create(text, voice=voice)
            write(out / f"kokoro-{voice}-{q}-{i}.wav", audio, sr)
    fish_dir = ROOT / "data" / "tts_cache" / "fish_audio"
    index = json.loads((fish_dir / "index.json").read_text()) if (fish_dir / "index.json").exists() else {}
    picked = 0
    for key, meta in index.items():
        if picked >= 3 or not (fish_dir / f"{key}.wav").exists():
            continue
        shutil.copyfile(fish_dir / f"{key}.wav", out / f"fish-egirl-{picked + 1}.wav")
        (out / f"fish-egirl-{picked + 1}.txt").write_text(meta.get("text", ""))
        picked += 1
    (out / "README.txt").write_text(
        "Same three sentences in each Kokoro variant (kokoro-<voice>-<int8|fp32>-<n>.wav) and three cached Fish Audio clips.\n"
        "Default since Milestone 5: af_bella, int8. Note which one sounds closest to the Fish voice and whether int8 sounds worse than fp32.\n"
        + "\n".join(f"{i}. {t}" for i, t in enumerate(LINES, 1))
        + "\n"
    )
    print(f"wrote {len(VARIANTS) * len(LINES)} Kokoro samples and {picked} Fish clips to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
