"""The real local models, no doubles: whisper.cpp through the STT worker, and the Qwen3
worker process (prefill, streaming, cancel, memory). Nothing plays aloud; no network.

The language model test loads 2.3 GB of weights and takes ~10 s, so it runs only with
LOCKEDIN_TEST_LOCAL_LLM=1 (docs/MILESTONE5_VERIFICATION.md records each run)."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
import wave
from pathlib import Path

import numpy as np
import pytest

from app.intelligence.actions import ActionName, parse_reply
from app.intelligence.prompting import SAMPLE_CONTEXT
from app.voice.transcription import WhisperTranscriber

MODELS = Path(__file__).resolve().parents[1] / "models"
LLM = "Qwen3-4B-Instruct-2507-4bit"


def spoken(text: str, tmp_path) -> np.ndarray:
    wav = tmp_path / "said.wav"
    subprocess.run(["say", "-o", str(wav), "--file-format=WAVE", "--data-format=LEI16@16000", "--", text], check=True, capture_output=True, timeout=30)
    with wave.open(str(wav)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768


@pytest.mark.skipif(shutil.which("say") is None, reason="needs macOS `say` to speak the sentence")
def test_whisper_cpp_worker_transcribes_a_natural_sentence(tmp_path):
    if not (MODELS / "whispercpp" / "ggml-base.en.bin").exists():
        pytest.skip("whisper.cpp weights not downloaded (scripts/download_models.py)")
    pytest.importorskip("pywhispercpp")
    tr = WhisperTranscriber("base.en", "int8", MODELS, engine="whisper.cpp")
    try:
        t0 = time.monotonic()
        while tr.state == "loading" and time.monotonic() - t0 < 60:
            time.sleep(0.05)
        assert tr.state == "ready", tr.error
        out = tr.transcribe(spoken("I have a calculus test tomorrow.", tmp_path))
        assert out is not None
        assert "calculus" in out["text"].lower() and "tomorrow" in out["text"].lower()
        assert 0.45 <= out["confidence"] <= 1.0  # same scale as faster-whisper: the command gates keep their meaning
        assert tr.describe()["engine"] == "whisper.cpp"
    finally:
        tr.close()


@pytest.mark.skipif(os.environ.get("LOCKEDIN_TEST_LOCAL_LLM") != "1", reason="loads 2.3 GB: set LOCKEDIN_TEST_LOCAL_LLM=1")
async def test_the_real_local_model_answers_in_json_streams_and_uses_the_prefill():
    if not (MODELS / "llm" / LLM / "config.json").exists():
        pytest.skip("model not downloaded")
    from app.intelligence.local_llm import LocalLLM

    llm = LocalLLM(LLM, MODELS)
    try:
        t0 = time.monotonic()
        while llm.state == "loading" and time.monotonic() - t0 < 120:
            await asyncio.sleep(0.1)
        assert llm.state == "ready", llm.error
        context = SAMPLE_CONTEXT.replace("{utterance}", "Can you be quiet for a bit?")
        prefix = context[: context.index("\n\nUSER JUST SAID")]
        llm.prefill(prefix)
        deltas: list[str] = []
        g = await llm.generate(context, max_tokens=90, temperature=0.0, timeout_s=20, on_delta=deltas.append)
        assert g.ok and len(deltas) > 3 and "".join(deltas) == g.text
        assert g.stats.get("prefilled") is True and llm.prefill_hits == 1
        reply = parse_reply(g.text, frozenset(ActionName))
        assert reply.say and not [p for p in reply.problems if "JSON" in p]
        # A background request yields to a person.
        bg = asyncio.create_task(llm.generate(context, max_tokens=200, timeout_s=20, background=True))
        await asyncio.sleep(0.3)
        fg = await llm.generate(context, max_tokens=40, temperature=0.0, timeout_s=20)
        assert fg.ok and (await bg).finish in ("cancelled", "superseded", "stop", "length")
        mem = await asyncio.to_thread(llm.memory, 2.0)
        assert mem.get("mlx_active_gb", 0) > 1.0 and mem.get("rss_mb")
    finally:
        llm.close()


@pytest.mark.skipif(os.environ.get("LOCKEDIN_TEST_LOCAL_LLM") != "1", reason="loads 2.3 GB: set LOCKEDIN_TEST_LOCAL_LLM=1")
async def test_reusing_the_cached_prompt_does_not_change_the_reply():
    """Greedy decoding: the same context gives the same words whether the worker read it from
    scratch, after a different context, or reused most of it from the previous request."""
    if not (MODELS / "llm" / LLM / "config.json").exists():
        pytest.skip("model not downloaded")
    from app.intelligence.local_llm import LocalLLM

    llm = LocalLLM(LLM, MODELS)
    try:
        t0 = time.monotonic()
        while llm.state == "loading" and time.monotonic() - t0 < 120:
            await asyncio.sleep(0.1)
        assert llm.state == "ready", llm.error
        a = SAMPLE_CONTEXT.replace("{utterance}", "Why do you keep doing that?")
        b = SAMPLE_CONTEXT.replace("{utterance}", "Can you be quiet for a bit?")
        first = await llm.generate(a, max_tokens=60, temperature=0.0, timeout_s=30)
        other = await llm.generate(b, max_tokens=60, temperature=0.0, timeout_s=30)  # shares the start with a
        again = await llm.generate(a, max_tokens=60, temperature=0.0, timeout_s=30)  # reuses all but the end
        assert first.ok and other.ok and again.ok
        assert again.stats.get("prefilled") is True
        assert again.text == first.text
    finally:
        llm.close()
