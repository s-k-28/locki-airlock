# Personality and voice

LOCKED IN talks like a friend who is on your side: it teases, celebrates, swears if you let
it, and drops the jokes the moment you need help or something goes wrong. The personality
changes **how** it says things, never **what** happened: every number and event comes from
the session record, and safety commands always win.

Dashboard: top bar → **Personality + Voice** (the waveform button).

## Personality

| Mode | Feels like | Profanity cap | Insults aimed at you |
|---|---|---|---|
| Chill | friendly, relaxed, lightly sarcastic | mild | never |
| Roast (default) | casual trash talk, like an actual friend | strong | never |
| Unhinged | high-energy roasting ("Dumbass, that's your third attempt.") | strong | yes, **only after you opt in**, paced |
| Judge demo | energetic and memorable, presentation-safe | **none** | never |

**Profanity** is your choice (Off / Mild / Strong; default Off), capped by the mode. Mild:
damn, hell, ass. Strong: adds the rest. Each line in the library carries its measured level
and a test keeps the tags honest.

**Unhinged needs an opt-in**: the switch under the mode tiles, or saying "unhinged mode"
(saying it counts as opting in). Until then it talks like Roast. Even then, insults are
paced: never twice in a row, at least 90 s apart, and none for 5 minutes after you ask for
help. Never, at any setting: slurs, threats, attacks on identity or appearance, or
humiliating words ("worthless", "pathetic"). It teases the behaviour, not the person.

**Tone overrides** (voice or dashboard):

| Say | Effect |
|---|---|
| "Stop roasting me" / "be nice" | gentle: chill lines, no swearing, no insults |
| "Be serious" / "no jokes" | plain facts only |
| "You can joke again" / "back to normal" | normal tone |
| "Be quiet" / "mute" / "shut up" | silence now, queue cleared (text still shows) |
| "Stop" | robot stops first, speech is cut |
| "Give me my phone" | robot stops, speech is cut, session ends |
| "I'm stuck" / "help" | helpful answer, no insults for 5 minutes |
| "Roast mode", "chill mode", "judge demo mode", "unhinged mode" | switch mode |
| "No swearing" / "you can swear a little" / "swear all you want" | profanity off / mild / strong |

**What it reacts to** (each is a real recorded event): session start, phone reach (early,
first, repeated, many, while it already holds the phone), progress, reaching your target,
time's up, ending early, pause/resume, halfway / five minutes left, looking away and coming
back, release, stop, emergency stop (a plain "Emergency stop. I'm not moving.", never a
joke). Event → response intent (e.g. phone reach → playful warning, target reached →
celebration, help → helpful explanation) → a line in the current personality → the speech
queue.

## Voice

Providers: **Fish Audio** (cloud, optional), **Local voice** (macOS `say`, offline, free),
**Text only**. Without a Fish Audio key everything works with the local voice.

### 1. Add your Fish Audio API key (done on this laptop)

1. Create a key at <https://fish.audio> (API keys page of your account).
2. In the project root, copy `.env.example` to `.env` if you have not, and set:
   ```
   FISH_AUDIO_API_KEY=your-key-here
   ```
3. Restart the backend (`make demo` / `make dev`).

The key stays on the backend: it is sent only in the `Authorization` header to
api.fish.audio, never to the dashboard, never written to logs (tests check both). `.env` is
git-ignored.

### 2. Choose the voice

The **E-girl** voice you picked is the default preset: Fish Audio voice
`ca3007f96ae7499ab87d27ea3599956a` (fish.audio/m/ca3007f96ae7499ab87d27ea3599956a, a public
library voice; its page names no real person it was cloned from). To use another:

* paste a voice id or a `fish.audio/m/...` link in **Voice id** → Set, or
* **Search** public voices / **My voices** (your own clones), listen to Fish's sample clip
  (free), **Use**.

**Check** (free) confirms the key, the network and the voice in one request. Only use voices
you have the right to use; do not clone anyone's voice without their permission.

Settings (what Fish Audio's API offers): model (`s2.1-pro-free` default, $0;
`s2.1-pro` / `s2-pro` $15 per million bytes of text; `drama-3`; `s1` deprecated), speed
0.5-2.0, expressiveness (temperature), variety (top-p), latency mode, and delivery cues: S2
models read `[excited]`-style hints, so a celebration really sounds excited. Volume and
output device apply to every provider.

### 3. Presets

A preset sets provider, voice, model, voice settings, personality, profanity and output
device at once, and the next line uses it (no restart). Built in: **E-girl (Fish Audio)**,
**LOCKED IN Original** (local voice), **Sarcastic Bro**, **Chaotic Coach** (Unhinged: needs
your opt-in), **Calm Study Buddy**. The last three have no voice yet: paste one, then **Save
current as…** to keep it as your own preset.

### 4. Generate the offline cache (do this before joining the robot's Wi-Fi)

The Zeus Car's Wi-Fi has no internet. Cached clips play in ~10 ms with no network.

* Dashboard: **Offline cache** → **Plan: this personality** (or all modes) shows how many
  lines are missing, the bytes of text and the estimated cost → **Generate N clips · approve
  B bytes**. Nothing is generated without that click.
* Or from a terminal with the backend stopped:
  ```bash
  cd backend
  uv run python scripts/generate_voice_cache.py                 # plan only, sends nothing
  uv run python scripts/generate_voice_cache.py --yes --max-bytes 9000
  ```

