# Real speaker and microphone tests

Software tests cover the speech queue, interrupts, echo filtering and device failures with
fake audio. These tests check the real hardware. Results so far: [Results](#results-2026-11-01-milestone-4).

## What is available (checked 2026-11-01, Milestone 4)

| Device | Status |
|---|---|
| MacBook Air microphone | present, default input, 48 kHz |
| MacBook Air speakers | present |
| JLab GO Party (Bluetooth) | **connected**: output 44.1 kHz; its 16 kHz hands-free mic must not be the input (it drops the speaker to call quality) |
| Beats Pill (Bluetooth) | paired, **not connected**: its tests are pending |

## Before you start

* Stop the backend for the `scripts/audio_check.py` steps (they need the devices), start it
  again (`make demo`) for the dashboard steps.
* Voice pack cached (Personality + Voice → Offline cache: "Voice pack 39/39").
* Grant Microphone access to the terminal app if `--mic` reports exact silence.

## Tests

| ID | Test | How | Pass |
|---|---|---|---|
| A1 | Laptop microphone | `uv run python scripts/audio_check.py --mic`, then talk; dashboard Voice panel → Test mic | speech detected; RMS while talking at least 20 dB above silence |
| A2 | Bluetooth speaker output | connect the speaker; `audio_check.py --list`; `--speaker --output "<name>"`; then dashboard → Personality + Voice → Output device → the speaker; press Phone grab | the line plays on the speaker; note the extra delay (film the key press if it matters) |
| A3 | Bluetooth microphone (only if the speaker has one) | select it as input in Settings → Voice; A1 again | macOS switches the speaker to a low-quality call profile when its mic is used, so **expected outcome: keep the laptop mic** and use the speaker for output only |
| A4 | Interrupting speech | dashboard: start a session, press Phone grab sample, then STOP while it talks; push-to-talk: hold the key while it talks; hands free (Settings → Voice → always on): say "stop" over a long line | speech cuts within a blink; the history shows `interrupted`; nothing queued plays afterwards |
| A5 | Stop and release over speech | simulated robot armed, session running: trigger a reach (Closed loop → Reach), say "give me my phone" while it jokes | robot stops, speech cuts, release line plays, session ends |
| A6 | Echo and self-trigger | at demo volume and placement: `audio_check.py --echo --output "<speaker>"`; hands free: make it say "Emergency stop. I'm not moving." (E-STOP, then Reset) and watch the timeline | `--echo` prints "would drop this as its own voice" (or nothing intelligible reached the mic); no self-triggered e-stop; the timeline shows "echo of the robot's own speech" if the mic heard it |
| A7 | Speaker disconnect | while a line plays on the Bluetooth speaker, switch the speaker off | the history shows `device error`, lines show as text for 15 s, then it tries again; picking the output again restores sound |
| A8 | Microphone disconnect | hands free on a Bluetooth/USB mic: switch it off | Voice panel warns "Microphone stopped delivering audio"; it clears when audio returns |
| A9 | Offline voice | join the robot's Wi-Fi or turn Wi-Fi off; press the samples | history shows "Cached Fish Audio"; uncached lines say why they used the local voice |

## Results 2026-11-01 (Milestone 4)

Scripts: `scripts/voice_loop_check.py` (speaker, echo, commands; a macOS voice on a speaker
plays the user, the MacBook mic listens) and `scripts/demo_check.py` (the real app started
as `make hardware` starts it, in a sandbox with no internet and no Fish key, push-to-talk over
HTTP, MacBook speakers as the user, the robot's voice on the JLab). Raw results:
`data/voice_tests/` (git-ignored). The person saying the commands was a macOS voice, so these
prove the microphone, recogniser, app and speaker path, not accuracy for a human voice.

| Test | Device | What happened | Status |
|---|---|---|---|
| A2 speaker | JLab GO Party | 05:14: cached Fish line "Nice try." played on the JLab; the MacBook mic heard "Nice try." (Whisper, exact), -41.8 dBFS, sound at the mic 346 ms after playback started (device reports 466 ms) | PASS, observed by the microphone; **a person has not confirmed it yet** |
| A2 speaker | JLab GO Party | 05:43: same test, mic heard silence (-58 dBFS): macOS output was **muted** (volume 16, muted) on the JLab | not a playback failure; the app now warns about it (below) |
| A2 speaker | Beats Pill | not connected | BLOCKED |
| A6 echo | JLab GO Party | 05:14: the mic heard "I found one too. Emergency stop. I'm not moving." (Whisper added words in the quiet); echo score 0.56 < 0.6, so it would have been taken as a command | **FAIL**, fixed: the echo score now also counts how much of the robot's line was heard (1.0 here); the exact transcript is a regression test (`test_voice_loop.py`) |
| A6 echo | JLab GO Party | 05:18: re-run after the fix; nothing intelligible reached the mic | inconclusive (the case did not recur) |
| A7 disconnect | — | needs someone to switch the speaker off during a line | NOT TESTED (software path tested: `test_speaker.py`) |
| Commands, hands free | JLab plays the user | run 1: 6/10 recognised; run 2: 2/10, with people talking nearby transcribed instead ("I did not make this preset.") | FAIL in a talking room: use push-to-talk (the demo default) |
| Commands, close | MacBook speakers play the user | 9/10 recognised ("How much longer?" heard as nothing); Whisper 1151 ms p50, 1780 ms max | PASS 9/10 |
| End-to-end app, offline | MacBook speakers → mic, JLab out | 05:32: 11/11 commands transcribed exactly with the right intent; 10/11 state checks (the 11th was the harness: with no robot the state stays DISCONNECTED by design; fixed). Cached Fish replies started 1.04-1.31 s after the speech ended; two local-voice replies took 3.9 and 5.7 s (rendered on demand) | PASS with a latency defect, fixed |
| End-to-end app, offline | same | 05:42: 11/11 PASS (transcript, intent, state change, reply); every reply from cache, 0.84-1.34 s from end of speech to playback (backend clock), including "how much time do I have left" (0.91 s, was 3.9) and "roast me" (0.94 s, was 5.7). The mic check of these replies is void: macOS output was muted | PASS in the app; audible playback not verified for this run |

Fixes that came out of these runs: echo coverage scoring (A6), local-voice lines rendered ahead
and before the background prewarm (latency), a macOS mute/volume check in the dashboard and in
`make offline-check` (A2 at 05:43), cached Fish clips playing without an API key.

## Recording template (PERFORMANCE.md)

```markdown
### Audio tests · <date>

| Field | Value |
|---|---|
| Output device / input device | |
| Volume, distance speaker ↔ mic | |
| Listen mode | push-to-talk / always on |

| Test | Result | Notes |
|---|---|---|
| A1 | | |
```
