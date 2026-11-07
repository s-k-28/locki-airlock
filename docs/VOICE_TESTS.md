# Voice tests with real people: protocol

Speech recognition has only been measured on synthesised speech (100 % intent accuracy on
63 utterances, docs/PERFORMANCE.md) and one live speaker-to-mic trial. These tests measure
what matters at the demo table: real voices, real noise, the configured listening mode.
They replace A1-A2 in [TEST_PLAN.md](TEST_PLAN.md). Nothing here has been run yet.

## What is measured, in order of importance

| Metric | Why | Target before the demo |
|---|---|---|
| **Stop recognised** (stop / emergency stop prompts) | the voice stop is a safety path (the dashboard E-STOP is the primary one) | ≥ 95 % in every condition |
| **Risky errors** (a wrong intent or false activation that moves the robot, gives the phone back or ends the session) | the dangerous kind of mistake | 0 |
| **Wrong-intent rate** (wrong intent or wrong number/duration) | worse than a rejection: the system acts on a mistake | ≤ 2 % |
| **Accuracy** (correct / prompts) | usability | ≥ 85 % with push-to-talk in hackathon noise |
| **Rejection rate** (rejected + nothing heard) | annoying but safe: the speaker repeats | ≤ 15 % |
| **False activations per 10 min** (quiet windows) | the robot reacting to conversation | ≤ 1 in wake-word mode; push-to-talk should be 0 by construction |
| STT latency p50 / p95 | responsiveness | report only |

A rejection is a safe failure. A wrong intent is not. Judge the results in that order.

## People

- Each teammate, plus 2-4 other people if they agree (different voices and accents help).
  Ask first; tell them only text is kept, never audio, and that they can stop any time.
- Use **labels** (A, B, C…) in the tool, never names. Keep the label → person mapping off
  the laptop if you keep it at all.

## Conditions

| Condition | How |
|---|---|
| quiet room | indoors, door closed |
| hackathon noise | play a crowd/café noise recording from a phone 1-2 m away, about as loud as normal conversation (≈ 60-65 dBA if a sound-meter app is at hand; write down what you used) |
| music playing | music at background level from a laptop or speaker |
| robot speaker talking | trigger a robot line (e.g. type "How am I doing?") while the speaker says the prompt: checks echo suppression |
| 2 m from the mic | speaker stands 2 m away instead of the demo distance |

Demo distance: the speaker sits where they will sit at the demo (≈ 50-70 cm from the
laptop mic). Use the **laptop microphone**, not a Bluetooth headset (HARDWARE_ASSEMBLY §7).

## Listening modes

Test the mode you will demo first (push-to-talk), then wake word. Run always-on only in the
quiet windows, to see why it is not the default.

## Procedure

1. Backend running, **Start microphone** in the voice panel, mode selected in Settings →
   Voice. Robot: simulated or disconnected (dry runs never act, but there is no reason to
   have a real car armed).
2. Dashboard → debug drawer → **Experiments** → Voice trials: speaker label, condition,
   leave **Dry run** on (utterances are classified and scored exactly as in normal use but
   not executed; stop words still halt any motion, as always). **Start block.**
3. **Next prompt** shows a phrase. The speaker says it once, naturally, in their own words
   if they like (the score checks the intent and the numbers, not the wording). The next
   utterance is scored against the prompt automatically.
4. If nothing is transcribed within ~5 s (the VAD or push-to-talk missed it), press
   **Nothing heard**. Do not repeat a prompt; move on.
5. Go through the full list once (22 prompts: 19 commands, including stop and emergency stop,
   plus 3 near-miss sentences that must not act). For stop, also try one shouted and one
   quiet "stop".
6. **Quiet window 2 min**: two or more people talk normally near the laptop about anything
   (mentioning phones, finishing, stopping is good), nobody addresses LOCKED IN. Everything
   transcribed in that window is scored as a correct rejection or a false activation. Do at
   least 2 windows per condition (≥ 4 min).
7. **End block.** Next condition, or next speaker.

Minimum set: 2 speakers × 2 conditions (quiet room, hackathon noise) × full list + 4 min
quiet each = about 90 prompts and 16 minutes of quiet windows. More is better.

## Reading the results

The table under the panel groups results by speaker and condition (accuracy, wrong,
rejected, stop OK, false activations per 10 min, risky errors, STT p50). Each utterance is
also an `experiment.voice_trial` event in the timeline and a line in
`data/experiments/voice_trials.jsonl`.

For every risky error or wrong intent, write down the transcript (what Whisper heard) and
the intent: the fix is in the intent rules (`backend/app/voice/intents.py`, with a test in
`tests/test_intents.py`) if the transcript was right, or in the microphone setup, mode or
confidence thresholds if the transcript was wrong.

Example found while building this tool, on text alone: "I'm done with this problem" was
classified as *end session* (it ended the whole session); it is now *progress*, with a
regression test.

## Recording template (PERFORMANCE.md)

```markdown
### Voice trials · <date>

| Field | Value |
|---|---|
| Mode | push-to-talk / wake word / always-on |
| Microphone, distance | laptop mic, 60 cm |
| Whisper model | base.en (CPU) |
| Noise source and level | |

| Speaker · condition | n | accuracy | wrong | rejected | stop OK | FA/10 min | risky | STT p50 |
|---|---|---|---|---|---|---|---|---|

Risky errors and wrong intents (transcript → intent):
Changes made after the trials:
```

## Privacy

- Audio stays in memory: the microphone buffer is transcribed and discarded. No recording.
- The JSONL keeps transcripts, labels and conditions. Delete it after copying the numbers
  (`data/experiments/voice_trials.jsonl`), and before giving the laptop to anyone else.
