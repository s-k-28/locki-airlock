"""Measure the local AI candidates for Milestone 5 on this Mac (docs/MODELS.md).

    .venv/bin/python scripts/bench_local_ai.py llm  [--model NAME] [--reps 5]
    .venv/bin/python scripts/bench_local_ai.py tts  [--reps 5]
    .venv/bin/python scripts/bench_local_ai.py stt
    .venv/bin/python scripts/bench_local_ai.py vad
    .venv/bin/python scripts/bench_local_ai.py contention

Every number is measured here, on this machine, and written with the hardware to
docs/benchmarks/m5-<what>-<time>.json. Nothing is played aloud and nothing leaves the Mac.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
MODELS = BACKEND / "models"
OUT = ROOT / "docs" / "benchmarks"
sys.path.insert(0, str(BACKEND))


def machine() -> dict:
    def sysctl(key: str) -> str:
        try:
            return subprocess.run(["sysctl", "-n", key], capture_output=True, text=True, timeout=5).stdout.strip()
        except Exception:
            return ""

    def low_power() -> str:
        try:
            out = subprocess.run(["pmset", "-g"], capture_output=True, text=True, timeout=5).stdout
            return next((ln.split()[-1] for ln in out.splitlines() if "lowpowermode" in ln), "?")
        except Exception:
            return "?"

    def gpu_busy() -> str:
        try:
            out = subprocess.run(["ioreg", "-r", "-d", "1", "-w", "0", "-c", "IOAccelerator"], capture_output=True, text=True, timeout=5).stdout
            i = out.find('"Device Utilization %"=')
            return out[i + 23 : i + 26].strip('",}') if i >= 0 else "?"
        except Exception:
            return "?"

    swap = sysctl("vm.swapusage")
    return {
        "low_power_mode": low_power(),
        "gpu_busy_before_pct": gpu_busy(),
        "swap": swap,
        "chip": sysctl("machdep.cpu.brand_string"),
        "model": sysctl("hw.model"),
        "memory_gb": round(int(sysctl("hw.memsize") or 0) / 2**30, 1),
        "cpu_cores": sysctl("hw.ncpu"),
        "macos": platform.mac_ver()[0],
        "python": platform.python_version(),
    }


def rss_mb() -> float:
    import psutil

    return round(psutil.Process().memory_info().rss / 2**20, 1)


def pct(values: list[float], q: float) -> float:
    v = sorted(values)
    return round(v[min(len(v) - 1, int(round(q * (len(v) - 1))))], 1)


def summary(values: list[float]) -> dict:
    return {"n": len(values), "p50": pct(values, 0.5), "p90": pct(values, 0.9), "min": round(min(values), 1), "max": round(max(values), 1)}


def save(kind: str, data: dict) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"m5-{kind}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps({"machine": machine(), "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **data}, indent=2))
    print(f"\nsaved {path.relative_to(ROOT)}")
    return path


# ---------------------------------------------------------------------------------- LLM
BENCH_TURNS = [
    "I have a calculus test tomorrow and I keep getting distracted.",
    "I just need five more minutes on my phone, it's important.",
    "Why do you keep doing that?",
    "Can you be quiet for a bit?",
    "I'm almost done with this problem set.",
]


def bench_llm(args) -> dict:
    import mlx.core as mx
    from mlx_lm import load
    from mlx_lm.generate import stream_generate
    from mlx_lm.models.cache import make_prompt_cache, trim_prompt_cache
    from mlx_lm.sample_utils import make_sampler

    from app.intelligence.prompting import bench_prompt_parts

    get_peak = getattr(mx, "get_peak_memory", None) or mx.metal.get_peak_memory
    reset_peak = getattr(mx, "reset_peak_memory", None) or mx.metal.reset_peak_memory
    get_active = getattr(mx, "get_active_memory", None) or mx.metal.get_active_memory

    results = {}
    for name in args.model:
        path = MODELS / "llm" / name
        print(f"\n== {name}")
        rss0 = rss_mb()
        t0 = time.perf_counter()
        model, tok = load(str(path))
        load_s = time.perf_counter() - t0
        mx.eval(model.parameters())
        rss_loaded = rss_mb()
        active_gb = get_active() / 1e9
        print(f"load {load_s:.2f}s  rss {rss0}->{rss_loaded} MB  mlx active {active_gb:.2f} GB")
        sampler = make_sampler(temp=0.7, top_p=0.9)
        static_text, dynamic_for = bench_prompt_parts(tok)
        static_ids = tok.encode(static_text, add_special_tokens=False)

        def run(prompt_ids, cache=None, max_tokens=args.max_tokens, model=model, tok=tok, sampler=sampler):
            t = time.perf_counter()
            first = None
            text = ""
            last = None
            for r in stream_generate(model, tok, prompt_ids, max_tokens=max_tokens, sampler=sampler, prompt_cache=cache):
                if first is None:
                    first = time.perf_counter() - t
                text += r.text
                last = r
            total = time.perf_counter() - t
            return {
                "ttft_ms": round(first * 1000, 1),
                "total_ms": round(total * 1000, 1),
                "prompt_tokens": last.prompt_tokens,
                "prompt_tps": round(last.prompt_tps, 1),
                "gen_tokens": last.generation_tokens,
                "gen_tps": round(last.generation_tps, 1),
                "text": text.strip(),
            }

        reset_peak()
        run(static_ids + tok.encode(dynamic_for(BENCH_TURNS[0]), add_special_tokens=False), max_tokens=8)  # warm the kernels
        cold, warm = [], []
        for _rep in range(args.reps):
            for utt in BENCH_TURNS:
                dyn = tok.encode(dynamic_for(utt), add_special_tokens=False)
                cold.append(run(static_ids + dyn))
        # Prefix cache: the persona is computed once and kept; each turn only adds its own tokens.
        cache = make_prompt_cache(model)
        t = time.perf_counter()
        for _ in stream_generate(model, tok, static_ids, max_tokens=1, sampler=sampler, prompt_cache=cache):
            pass
        prefix_ms = (time.perf_counter() - t) * 1000
        base = cache[0].offset - 1  # the persona, without the one generated token
        trim_prompt_cache(cache, cache[0].offset - base)
        for _rep in range(args.reps):
            for utt in BENCH_TURNS:
                dyn = tok.encode(dynamic_for(utt), add_special_tokens=False)
                r = run(dyn, cache=cache)
                trim_prompt_cache(cache, cache[0].offset - base)
                warm.append(r)
        peak_gb = get_peak() / 1e9
        res = {
            "path": str(path.relative_to(ROOT)),
            "load_s": round(load_s, 2),
            "rss_after_load_mb": rss_loaded,
            "mlx_active_after_load_gb": round(active_gb, 2),
            "mlx_peak_gb": round(peak_gb, 2),
            "rss_end_mb": rss_mb(),
            "static_prompt_tokens": len(static_ids),
            "prefix_prefill_ms": round(prefix_ms, 1),
            "cold": {
                "ttft_ms": summary([r["ttft_ms"] for r in cold]),
                "total_ms": summary([r["total_ms"] for r in cold]),
                "prompt_tokens": summary([r["prompt_tokens"] for r in cold]),
                "prompt_tps": summary([r["prompt_tps"] for r in cold]),
                "gen_tps": summary([r["gen_tps"] for r in cold]),
                "gen_tokens": summary([r["gen_tokens"] for r in cold]),
            },
            "prefix_cached": {
                "ttft_ms": summary([r["ttft_ms"] for r in warm]),
                "total_ms": summary([r["total_ms"] for r in warm]),
                "prompt_tokens": summary([r["prompt_tokens"] for r in warm]),
                "gen_tps": summary([r["gen_tps"] for r in warm]),
            },
            "samples": [{"user": BENCH_TURNS[i % len(BENCH_TURNS)], "output": r["text"]} for i, r in enumerate(warm[: len(BENCH_TURNS) * 2])],
        }
        print(json.dumps({k: v for k, v in res.items() if k != "samples"}, indent=1))
        for s in res["samples"][: len(BENCH_TURNS)]:
            print(f"  USER: {s['user']}\n  OUT:  {s['output']}")
        results[name] = res
        model = tok = cache = None
        mx.clear_cache() if hasattr(mx, "clear_cache") else mx.metal.clear_cache()
    return {"llm": results, "reps": args.reps, "max_tokens": args.max_tokens}


# ---------------------------------------------------------------------------------- TTS
TTS_LINES = [
    "Calculus tomorrow? Then the phone can wait. Forty minutes, go.",
    "That's twice in five minutes. Hands off.",
    "Fine, it's yours. Come back when the important thing is done.",
    "Going quiet for ten minutes. I'm still watching the phone though.",
    "You said almost done ten minutes ago. Prove it.",
    "Because your hand keeps drifting toward the phone, and you told me you have a test tomorrow.",
]
KOKORO_VOICES = ["af_heart", "af_bella", "af_nicole", "af_sky", "af_jessica", "af_sarah", "af_nova", "af_river", "af_kore", "af_aoede", "af_alloy"]


def yin_f0(x, sr: int, fmin: float = 80.0, fmax: float = 500.0, frame: int = 1024, hop: int = 256):
    """Fundamental frequency per voiced frame (YIN, de Cheveigne & Kawahara 2002), Hz."""
    import numpy as np

    x = x.astype(np.float64)
    tau_min, tau_max = int(sr / fmax), int(sr / fmin)
    out = []
    for start in range(0, len(x) - frame - tau_max, hop):
        seg = x[start : start + frame + tau_max]
        if np.sqrt(np.mean(seg[:frame] ** 2)) < 0.01:
            continue
        d = np.array([np.sum((seg[:frame] - seg[t : t + frame]) ** 2) for t in range(tau_max + 1)])
        cmnd = d.copy()
        cmnd[0] = 1.0
        cmnd[1:] = d[1:] * np.arange(1, tau_max + 1) / np.maximum(np.cumsum(d[1:]), 1e-12)
        cand = np.where(cmnd[tau_min:] < 0.15)[0]
        if cand.size:
            t = cand[0] + tau_min
            while t + 1 <= tau_max and cmnd[t + 1] < cmnd[t]:
                t += 1
            out.append(sr / t)
    return np.array(out)


def voice_profile(clips) -> dict:
    """clips: [(audio float32, sr, text)] -> median pitch, pitch spread, words per second."""
    import re

    import numpy as np

    f0s, words, secs = [], 0, 0.0
    for audio, sr, text in clips:
        f = yin_f0(audio, sr)
        if f.size:
            f0s.append(f)
        words += len(re.findall(r"[A-Za-z']+", re.sub(r"\[[^\]]*\]", " ", text)))
        secs += len(audio) / sr
    f0 = np.concatenate(f0s) if f0s else np.array([0.0])
    q1, q3 = np.percentile(f0, [25, 75])
    return {"f0_median_hz": round(float(np.median(f0)), 1), "f0_iqr_hz": round(float(q3 - q1), 1), "words_per_s": round(words / secs, 2) if secs else None}


def load_wav(path: Path):
    import numpy as np

    with wave.open(str(path)) as w:
        sr, n, ch, width = w.getframerate(), w.getnframes(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(n)
    a = np.frombuffer(raw, dtype=np.int16 if width == 2 else np.int32).astype(np.float32)
    a /= 32768.0 if width == 2 else 2147483648.0
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    return a, sr


def bench_tts(args) -> dict:
    import numpy as np
    import onnxruntime as ort
    from kokoro_onnx import Kokoro

    kdir = MODELS / "kokoro"
    voices = str(kdir / "voices-v1.0.bin")
    res: dict = {"engines": {}}
    for variant, providers in [("kokoro-v1.0.onnx", ["CPUExecutionProvider"]), ("kokoro-v1.0.int8.onnx", ["CPUExecutionProvider"]), ("kokoro-v1.0.onnx", ["CoreMLExecutionProvider", "CPUExecutionProvider"])]:
        label = f"{variant} [{providers[0].replace('ExecutionProvider', '')}]"
        print(f"\n== {label}")
        rss0 = rss_mb()
        t0 = time.perf_counter()
        try:
            sess = ort.InferenceSession(str(kdir / variant), providers=providers)
            k = Kokoro.from_session(sess, voices)
            k.create("Warm up.", voice="af_heart")
        except Exception as exc:
            print(f"  failed: {exc}")
            res["engines"][label] = {"error": str(exc)[:300]}
            continue
        load_s = time.perf_counter() - t0
        rows = []
        for _ in range(args.reps):
            for text in TTS_LINES:
                t = time.perf_counter()
                audio, sr = k.create(text, voice="af_heart")
                ms = (time.perf_counter() - t) * 1000
                rows.append({"text": text, "synth_ms": round(ms, 1), "audio_s": round(len(audio) / sr, 2), "rtf": round(ms / 1000 / (len(audio) / sr), 3)})
        first = []
        for _ in range(args.reps):
            for text in TTS_LINES:
                sentence = text.split(". ")[0].rstrip(".") + "."
                t = time.perf_counter()
                k.create(sentence, voice="af_heart")
                first.append((time.perf_counter() - t) * 1000)
        res["engines"][label] = {
            "load_s": round(load_s, 2),
            "rss_delta_mb": round(rss_mb() - rss0, 1),
            "synth_ms": summary([r["synth_ms"] for r in rows]),
            "first_sentence_ms": summary(first),
            "rtf": summary([r["rtf"] for r in rows]),
            "audio_s": summary([r["audio_s"] for r in rows]),
        }
        print(json.dumps(res["engines"][label], indent=1))
        if variant == "kokoro-v1.0.onnx" and providers[0].startswith("CPU"):
            k_fp32 = k
    # Which Kokoro voice is closest to the Fish Audio character clips (pitch and pace)?
    idx = json.loads((ROOT / "data" / "tts_cache" / "fish_audio" / "index.json").read_text())
    fish = []
    for key, meta in idx.items():
        path = ROOT / "data" / "tts_cache" / "fish_audio" / f"{key}.wav"
        if path.exists():
            a, sr = load_wav(path)
            fish.append((a, sr, meta.get("text", "")))
    res["fish_reference"] = {"clips": len(fish), **voice_profile(fish)}
    print("\nFish Audio clips:", res["fish_reference"])
    cands = {}
    for v in KOKORO_VOICES:
        try:
            clips = [(*k_fp32.create(t, voice=v), t) for t in TTS_LINES[:4]]
        except Exception as exc:
            cands[v] = {"error": str(exc)[:120]}
            continue
        prof = voice_profile([(a, sr, t) for a, sr, t in clips])
        ref = res["fish_reference"]
        prof["distance"] = round(
            abs(np.log(prof["f0_median_hz"] / ref["f0_median_hz"])) / 0.1
            + abs(prof["f0_iqr_hz"] - ref["f0_iqr_hz"]) / max(ref["f0_iqr_hz"], 1) + abs(prof["words_per_s"] - ref["words_per_s"]) / ref["words_per_s"],
            3,
        )
        cands[v] = prof
        print(f"  {v:10s} {prof}")
    res["kokoro_voices"] = dict(sorted(cands.items(), key=lambda kv: kv[1].get("distance", 99)))
    return res


def log_mel(audio, sr: int = 24000, n_fft: int = 1024, hop: int = 256, n_mels: int = 64):
    import numpy as np

    win = np.hanning(n_fft)
    frames = [audio[i : i + n_fft] * win for i in range(0, len(audio) - n_fft, hop)]
    spec = np.abs(np.fft.rfft(np.array(frames), axis=1)) ** 2
    freqs = np.fft.rfftfreq(n_fft, 1 / sr)
    mel = 2595 * np.log10(1 + freqs / 700)
    edges = np.linspace(mel[1], mel[-1], n_mels + 2)
    fb = np.zeros((n_mels, len(freqs)))
    for m in range(n_mels):
        lo, mid, hi = edges[m], edges[m + 1], edges[m + 2]
        fb[m] = np.clip(np.minimum((mel - lo) / (mid - lo), (hi - mel) / (hi - mid)), 0, None)
    return np.log10(spec @ fb.T + 1e-8)


def mel_distance(a, b) -> float:
    """Mean absolute log-mel difference after aligning lengths (dB-like units)."""
    import numpy as np

    ma, mb = log_mel(a), log_mel(b)
    n = min(len(ma), len(mb))
    return float(np.mean(np.abs(ma[:n] - mb[:n])) * 10)


def bench_tts_quant(args) -> dict:
    """How far the int8 model drifts from fp32 for the chosen voice, on the scale of how far
    two different fp32 voices are apart (a proxy for audible difference; no listening test)."""
    from kokoro_onnx import Kokoro

    kdir = MODELS / "kokoro"
    voices = str(kdir / "voices-v1.0.bin")
    fp32 = Kokoro(str(kdir / "kokoro-v1.0.onnx"), voices)
    int8 = Kokoro(str(kdir / "kokoro-v1.0.int8.onnx"), voices)
    import numpy as np

    rows = []
    for text in TTS_LINES:
        a, _ = fp32.create(text, voice=args.voice)
        b, _ = int8.create(text, voice=args.voice)
        c, _ = fp32.create(text, voice=args.other)
        a2, _ = fp32.create(text, voice=args.voice)  # same model twice: the run-to-run floor
        env = lambda x: log_mel(x).mean(axis=0)  # noqa: E731 - average spectral envelope: timing-insensitive
        f0 = lambda x: float(np.median(yin_f0(x, 24000)))  # noqa: E731
        rows.append({"text": text, "int8_vs_fp32": round(mel_distance(a, b), 2), "other_voice_vs_fp32": round(mel_distance(a, c), 2),
                     "fp32_repeat": round(mel_distance(a, a2), 2), "len_ratio": round(len(b) / len(a), 3),
                     "envelope_int8": round(float(np.mean(np.abs(env(a) - env(b))) * 10), 2),
                     "envelope_other_voice": round(float(np.mean(np.abs(env(a) - env(c))) * 10), 2),
                     "f0_fp32": round(f0(a), 1), "f0_int8": round(f0(b), 1), "f0_other": round(f0(c), 1)})
        print(rows[-1])
    keys = ("int8_vs_fp32", "other_voice_vs_fp32", "fp32_repeat", "len_ratio", "envelope_int8", "envelope_other_voice", "f0_fp32", "f0_int8", "f0_other")
    out = {k: round(float(np.mean([r[k] for r in rows])), 3) for k in keys}
    print("mean", out)
    return {"voice": args.voice, "other_voice": args.other, "mean": out, "rows": rows}


# ---------------------------------------------------------------------------------- STT
STT_PHRASES = [
    "Roast me.", "Start my focus session.", "How much time do I have left?", "Pause my session.", "Resume.",
    "Stop talking.", "You can talk again.", "Stop.", "Give me my phone back.", "Lock me in for 25 minutes.", "Emergency stop.",
    "I have a calculus test tomorrow.", "I just need five more minutes.", "I'm almost done.", "This is actually so annoying.",
    "Why do you keep doing that?", "I need my phone for something important.", "Can you be quiet for a bit?", "I'm done with this.",
    "Okay I'm back, where was I?", "Can you give me like forty minutes on my essay?", "Actually make it thirty.",
    "Did I reach for it again?", "How long have I been working?", "I'm going to get some water.",
    "Wait, what did I say I was working on?", "Be nicer, I'm stressed.", "Who's winning, me or the phone?",
    "Tell me something to keep me going.", "I finished the first two problems.", "Let's wrap this up.",
]
STT_VOICES = ["Samantha", "Reed (English (US))", "Daniel"]


def norm_words(text: str) -> list[str]:
    import re

    from app.voice.intents import words_to_numbers

    t = re.sub(r"[^a-z0-9' ]+", " ", text.lower().replace("-", " "))
    return words_to_numbers(" ".join(t.split())).split()


def wer(ref: str, hyp: str) -> float:
    r, h = norm_words(ref), norm_words(hyp)
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = d[j]
            d[j] = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev = cur
    return d[len(h)] / max(1, len(r))


def say_16k(text: str, voice: str, tmp: Path):
    path = tmp / "u.wav"
    subprocess.run(["say", "-v", voice, "-o", str(path), "--file-format=WAVE", "--data-format=LEI16@16000", "--", text], check=True, capture_output=True, timeout=30)
    return load_wav(path)[0]


def stt_test_set(tmp: Path):
    """(label, reference text, 16 kHz float32 audio). Synthetic macOS voices plus the real-mic
    recording from the Milestone 4 demo check (Mac speakers -> MacBook mic, 11 commands)."""
    items = [(f"say:{v}", t, say_16k(t, v, tmp)) for v in STT_VOICES for t in STT_PHRASES]
    rec = ROOT / "data" / "voice_tests" / "demo-check-20261101-054250.wav"
    meta = ROOT / "data" / "voice_tests" / "demo-check-20261101-054250.json"
    if rec.exists() and meta.exists():
        audio, sr = load_wav(rec)
        if sr != 16000:
            import numpy as np

            audio = np.interp(np.arange(0, len(audio), sr / 16000), np.arange(len(audio)), audio).astype("float32")
        said = [s["said"] for s in json.loads(meta.read_text())["steps"]]
        segs = silero_segments(audio, threshold=0.35)  # 0.5 misses the 0.3 s "Stop."
        if len(segs) == len(said):
            items += [("mic:demo-check", t, audio[a:b]) for t, (a, b) in zip(said, segs, strict=True)]
        else:
            print(f"  (real-mic recording: {len(segs)} speech segments for {len(said)} commands; not used)")
    return items


def silero_model():
    import faster_whisper
    import onnxruntime as ort

    path = Path(faster_whisper.__file__).parent / "assets" / "silero_vad_v6.onnx"
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = 1
    opts.inter_op_num_threads = 1
    return ort.InferenceSession(str(path), sess_options=opts, providers=["CPUExecutionProvider"]), path


def silero_probs(audio, sess=None):
    """Speech probability per 32 ms frame (512 samples at 16 kHz), streamed one frame at a time
    the way the microphone would feed it. faster-whisper's bundled v6 graph takes 64 samples of
    context + 512 new samples and an LSTM state (h, c) that is carried from frame to frame."""
    import numpy as np

    sess = sess or silero_model()[0]
    h = np.zeros((1, 1, 128), dtype=np.float32)
    c = np.zeros((1, 1, 128), dtype=np.float32)
    ctx = np.zeros(64, dtype=np.float32)
    out = []
    for i in range(0, len(audio) - 511, 512):
        frame = np.concatenate([ctx, audio[i : i + 512]]).astype(np.float32)[None, :]
        prob, h, c = sess.run(None, {"input": frame, "h": h, "c": c})
        ctx = audio[i + 448 : i + 512]
        out.append(float(np.asarray(prob).reshape(-1)[0]))
    return out


def silero_segments(audio, threshold: float = 0.5, min_silence_ms: int = 600, pad_ms: int = 200):
    probs = silero_probs(audio)
    segs, start, quiet = [], None, 0
    for i, p in enumerate(probs):
        if p >= threshold:
            if start is None:
                start = i
            quiet = 0
        elif start is not None:
            quiet += 1
            if quiet * 32 >= min_silence_ms:
                segs.append((start, i - quiet + 1))
                start, quiet = None, 0
    if start is not None:
        segs.append((start, len(probs)))
    pad = pad_ms * 16
    return [(max(0, a * 512 - pad), min(len(audio), b * 512 + pad)) for a, b in segs if (b - a) * 32 >= 250]


def bench_stt(args) -> dict:
    import tempfile

    import numpy as np

    from app.voice.stt_worker import INITIAL_PROMPT

    with tempfile.TemporaryDirectory() as td:
        items = stt_test_set(Path(td))
    print(f"{len(items)} utterances ({sum(1 for i in items if i[0].startswith('mic'))} real-mic)")
    res: dict = {"utterances": len(items), "engines": {}}

    def run_engine(label, fn, warm):
        fn(warm)
        rows = []
        for src, ref, audio in items:
            t = time.perf_counter()
            hyp = fn(audio)
            ms = (time.perf_counter() - t) * 1000
            rows.append({"src": src, "ref": ref, "hyp": hyp, "ms": round(ms, 1), "wer": round(wer(ref, hyp), 3), "audio_s": round(len(audio) / 16000, 2)})
        out = {"ms": summary([r["ms"] for r in rows]), "wer_mean": round(float(np.mean([r["wer"] for r in rows])), 3),
               "exact": sum(1 for r in rows if r["wer"] == 0) / len(rows), "rows": rows}
        for src in sorted({r["src"] for r in rows}):
            sub = [r for r in rows if r["src"] == src]
            out[f"wer_{src}"] = round(float(np.mean([r["wer"] for r in sub])), 3)
        print(f"  {label}: p50 {out['ms']['p50']} ms p90 {out['ms']['p90']} ms, WER {out['wer_mean']}, exact {out['exact']:.0%}")
        res["engines"][label] = out

    warm = np.zeros(16000, dtype=np.float32)
    from faster_whisper import WhisperModel

    fw = WhisperModel(str(MODELS / "whisper" / "base.en"), device="cpu", compute_type="int8", cpu_threads=4)

    def fw_run(audio):
        segs, _ = fw.transcribe(audio, language="en", beam_size=1, vad_filter=False, condition_on_previous_text=False, initial_prompt=INITIAL_PROMPT, without_timestamps=True)
        return " ".join(s.text.strip() for s in segs).strip()

    run_engine("faster-whisper base.en int8 (current)", fw_run, warm)
    fw = None  # noqa: F841 (frees the model before whisper.cpp loads)
    from pywhispercpp.model import Model

    wc = Model(str(MODELS / "whispercpp" / "ggml-base.en.bin"), redirect_whispercpp_logs_to=None, n_threads=4, print_progress=False, print_realtime=False)

    def wc_run(audio, audio_ctx=0):
        segs = wc.transcribe(audio.astype(np.float32), language="en", no_context=True, single_segment=True, initial_prompt=INITIAL_PROMPT, audio_ctx=audio_ctx)
        return " ".join(s.text.strip() for s in segs).strip()

    run_engine("whisper.cpp base.en (pywhispercpp, Metal GPU)", wc_run, warm)
    wc = Model(str(MODELS / "whispercpp" / "ggml-base.en.bin"), redirect_whispercpp_logs_to=None, n_threads=4, print_progress=False,
               print_realtime=False, context_params={"use_gpu": False})
    run_engine("whisper.cpp base.en (CPU only)", wc_run, warm)
    # audio_ctx: encode only as many 20 ms frames as the clip needs (+ margin) instead of 30 s.
    run_engine("whisper.cpp base.en audio_ctx=fit", lambda a: wc_run(a, audio_ctx=min(1500, int(len(a) / 16000 * 50) + 64)), warm)
    return res


# ---------------------------------------------------------------------------------- VAD
def bench_vad(args) -> dict:
    """Silero VAD v6 vs the current energy VAD: speech found in synthetic speech + noise mixtures,
    false starts on noise alone, CPU per frame. Synthetic audio only (labelled)."""
    import tempfile

    import numpy as np

    from app.voice.input import BLOCK, EnergyVAD

    rng = np.random.default_rng(7)
    with tempfile.TemporaryDirectory() as td:
        speech = [say_16k(t, v, Path(td)) for t in STT_PHRASES[11:21] for v in STT_VOICES[:2]]

    def noise(kind: str, n: int):
        if kind == "white":
            return rng.normal(0, 1, n).astype(np.float32)
        if kind == "keyboard":  # sparse clicks
            x = np.zeros(n, dtype=np.float32)
            for i in rng.integers(0, n - 200, n // 1600):
                x[i : i + 160] += rng.normal(0, 1, 160).astype(np.float32) * np.exp(-np.arange(160) / 25)
            return x
        if kind == "hum":  # fan / mains hum with harmonics
            t = np.arange(n) / 16000
            return (np.sin(2 * np.pi * 120 * t) + 0.5 * np.sin(2 * np.pi * 240 * t) + 0.3 * rng.normal(0, 1, n)).astype(np.float32)
        raise ValueError(kind)

    def at_level(x, db):
        rms = np.sqrt(np.mean(x**2)) + 1e-9
        return x * (10 ** (db / 20) / rms)

    def energy_detects(audio) -> bool:
        vad = EnergyVAD()
        for i in range(0, len(audio) - BLOCK + 1, BLOCK):
            if any(k == "start" for k, _ in vad.process(audio[i : i + BLOCK])):
                return True
        return False

    sess, path = silero_model()

    def silero_detects(audio) -> bool:
        probs = silero_probs(audio, sess)
        run = 0
        for p in probs:
            run = run + 1 if p >= 0.5 else 0
            if run >= 3:  # ~96 ms of speech, like the energy VAD's 90 ms
                return True
        return False

    res: dict = {"silero_model": str(path.name), "cases": {}}
    lead = np.zeros(16000, dtype=np.float32)
    for kind in ("white", "keyboard", "hum"):
        for noise_db in (-50, -35):
            hits_e = hits_s = 0
            for sp in speech:
                mix = np.concatenate([lead, at_level(sp, -26)])
                mix = mix + at_level(noise(kind, len(mix)), noise_db)
                hits_e += energy_detects(mix)
                hits_s += silero_detects(mix)
            only = [at_level(noise(kind, 16000 * 4), noise_db) for _ in range(len(speech))]
            fa_e = sum(energy_detects(x) for x in only)
            fa_s = sum(silero_detects(x) for x in only)
            key = f"{kind}@{noise_db}dBFS"
            res["cases"][key] = {"speech_found_energy": f"{hits_e}/{len(speech)}", "speech_found_silero": f"{hits_s}/{len(speech)}",
                                 "false_start_energy": f"{fa_e}/{len(only)}", "false_start_silero": f"{fa_s}/{len(only)}"}
            print(f"  {key:22s} {res['cases'][key]}")
    x = at_level(noise("white", 16000 * 10), -40)
    t = time.perf_counter()
    silero_probs(x, sess)
    res["silero_ms_per_32ms_frame"] = round((time.perf_counter() - t) * 1000 / (len(x) // 512), 3)
    t = time.perf_counter()
    energy_detects(x)
    res["energy_ms_per_30ms_block"] = round((time.perf_counter() - t) * 1000 / (len(x) // BLOCK), 3)
    print(f"  cost: silero {res['silero_ms_per_32ms_frame']} ms/frame, energy {res['energy_ms_per_30ms_block']} ms/block")
    return res


# ---------------------------------------------------------------------------------- contention
def bench_contention(args) -> dict:
    """Everything on one Mac at once: the language model writing replies (GPU, own process),
    the ESP32-camera vision work (JPEG decode + hand landmarks + face, CPU thread at ~15 fps on
    synthetic test-card frames), whisper.cpp (GPU or CPU) and Kokoro (CPU). Each is measured
    alone, then with the others running."""
    import asyncio
    import tempfile
    import threading

    import cv2
    import numpy as np
    from kokoro_onnx import Kokoro
    from pywhispercpp.model import Model

    sys.path.insert(0, str(BACKEND / "tests"))
    from fake_mjpeg import test_card

    from app.intelligence.local_llm import LocalLLM
    from app.intelligence.prompting import SAMPLE_CONTEXT
    from app.vision.hands import FaceObserver, HandTracker

    with tempfile.TemporaryDirectory() as td:
        utts = [say_16k(t, "Samantha", Path(td)) for t in STT_PHRASES[11:26]]
    jpegs = [test_card(i) for i in range(30)]
    tracker, face = HandTracker(MODELS), FaceObserver(MODELS)
    stt = {mode: Model(str(MODELS / "whispercpp" / "ggml-base.en.bin"), redirect_whispercpp_logs_to=None, n_threads=4, print_progress=False,
                       print_realtime=False, context_params={"use_gpu": mode == "gpu"}) for mode in ("gpu", "cpu")}
    tts = Kokoro(str(MODELS / "kokoro" / "kokoro-v1.0.int8.onnx"), str(MODELS / "kokoro" / "voices-v1.0.bin"))
    for m in stt.values():
        m.transcribe(np.zeros(16000, dtype=np.float32), language="en")
    tts.create("Warm up.", voice="af_bella")

    clock = {"n": 0}  # MediaPipe's VIDEO mode needs timestamps that only ever increase

    def vision_frame(i: int) -> float:
        clock["n"] += 1
        ts = 1000 + clock["n"] * 66
        t = time.perf_counter()
        frame = cv2.imdecode(np.frombuffer(jpegs[i % len(jpegs)], np.uint8), cv2.IMREAD_COLOR)
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        tracker.detect(rgb, ts)
        if i % 3 == 0:
            face.observe(rgb, ts)
        return (time.perf_counter() - t) * 1000

    def stt_once(mode: str, audio) -> float:
        t = time.perf_counter()
        stt[mode].transcribe(audio.astype(np.float32), language="en", no_context=True, single_segment=True)
        return (time.perf_counter() - t) * 1000

    def tts_once(text: str) -> float:
        t = time.perf_counter()
        tts.create(text, voice="af_bella")
        return (time.perf_counter() - t) * 1000

    def measure_side(tag: str) -> dict:
        return {
            "stt_gpu_ms": summary([stt_once("gpu", a) for a in utts]),
            "stt_cpu_ms": summary([stt_once("cpu", a) for a in utts]),
            "kokoro_ms": summary([tts_once(t) for t in TTS_LINES]),
        }

    async def run() -> dict:
        llm = LocalLLM(args.model, MODELS)
        while llm.state == "loading":
            await asyncio.sleep(0.2)
        assert llm.state == "ready", llm.error
        ctx = SAMPLE_CONTEXT.replace("{utterance}", "Why do you keep doing that?")

        async def llm_runs(n: int) -> list:
            out = []
            for _ in range(n):
                g = await llm.generate(ctx, max_tokens=60, timeout_s=30)
                out.append(g)
            return out

        res: dict = {"llm_model": args.model}
        # 1. alone
        res["vision_alone_ms"] = summary([vision_frame(i) for i in range(120)])
        res["alone"] = await asyncio.to_thread(measure_side, "alone")
        g = await llm_runs(5)
        res["llm_alone"] = {"total_ms": summary([x.total_ms for x in g]), "gen_tps": summary([x.gen_tps or 0 for x in g])}
        # 2. together: the model writing non-stop and the camera pipeline at ~15 fps
        stop = threading.Event()
        vis: list[float] = []

        def camera_loop() -> None:
            i = 0
            while not stop.is_set():
                t0 = time.perf_counter()
                vis.append(vision_frame(i))
                i += 1
                time.sleep(max(0.0, 1 / 15 - (time.perf_counter() - t0)))

        th = threading.Thread(target=camera_loop, daemon=True)
        th.start()
        busy = {"run": True, "gens": []}

        async def llm_loop() -> None:
            while busy["run"]:
                busy["gens"].append(await llm.generate(ctx, max_tokens=60, timeout_s=30))

        task = asyncio.create_task(llm_loop())
        await asyncio.sleep(1.0)
        t_start = time.perf_counter()
        res["together"] = await asyncio.to_thread(measure_side, "together")
        busy["run"] = False
        await task
        stop.set()
        th.join()
        span = time.perf_counter() - t_start
        gens = [x for x in busy["gens"] if x.ok]
        res["llm_together"] = {"total_ms": summary([x.total_ms for x in gens]), "gen_tps": summary([x.gen_tps or 0 for x in gens])}
        res["vision_together_ms"] = summary(vis)
        res["vision_together_fps"] = round(len(vis) / max(span, 1e-6), 1)
        res["llm_memory"] = await asyncio.to_thread(llm.memory, 2.0)
        res["bench_process_rss_mb"] = rss_mb()
        llm.close()
        return res

    out = asyncio.run(run())
    tracker.close(), face.close()
    print(json.dumps({k: v for k, v in out.items()}, indent=1))
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    pl = sub.add_parser("llm")
    pl.add_argument("--model", action="append", default=None)
    pl.add_argument("--reps", type=int, default=3)
    pl.add_argument("--max-tokens", type=int, default=90)
    pt = sub.add_parser("tts")
    pt.add_argument("--reps", type=int, default=3)
    pq = sub.add_parser("tts-quant")
    pq.add_argument("--voice", default="af_bella")
    pq.add_argument("--other", default="af_heart")
    sub.add_parser("stt")
    sub.add_parser("vad")
    pc = sub.add_parser("contention")
    pc.add_argument("--model", default="Qwen3-4B-Instruct-2507-4bit")
    args = p.parse_args()
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    if args.cmd == "llm":
        args.model = args.model or ["Qwen3-4B-Instruct-2507-4bit", "Qwen3-1.7B-4bit"]
        save("llm", bench_llm(args))
    elif args.cmd == "tts":
        save("tts", bench_tts(args))
    elif args.cmd == "tts-quant":
        save("tts-quant", bench_tts_quant(args))
    elif args.cmd == "stt":
        save("stt", bench_stt(args))
    elif args.cmd == "contention":
        save("contention", bench_contention(args))
    elif args.cmd == "vad":
        save("vad", bench_vad(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
