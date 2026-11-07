# 90-second demo

Roles: **Driver** talks and reaches for the phone. **Operator** watches the dashboard with a
finger near E-STOP and narrates the technical part.

## Before judges arrive (checklist)

- [ ] `make demo` running (backend + built dashboard on http://127.0.0.1:8077), browser full screen.
- [ ] Robot on the floor or a large table with ≥ 60 cm clear behind it, edge barrier if on a table.
- [ ] Phone secured (Velcro + cable ties); compass calibrated with the phone mounted.
- [ ] Laptop joined to `Zeus_Car` Wi-Fi; robot panel shows **Live · Zeus Car**, link `connected`.
- [ ] R4 and R5 passed today; autonomy armed with the clear-path box ticked.
- [ ] Camera live; phone zone calibrated around the phone (Vision → Zone); **Phone zone → Follows
      robot** shows "Robot tracked"; one test reach; one retreat to check the zone moves with it.
- [ ] Mic started, push-to-talk; robot voice unmuted; volume checked.
- [ ] Personality + Voice: **Judge demo** for judges (clean) or Roast with profanity off;
      voice preset chosen; **Offline cache shows every line cached** (generated while online,
      before joining the robot's Wi-Fi). Speech history should show "Cached Fish Audio".
- [ ] Fresh backend start so the timeline only shows the demo (`make demo` again).

## Script

| Time | What happens | What the dashboard shows |
|---|---|---|
| 0:00 | Driver (holding Space): "**LOCKED IN, I have 25 minutes to finish calculus. Let's go.**" | transcript, `start_session 0.9x`, timer 25:00 counting, state ACTIVE |
| 0:05 | Robot: "Twenty-five minutes. Let's see if you actually do it." (one of its lines) | |
| 0:12 | Driver reaches for the phone | camera: zone turns amber, then magenta |
| 0:13 | Robot backs away ~0.6 s and says "**Bro. We literally just started.**" | banner PHONE REACH DETECTED · RETREAT EXECUTED; loop trace lights up with ms per stage |
| 0:25 | Driver: "**I finished two problems.**" then "**How am I doing?**" | Done 2; reply quotes real elapsed time, problems and reaches |
| 0:40 | Driver: "**Okay, give my phone back.**" | robot stops, state RELEASED, session summary saved |
| 0:45-1:30 | Operator: point at the state rail, "Why it acted" rules, the loop trace (detect → decide → safety → sent → ack), Robot wire tab with the exact JSON frames, and the safety gates (disarmed by default, autonomy locked until device stop measured) | |

Say only measured numbers (from [PERFORMANCE.md](PERFORMANCE.md)) and say which ones are
software-only.

## Milestone 5: the conversational demo (robot stationary)

The robot does not move in this version: motor power is unverified and the firmware patch is
not flashed (SAFETY.md §7). The demo is the companion: it understands what the Driver says,
watches the session through the camera, speaks up on its own when a reach is confirmed, and
remembers. Everything runs on the laptop, offline.

**Extra checklist:** Low Power Mode **off** (it cuts the model to a quarter of its speed);
quit heavy apps (the model needs ~3 GB of memory); `make offline-check` shows READY with the
"conversation" probe OK; macOS output **unmuted**; push-to-talk.

Lines marked *e.g.* are examples: the robot writes its own words each time (MILESTONE5_EVALUATION.md
has real transcripts). What is fixed is what it does.

| Time | Driver / what happens | Robot | Dashboard (Companion panel unless noted) |
|---|---|---|---|
| 0:00 | (Space) "I have a calculus test tomorrow and I keep checking my phone. Give me like forty minutes." | *e.g.* "Calculus tomorrow? Then the phone is mine for forty minutes. Go." (local Kokoro voice) | timer 40:00; **goal: calculus test** (you said); action `start_session` executed; reply "written now" with its timings |
| 0:15 | reaches for the phone (camera confirms) | a reaction about calculus, prepared in the background so it plays at once | decision **spoke** · confirmed reach #1 · "prepared ahead"; physical: no motion (wheels locked) |
| 0:25 | "I just need five more minutes, my friend texted me." | teases, or gives it back and says so: the words always match the action | reply + action (none or `release_phone`) |
| 0:35 | "Why do you keep doing that?" | explains from the record: the reach it saw, the test tomorrow | observed vs you said, side by side |
| 0:45 | "Can you be quiet for a bit?" | short acknowledgement | bounds: **quiet mode**, minutes left |
| 0:50 | reaches again | silence | decision **silent** · quiet mode: noted, not said |
| 1:00 | "Wait, what was I studying for again?" | answers from memory | conversation + goal |
| 1:10 | "Give me my phone back." | fixed line, instant (cached Fish Audio voice) | parser path; state RELEASED |
| 1:15-1:30 | Operator: everything ran on this laptop (speech, model, voice) with no internet; the model can only *ask* (typed actions, safety decides); show "why it spoke or stayed quiet" and an action it was refused | | |

If the model misbehaves or is slow: Settings → Conversation → switch off "Write replies with
the local model": the robot is back to the Milestone 4 line library, same commands.

## Fallbacks (switch in seconds, always labelled)

| Mode | When | How |
|---|---|---|
| **A** full physical | everything works | — |
| **B** live camera + voice, simulated robot | robot dead, battery low, Wi-Fi trouble | Robot panel → **Sim** → Connect. Banner and events show SIMULATED; the top-down map shows the simulated retreat |
| **C** full simulation | camera also unavailable | Closed loop panel → **Reach** / **Demo script** (labelled synthetic events) |

Say the mode out loud when switching ("the car's battery died, this is the same software with
the simulated robot"). Never present B or C as physical.

**Demo script button (rehearsal, Mode C):** safe to press again and again. Before each run it
ends any running session, puts the simulated robot back home (otherwise the new session
would start in HOLDING and never retreat), arms, and waits out the retreat cooldown left by
the previous run; the timeline shows each of these as a `Demo ·` row. It refuses, saying
why, during an e-stop, while the robot is moving, or when a **real** robot is away from home
(put it back and press Set home). `make soak` runs it back to back with invariant checks.

## If something goes wrong mid-demo

- Robot moving unexpectedly: E-STOP (top right). Reset clears the latch; the robot stays
  disarmed until re-armed.
- Voice not recognised: type the same sentence in the voice panel (same pipeline).
- False reach trigger: hover the timeline row → "false trigger"; after two the robot switches to
  voice-only on its own (adaptation rule A3), which is itself worth showing.
- Backend crash: the car stops when the socket closes (stock firmware); restart with `make demo`.
