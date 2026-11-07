# Local AI components (Milestone 5)

What LOCKED IN runs on the laptop to understand speech, decide what to say and say it,
which candidates were measured, and why each was or was not selected. Every number below
was measured on the demo machine and is stored in `docs/benchmarks/m5-*.json` together
with the power mode, GPU load and swap at the time
(`backend/scripts/bench_local_ai.py`, `backend/scripts/conversation_eval.py`,
`backend/scripts/latency_check.py`).

**Machine:** MacBook Air (Mac16,12), Apple M4 (4 performance + 6 efficiency cores,
8-core GPU), 16 GB unified memory, macOS 26.4. During the measurements the Mac also
ran the owner's usual software (several Claude Code sessions, a VM, Chrome), with
5-6.5 GB of swap in use; the demo laptop will look similar unless apps are closed.

**Low Power Mode matters more than anything else on this list.** With it on, the GPU
measured 0.70-1.02 TFLOPS fp16 and Qwen3-4B generated 8 tokens/s; with it off, 2.47
TFLOPS and 37 tokens/s (`m5-llm-20261101-120323-LOW-POWER-MODE.json` vs
`m5-llm-20261101-123607.json`). `make offline-check` should be run with it off, and it
must be off for the demo.

## Selected

| Role | Component | Version / weights | License | Why |
|---|---|---|---|---|
| Speech-to-text | whisper.cpp via `pywhispercpp` 1.5.1 (Metal) | `ggml-base.en.bin` from `ggerganov/whisper.cpp` @ `5359861c`, sha256 `a03779c8…6d002` | MIT (code and weights) | 98 ms p50 vs 338 ms for the Milestone 4 engine, fewer word errors (below) |
| Speech-to-text fallback | faster-whisper 1.2.1 (CPU, int8) | `Systran/faster-whisper-base.en` @ `3d3d5dee` | MIT | unchanged from M4; used automatically if whisper.cpp is unavailable |
| Speech detection (hands-free only) | Silero VAD v6 (ONNX Runtime, 1 thread) | `silero_vad_v6.onnx` shipped in faster-whisper, copied to `models/vad/`, sha256 `4cbf549b…be17d2` | MIT | ignores clicks and hums that open the M4 energy detector |
| Conversation | Qwen3-4B-Instruct-2507, 4-bit, MLX (`mlx-lm` 0.32.2, `mlx` 0.32.4) | `mlx-community/Qwen3-4B-Instruct-2507-4bit` @ `50d42775`, sha256 `2a73c6c2…4173f` (converted with mlx-lm 0.26.2 by mlx-community) | Apache-2.0 (weights), MIT (MLX) | the smallest model that picked the right action and stayed in character (below) |
| Local voice | Kokoro-82M v1.0 int8 via `kokoro-onnx` 0.6.1 (ONNX Runtime, CPU), voice `af_bella` | `kokoro-v1.0.int8.onnx` + `voices-v1.0.bin` from thewh1teagle/kokoro-onnx release `model-files-v1.0`, sha256 `6e742170…406cb` / `bca610b8…fbf7d` | Apache-2.0 (weights), MIT (kokoro-onnx), **GPL-3.0-or-later** (phonemizer 3.4.0, espeak-ng 1.52 bundled by espeakng-loader) | renders a reply in ~0.5 s on CPU and sounds like a person; int8 is as close to fp32 as fp32 is to itself, at half the time |
| Character voice | Fish Audio cached clips (Milestone 4) | 39 clips, E-girl voice; generated output, kept out of the repository (README) | Fish Audio terms | unchanged: fixed lines play from the cache in ~10 ms |

Downloads are pinned in `backend/scripts/download_models.py` (`make models`): Hugging Face
files to a commit, MediaPipe to the versioned `float16/1/` path, Kokoro to its release.
Every file is checked against `backend/models.sha256` after the download;
`make verify-models` repeats the check offline, and
`cd backend/models && shasum -a 256 -c ../models.sha256` does it without Python.
On 2026-11-01 every Hugging Face file on the demo Mac matched its pinned commit (LFS sha256
or git blob id), and both MediaPipe files matched the md5 of the `float16/1/` objects,
which are byte-identical to `latest`. The Kokoro release assets publish no digest, so
their manifest entries are the files measured here.
Total on disk: 3.0 GB language models (both sizes), 441 MB Kokoro (fp32 and int8 kept for
comparison), 141 MB whisper.cpp, 141 MB faster-whisper, 1.2 MB Silero VAD, 8 MB MediaPipe.

