"""Download the model files used for vision (MediaPipe), speech (Whisper), the local
language model (Qwen3 on MLX) and the local voice (Kokoro), and check every file against
backend/models.sha256.

Run once while online (the hackathon demo may run on the robot's offline Wi-Fi):

    uv run python scripts/download_models.py            # fetch what is missing, then verify
    uv run python scripts/download_models.py --verify   # offline: only check what is on disk

Files land in backend/models/ (git-ignored). Existing files are kept unless --force.
Every source is pinned (Hugging Face to a commit, MediaPipe to a versioned path, Kokoro to
a release), and backend/models.sha256 holds the sha256 of every file measured in
docs/MODELS.md, in `shasum -a 256 -c` format (run it from backend/models). A single-file
download that does not match is discarded; any mismatch or missing file exits with 1.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.request
from pathlib import Path

MODELS_DIR = Path(__file__).resolve().parents[1] / "models"
MANIFEST = MODELS_DIR.parent / "models.sha256"

# Versioned paths: float16/1 is byte-identical to float16/latest (checked 2026-11-01).
MEDIAPIPE = {
    "hand_landmarker.task": (
        "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task"
    ),
    "blaze_face_short_range.tflite": (
        "https://storage.googleapis.com/mediapipe-models/face_detector/blaze_face_short_range/float16/1/blaze_face_short_range.tflite"
    ),
}

# faster-whisper (the speech-to-text fallback): Systran/faster-whisper-<model>, pinned commits.
FASTER_WHISPER = {"base.en": "3d3d5dee26484f91867d81cb899cfcf72b96be6c"}

# Local language model for open-ended replies (Apple silicon, MLX). Pinned commits.
LLMS = {
    "Qwen3-4B-Instruct-2507-4bit": ("mlx-community/Qwen3-4B-Instruct-2507-4bit", "50d427756c6b1b2fe0c0a10f67fbda1fc8e82c1b"),
    "Qwen3-1.7B-4bit": ("mlx-community/Qwen3-1.7B-4bit", "3b1b1768f8f8cf8351c712464f906e86c2b8269e"),
}
DEFAULT_LLM = "Qwen3-4B-Instruct-2507-4bit"

# Local voice for generated replies (kokoro-onnx release files, Kokoro-82M v1.0 weights).
_KOKORO_RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
KOKORO = {
    "kokoro/kokoro-v1.0.onnx": f"{_KOKORO_RELEASE}/kokoro-v1.0.onnx",
    "kokoro/kokoro-v1.0.int8.onnx": f"{_KOKORO_RELEASE}/kokoro-v1.0.int8.onnx",
    "kokoro/voices-v1.0.bin": f"{_KOKORO_RELEASE}/voices-v1.0.bin",
}

# whisper.cpp weights: the default speech-to-text engine since Milestone 5 (docs/MODELS.md).
WHISPER_CPP = ("ggerganov/whisper.cpp", "5359861c739e955e79d9a303bcbc70fb988958b1")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def expected() -> dict[str, str]:
    """backend/models.sha256 as {path under backend/models: sha256}."""
    pairs = (line.split(maxsplit=1) for line in MANIFEST.read_text().splitlines() if line.strip())
    return {rel.strip(): digest for digest, rel in pairs}


def check(target: str) -> tuple[int, list[str]]:
    """Manifest files at or under `target`: how many match, and what is wrong with the others."""
    ok, problems = 0, []
    for rel, digest in expected().items():
        if rel != target and not rel.startswith(target + "/"):
            continue
        path = MODELS_DIR / rel
        if not path.exists():
            problems.append(f"{rel}: missing")
        elif sha256(path) != digest:
            problems.append(f"{rel}: sha256 differs from models.sha256")
        else:
            ok += 1
    return ok, problems


def download(name: str, url: str, force: bool) -> None:
    dest = MODELS_DIR / name
    if dest.exists() and not force:
        print(f"ok      {name} ({dest.stat().st_size / 1e6:.1f} MB, already present)")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"fetch   {name} <- {url}")
    with urllib.request.urlopen(url, timeout=60) as resp, open(tmp, "wb") as fh:
        while chunk := resp.read(1 << 16):
            fh.write(chunk)
    want = expected().get(name)
    if want and sha256(tmp) != want:
        tmp.unlink()
        raise RuntimeError(f"{name}: sha256 differs from models.sha256, download discarded")
    tmp.replace(dest)
    print(f"saved   {name} ({dest.stat().st_size / 1e6:.1f} MB, sha256 {sha256(dest)})")


def download_whisper(model: str) -> None:
    """Speech-to-text weights from the Hugging Face Hub (Systran/faster-whisper-*)."""
    target = MODELS_DIR / "whisper" / model
    if (target / "model.bin").exists():
        print(f"ok      whisper {model} (already present)")
        return
    from faster_whisper import download_model

    revision = FASTER_WHISPER.get(model)
    print(f"fetch   whisper {model} -> {target} @{revision[:10] if revision else 'latest (not pinned)'}")
    download_model(model, output_dir=str(target), revision=revision)
    print(f"saved   whisper {model}")


def copy_silero_vad() -> None:
    """Silero VAD v6 ships inside faster-whisper; keep a copy with the other models so the
    hands-free detector does not depend on where that package lives."""
    import importlib.util
    import shutil

    dest = MODELS_DIR / "vad" / "silero_vad_v6.onnx"
    if dest.exists():
        print("ok      vad/silero_vad_v6.onnx (already present)")
        return
    spec = importlib.util.find_spec("faster_whisper")
    src = Path(list(spec.submodule_search_locations)[0]) / "assets" / "silero_vad_v6.onnx" if spec and spec.submodule_search_locations else None
    if src is None or not src.exists():
        raise FileNotFoundError("silero_vad_v6.onnx not found in the faster-whisper package (uv sync --extra voice)")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    print(f"saved   vad/silero_vad_v6.onnx ({dest.stat().st_size / 1e6:.1f} MB, sha256 {sha256(dest)}) <- faster-whisper assets")


def download_llm(name: str) -> None:
    """MLX weights + tokenizer from the Hugging Face Hub, pinned to a commit."""
    repo, revision = LLMS[name]
    target = MODELS_DIR / "llm" / name
    if (target / "model.safetensors").exists() and (target / "tokenizer.json").exists():
        print(f"ok      llm {name} (already present)")
        return
    from huggingface_hub import snapshot_download

    print(f"fetch   llm {name} <- {repo}@{revision[:10]}")
    snapshot_download(repo, revision=revision, local_dir=str(target))
    weights = target / "model.safetensors"
    print(f"saved   llm {name} ({weights.stat().st_size / 1e9:.2f} GB, sha256 {sha256(weights)})")


def download_whisper_cpp(model: str) -> None:
    repo, revision = WHISPER_CPP
    name = f"ggml-{model}.bin"
    dest = MODELS_DIR / "whispercpp" / name
    if dest.exists():
        print(f"ok      whisper.cpp {name} (already present)")
        return
    from huggingface_hub import hf_hub_download

    print(f"fetch   whisper.cpp {name} <- {repo}@{revision[:10]}")
    hf_hub_download(repo, name, revision=revision, local_dir=str(dest.parent))
    print(f"saved   whisper.cpp {name} ({dest.stat().st_size / 1e6:.1f} MB, sha256 {sha256(dest)})")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="re-download even if present")
    parser.add_argument("--verify", action="store_true", help="download nothing; check the files on disk against models.sha256")
    parser.add_argument("--whisper", default="base.en", help="Whisper model to fetch ('none' to skip)")
    parser.add_argument("--llm", default=DEFAULT_LLM, help=f"local language model: {', '.join(LLMS)}, 'all' or 'none'")
    parser.add_argument("--no-kokoro", action="store_true", help="skip the local Kokoro voice")
    parser.add_argument("--whisper-cpp", default="base.en", help="whisper.cpp weights (the default speech-to-text engine); 'none' to skip")
    args = parser.parse_args()
    llms = list(LLMS) if args.llm == "all" else [] if args.llm == "none" else [args.llm]
    failed = 0
    if not args.verify:
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        jobs = [(name, lambda n=name, u=url: download(n, u, args.force)) for name, url in MEDIAPIPE.items()]
        if args.whisper != "none":
            jobs.append((f"whisper {args.whisper}", lambda: download_whisper(args.whisper)))
        jobs.append(("vad", copy_silero_vad))
        if not args.no_kokoro:
            jobs += [(name, lambda n=name, u=url: download(n, u, args.force)) for name, url in KOKORO.items()]
        jobs += [(f"llm {name}", lambda n=name: download_llm(n)) for name in llms]
        if args.whisper_cpp != "none":
            jobs.append((f"whisper.cpp {args.whisper_cpp}", lambda: download_whisper_cpp(args.whisper_cpp)))
        for label, job in jobs:
            try:
                job()
            except Exception as exc:  # network down, hash mismatch etc.
                failed += 1
                print(f"FAILED  {label}: {exc}", file=sys.stderr)

    # Everything selected, including files that were already present, against the manifest.
    targets = list(MEDIAPIPE) + ["vad/silero_vad_v6.onnx"]
    targets += [f"whisper/{args.whisper}"] if args.whisper != "none" else []
    targets += [] if args.no_kokoro else list(KOKORO)
    targets += [f"llm/{name}" for name in llms]
    targets += [f"whispercpp/ggml-{args.whisper_cpp}.bin"] if args.whisper_cpp != "none" else []
    checked = 0
    for target in targets:
        ok, problems = check(target)
        checked += ok
        if not ok and not problems:
            print(f"unpinned {target}: not in models.sha256, not verified")
        for p in problems:
            failed += 1
            print(f"FAILED  {p}", file=sys.stderr)
    print(f"{'verified' if not failed else 'checked '} {checked} files against {MANIFEST.name}" + (f", {failed} problem(s)" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