Clips are keyed by voice, model, settings and exact text: change any of them and the old
clips are simply not used (no stale voice). Only fixed lines are stored; lines with session
details (task names, times) and LLM replies are never written to disk.

### 5. Live generation (optional, off by default)

"When a line is not cached": **Local voice** (default, never calls the API while speaking)
or **Generate live** (after a confirmation): uncached lines go to Fish Audio within a daily
byte budget (default 5,000 bytes). Safety lines never wait for the network. If the network
drops, it falls back to the local voice and stops trying for 30 s.

## Speech priorities

| Priority | Lines | Rule |
|---|---|---|
| P0 | stop, emergency stop, release, mute | cuts whatever is playing, clears the queue, never waits for the network |
| P1 | essential status (time's up, halfway, can't move) | goes before answers and jokes |
| P2 | answers to you | cuts a joke that is playing |
| P3 | jokes, celebrations, ambient remarks | one waits at most (the newest), 4 s shelf life |

An interrupt invalidates everything queued before it, so a joke can never play after an
emergency stop. A burst of reaches produces one reaction, not five. Duplicate lines within
2 s are dropped. If the speaker disappears (Bluetooth off), lines show as text for 15 s and
it tries again.

**Talking over the robot.** Push-to-talk: pressing the key stops answers and jokes. Hands
free: the mic keeps listening while the robot talks; anything that matches the robot's own
line (its echo) is dropped, and only stop, mute, release, pause, "be serious" and "stop
roasting me" are taken over its voice. So the robot saying "Emergency stop" cannot trigger
an emergency stop.

**Speech history** in the drawer shows each line with its real source: Cached Fish Audio,
Live Fish Audio, Local voice (cached), Local voice, Text only, or Not spoken (muted,
replaced, stale, cancelled), plus why it fell back ("not cached; live generation is off").

## Robot expression

Each moment has a light state (locking in, reach warning, celebration, break, stopped,
emergency stop) shown as a chip in the top bar. On the Zeus Car there is no verified way to
set the RGB strip (the stock sketch only shows its own mode colours), so LOCKED IN sends no
LED frames; the simulator reports the state as simulated. No celebratory motion until the
hardware tests pass.

## The language model (Milestone 5)

By default (`LOCKEDIN_LLM_PROVIDER=local`) a model on the laptop (Qwen3-4B, MLX, offline)
writes the replies to anything the deterministic parser should not settle alone, and the
reactions it prepares for the next reach. The personality settings become its STYLE line
(tone, profanity limit, insults only in opted-in unhinged mode, kind after "help"). Its words
pass the same content guard as the fixed lines plus the reply guards (numbers on record, no
motion claims, no unrecorded sightings, no guessed feelings, no repeats, length); refused
words are never spoken. It can request a typed action; it cannot reach the motors.

**Voice:** generated lines have no Fish Audio clip, so the **local Kokoro voice (`af_bella`)
speaks them**; it is not the Fish Audio character voice, and the speech history labels every
line with the voice that spoke it. Generated lines are never sent to Fish Audio, online or
not. To hear the difference: `backend/scripts/voice_samples.py` writes the same sentences in
the Kokoro candidates next to cached Fish clips (`data/voice_samples/`).

The cloud option (`LOCKEDIN_LLM_PROVIDER=anthropic`, a paid API, off by default and never
used offline) is unchanged: text-only replies to open questions with the same guards.

## What has and has not been verified

| | Status |
|---|---|
| Personality, profanity, tones, pacing, guard, intents | automated tests |
| Speech queue: priorities, interrupts, stale lines, device failure | automated tests (fake audio) |
| Fish Audio request format, errors, cache, budget, offline fallback | automated tests against a mock API, written from docs.fish.audio (2026-11-01) |
| Local voice through the real audio path | run on this laptop at volume 0: cached reactions play in their audio length, a line with numbers needs ~2 s to render |
| **Real Fish Audio** (2026-11-01, key in `.env`) | E-girl voice found via the API (trained, public, English TTS voice); `s2.1-pro-free` synthesises it; measured request below; 39-line voice pack generated; cached clips play with Fish Audio unreachable. Paid models **not tested** (account credit is 0) |
| Real speakers, Bluetooth speaker, echo from a real speaker into the mic | not measured ([AUDIO_TESTS.md](AUDIO_TESTS.md)) |

### Real Fish Audio measurements (MacBook Air, home Wi-Fi, 2026-11-01)

| Measure | Value |
|---|---|
| Test line | "Bro, we literally just started. Get your ass back to work." (58 bytes, `s2.1-pro-free`) |
| First audio bytes | 656 ms |
| Whole clip received | 2,047 ms for 3.90 s of audio |
| Playback start after that | 68 ms (first use in a fresh process) + 28 ms device latency |
| Spoken correctly | local Whisper transcript matched 100 %, "ass" included |
| Voice pack generation | 39 clips, median 1.50 s per clip (p95 2.07 s), 0 failures, 1,511 bytes |
| Cost | $0: `s2.1-pro-free`; account credit 0.0 before and after |
| Offline | Fish Audio unreachable: cached pack lines played as "Cached Fish Audio"; a live request failed cleanly ("no connection") |

A line that is not cached therefore needs ~2 s live, which is why reactions come from the
pack. Whether the clip *sounds* like the E-girl voice is a listening check: the test clip is
in `data/voice_tests/`.