**GPL note.** Kokoro needs a phonemizer; `kokoro-onnx` uses `phonemizer` (GPL-3.0+) with
the espeak-ng library (GPL-3.0+) loaded in the backend process. Fine for running the demo;
anyone redistributing LOCKED IN with the `local-ai` extra must meet the GPL. Without the
extra, generated replies fall back to the macOS voice.

## Measurements behind each choice

### Language model (`m5-llm-20261101-123607.json`, normal power)

Real persona prompt (581 tokens, cached) plus a realistic per-turn context (278-282
tokens), 5 spec sentences x 3 repetitions, temperature 0.7.

| | Qwen3-4B-Instruct-2507 4-bit | Qwen3-1.7B 4-bit |
|---|---|---|
| Load | 1.9 s | 1.1 s |
| GPU memory, peak | 3.01 GB | 1.66 GB |
| Prompt processing | 286 tokens/s | 585 tokens/s |
| Generation | 37 tokens/s | 78 tokens/s |
| First token, persona cached | 1170 ms p50 | 554 ms p50 |
| Whole JSON reply, persona cached | 2213 ms p50 | 1052 ms p50 |
| Whole reply, nothing cached | 4167 ms p50 | 1988 ms p50 |

Quality on the same five sentences decided it: the 4B chose `set_quiet` for "Can you be
quiet for a bit?" and stayed on the user's calculus goal; the 1.7B refused the quiet
request, answered "five more minutes on my phone" with `release_phone` while saying it
would not give the phone back, and repeated the user's question back. Both sometimes
invented numbers ("a 10-minute deadline"), which is why replies are checked before they
are spoken (`dialogue.check_reply`). The conversation evaluation compares both models on
35 scenarios (`docs/MILESTONE5_EVALUATION.md`): 92.4% vs 68.2% of actions right, held out 81% vs 50%.

Latency work that followed from these numbers: the persona's KV cache is kept for the
life of the worker; the worker keeps the last prompt it read and only reads what changed
(a retry, the next turn); the per-turn context is prefilled while the user is still talking
(push-to-talk down), rendered as of that moment, so after Whisper only the quoted sentence is
left to read; the reply streams and its first sentence goes to the voice as soon as the
action in front of it has been validated (MILESTONE5_VERIFICATION.md has the end-to-end numbers).

### Speech-to-text (`m5-stt-20261101-125118.json`)

104 utterances: 31 sentences (the 11 Milestone 4 commands and 20 natural ones) in three
macOS voices, plus the 11 commands recorded through the MacBook microphone in the
Milestone 4 demo check (Mac speakers -> mic). Same base.en weights and prompt for all.

| Engine | p50 | p90 | Word error rate | Exact | Real-mic errors |
|---|---|---|---|---|---|
| faster-whisper int8, CPU (M4 default) | 338 ms | 351 ms | 2.2% | 98% | 0 of 11 |
| **whisper.cpp, Metal GPU** | **98 ms** | **104 ms** | **0.3%** | **99%** | **0 of 11** |
| whisper.cpp, CPU only | 345 ms | 366 ms | 0.3% | 99% | 0 of 11 |
| whisper.cpp with `audio_ctx` cut to the clip | 86 ms | 3280 ms | 47% | 66% | 4 of 11 |

The speed comes from the GPU, which the language model also uses (see contention).
Confidence is the geometric mean of token probabilities, the same quantity
faster-whisper reports, so the command gates (0.45, 0.8 for motion) keep their meaning.

### Local voice (`m5-tts-20261101-123944.json`, `m5-tts-quant-20261101-124158.json`)

Six typical generated replies (3.3 s of speech median), 3 repetitions, voice `af_heart`.

| Kokoro v1.0 | Load | Render, whole reply | First sentence | Extra memory |
|---|---|---|---|---|
| fp32, CPU | 1.4 s | 1040 ms p50 (0.3x real time) | 582 ms | 659 MB |
| **int8, CPU** | 0.6 s | **523 ms p50 (0.2x)** | **298 ms** | 316 MB |
| fp32, CoreML | 5.2 s | 1227 ms (0.4x) | 731 ms | 639 MB |

int8 against fp32 for `af_bella`, measured without listening: average spectral envelope
difference 0.45 (another voice, `af_heart`: 3.48), median pitch 199 vs 203 Hz (other voice
211 Hz), length 99.3%. A frame-by-frame comparison (8.0 vs 12.9) is dominated by small
timing shifts and overstates the difference. A person still has to listen
(MILESTONE5_VERIFICATION.md, H3).

