"""Pre-generate Fish Audio clips for the robot's fixed lines, for offline use.

    uv run python scripts/generate_voice_cache.py                       # show the plan only
    uv run python scripts/generate_voice_cache.py --scope all_modes     # plan for every mode
    uv run python scripts/generate_voice_cache.py --yes --max-bytes 9000

Uses the voice profile and personality saved by the dashboard (data/runtime_settings.json)
and FISH_AUDIO_API_KEY from .env. Without --yes nothing is sent to Fish Audio. With --yes,
--max-bytes must cover the plan: that number is your approval of what may be billed (Fish
Audio counts UTF-8 bytes of text; the free s2.1-pro-free model costs nothing).

Run it while the backend is stopped (both write the same cache), or use the dashboard's
Personality + Voice drawer, which does the same thing.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.config import RuntimeSettingsStore, Settings  # noqa: E402
from app.voice.audio_cache import AudioCache  # noqa: E402
from app.voice.fish_audio import CacheJob, CachePlanItem, FishAudioVoice, estimate_usd  # noqa: E402
from app.voice.personality import Style, cacheable_lines, voice_pack  # noqa: E402
from app.voice.tts import SpeechRequest  # noqa: E402
from app.voice.usage import UsageLedger, utf8_bytes  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scope", choices=["pack", "pack_chill", "current", "all_modes"], default="pack")
    ap.add_argument("--yes", action="store_true", help="actually generate (billable unless the model is free)")
    ap.add_argument("--max-bytes", type=int, default=0, help="most text bytes you approve sending")
    a = ap.parse_args()

    settings = Settings()
    rs = RuntimeSettingsStore(settings.data_dir / "runtime_settings.json").load()
    p, c = rs.voice_profile, rs.coach
    if p.provider != "fish_audio" or not p.reference_id:
        print("The saved voice profile is not Fish Audio with a voice id. Choose one in the dashboard first.")
        return 1
    fish = FishAudioVoice(
        settings.fish_audio_api_key,
        AudioCache(settings.data_dir / "tts_cache" / "fish_audio"),
        UsageLedger(settings.data_dir / "tts_usage.json"),
        base_url=settings.fish_audio_base_url,
    )
    if a.scope in ("pack", "pack_chill"):
        lines = voice_pack(include_chill=a.scope == "pack_chill")
    else:
        if a.scope == "all_modes":
            styles = [Style(mode=m, profanity=c.profanity, unhinged_opt_in=True) for m in ("chill", "roast", "unhinged", "judge_demo")]
        else:
            styles = [Style(mode=c.personality, profanity=c.profanity, unhinged_opt_in=c.unhinged_opt_in)]
        lines = cacheable_lines(styles + [Style(tone="serious")])
    items = []
    total = 0
    for category, text, cue in lines:
        tts_text = fish.text_for(SpeechRequest(text, cue), p)
        key = fish.cache_key(tts_text, p)
        total += 1
        if not fish.cache.has(key):
            items.append(CachePlanItem(category, text, tts_text, key, utf8_bytes(tts_text)))
    nbytes = sum(i.nbytes for i in items)
    print(f"Voice {p.reference_id} · model {p.model} · personality {c.personality} · profanity {c.profanity} · scope {a.scope}")
    print(f"{len(items)} of {total} lines need generating: {nbytes} bytes of text, estimated ${estimate_usd(nbytes, p.model)}")
    for i in items[:10]:
        print(f"  - {i.tts_text}")
    if not items:
        return 0
    if not a.yes:
        print("\nPlan only. Re-run with --yes --max-bytes N (N >= the bytes above) to generate.")
        return 0
    if not fish.configured:
        print("FISH_AUDIO_API_KEY is not set in .env.")
        return 1
    if a.max_bytes < nbytes:
        print(f"--max-bytes {a.max_bytes} does not cover the plan ({nbytes}). Nothing sent.")
        return 1

    def progress(state: dict) -> None:
        print(f"\r{state['done']}/{state['total']} done, {state['failed']} failed", end="", flush=True)

    state = await CacheJob(fish, items, p, on_progress=progress).run()
    print(f"\n{state['state']}: {state['done']} clips, {state['bytes']} bytes{'; ' + state['error'] if state.get('error') else ''}")
    return 0 if state["state"] == "finished" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