Voice choice: the 39 Fish Audio clips have a median pitch of 197 Hz, a pitch spread
(interquartile range) of 38 Hz and 2.07 words/s. Of 11 American female Kokoro presets,
`af_bella` is closest (205 Hz, 40 Hz, 2.95 words/s); next `af_river` and `af_heart`.
**Kokoro is not the Fish Audio character voice.** The dashboard labels every line with
the voice that actually spoke it ("Cached Fish Audio", "Local Kokoro voice").

### Speech detection (`m5-vad-20261101-121357.json`; cost measured in Low Power Mode)

20 synthetic utterances mixed with generated noise, and 20 noise-only clips of 4 s:

| Noise | Speech found: energy / Silero | False starts on noise alone: energy / Silero |
|---|---|---|
| white, -50 dBFS | 20 / 20 | 0 / 0 |
| white, -35 dBFS | 18 / 20 | 0 / 0 |
| keyboard-like clicks, -50 dBFS | 20 / 20 | **14 / 0** |
| keyboard-like clicks, -35 dBFS | 20 / 20 | **19 / 0** |
| 120 Hz hum, -50 dBFS | 20 / 20 | 0 / 0 |
| 120 Hz hum, -35 dBFS | 17 / 20 | 0 / 0 |

Silero costs 0.12 ms of CPU per 32 ms frame. It does not tell the user's voice from other
people's, which was the actual hands-free failure in Milestone 4 (2 of 10 commands right
with room conversation): push-to-talk stays the default.

### Everything at once (`m5-contention-20261101-125525.json`)

Worst case: the language model writing replies back to back, the camera pipeline's
per-frame work (JPEG decode, hand landmarks, face every third frame; synthetic frames) at
15 fps, and whisper.cpp and Kokoro measured on top.

| | Alone | Together |
|---|---|---|
| Camera frame processing | 8.5 ms p50 | 32.6 ms p50, 64 ms p90; 14.2 of 15 fps sustained |
| whisper.cpp, GPU | 94 ms | 284 ms p50, 860 ms p90 |
| whisper.cpp, CPU | 346 ms | 1165 ms |
| Kokoro int8 | 623 ms | 3386 ms |
| Language model reply | 2406 ms, 37 tokens/s | 2868 ms, 32 tokens/s |
| Language model worker memory | | 261 MB RSS + 2.43 GB GPU (peak 2.96 GB) |

The camera keeps up. Kokoro and the model compete for memory bandwidth, not only cores:
Kokoro is 5x slower while the model is generating. Inside one turn they mostly run one
after the other; `latency_check.py` measures the real sequence.

## Evaluated, not selected

| Candidate | What was done | Why not |
|---|---|---|
| Qwen3-1.7B (MLX 4-bit) | measured above and in the conversation evaluation | twice as fast, clearly worse at choosing actions; kept as `LOCKEDIN_LOCAL_LLM_MODEL=Qwen3-1.7B-4bit` |
| huggingface/speech-to-speech | design review only (not installed) | a cascaded VAD -> STT -> LLM -> TTS pipeline of the same component classes chosen here; adopting it means replacing the runtime that owns safety, sessions and the robot. Its Mac STT path (`mlx-whisper`) requires PyTorch |
| pipecat-ai/pipecat | design review only (not installed) | a frame-pipeline framework for voice agents, mostly around cloud services and WebRTC transports; its local pieces (Silero VAD, Whisper, a local LLM) are used here directly. A framework rewrite would add risk without a measured gain |
| dscripka/openWakeWord | design review only (not installed) | no "locked in" model: a custom one needs its training pipeline (PyTorch, synthetic speech); the pre-trained models are CC BY-NC-SA 4.0. The existing wake phrase mode reads "locked in, ..." from the transcript, now that whisper.cpp takes ~100 ms |
| `silero-vad` pip package | metadata check | requires PyTorch; the same model runs from its ONNX file |
| `mlx-whisper` | metadata check | requires PyTorch and numba |
| whisper.cpp `audio_ctx` trimming | measured above | 47% word errors |
| Kokoro fp32 / CoreML | measured above | slower; fp32 kept on disk for comparison |

PyTorch was avoided on purpose: the disk had 15-19 GB free and every selected component
runs without it.
