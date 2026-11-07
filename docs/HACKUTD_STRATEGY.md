# HackUTD 2026: Zero Day strategy

## 1. Verified facts (https://zeroday.hackutd.co/, fetched 2026-10-31)

- **When / where:** November 7-8, 2026, ECSW building, UT Dallas. Free for hackers.
- **Tracks:** *not announced.* The page says: "This year's challenge tracks will be
  announced soon." It also says teams may "Enter up to two."
- **Schedule:** "The event schedule has not been published yet." Times will be Central.
- **Submission:** Devpost is listed under resources; no rules published yet.
- **Hardware:** no hardware rules or hardware lab information on the page; the description
  says students "build apps, hardware and more."
- **Judging criteria:** not published.

Sponsors as listed on the page:

| Tier | Sponsors |
|---|---|
| Title | T-Mobile |
| Platinum | PNC Bank, 8090 Solutions |
| Gold | Northmark Compute & Cloud, Toyota Financial Services, Winstead PC, EOG Resources, Goldman Sachs |
| Silver | State Farm, CBRE, NVIDIA, Capital One, Clockwork Labs |
| Bronze | Niantic Spatial |
| Other sponsors | Coveron, NordPass, Saily, Incogni, AT&T, Photon, NordVPN |
| Also thanking | Lovable, DeepSpace, 639Cloud, Maritime, Aperturent, Hugging Face |

(The brief listed Hugging Face and Lovable as supporters; the page puts them under "Also
thanking". Coveron, NordPass, Saily, Incogni, NordVPN, DeepSpace, 639Cloud, Maritime and
Aperturent are on the page but were not in the brief.)

## 2. Track recommendation (decision rules until tracks are published)

Re-check the site daily and on arrival. Rank announced tracks with these rules, in order:

1. **A hardware / robotics / "best hardware hack" track** → first choice. LOCKED IN's
   strongest evidence is a physical closed loop: hand detected → decision → safety check →
   robot moved, with measured latency.
2. **An AI / ML / computer-vision / agents track** → strong second. Real-time hand
   landmarks, an explicit state machine, an explainable policy, deterministic voice intents,
   and an LLM that is fenced off from motion with a grounding guard.
3. **A health / wellness / education / productivity / human-centred track** → good fit
   (McLockin Jr. won in MakeMIT's healthcare category). Lead with session metrics,
   no-guessing presence data, and privacy (no video stored, local-only processing).
4. **Sponsor challenges** only when the requirement is met by something we would build anyway:
   - **NVIDIA:** only if the challenge accepts CPU/edge work, or if NVIDIA hardware is
     provided on site. We have no NVIDIA GPU; nothing in the project claims CUDA.
   - **Niantic Spatial:** only if the challenge is about localisation or spatial
     understanding *and* we can show it. Our robot has no localisation (return-home is
     labelled dead reckoning), so do not enter this unless the challenge provides the tools
     and the team has time.
   - **Hugging Face:** the speech model weights (`faster-whisper base.en`) are downloaded
     from the Hugging Face Hub, a real but modest use. Mention it only if a challenge asks.
   - **T-Mobile, AT&T** (connectivity): no natural fit; the robot runs on its own offline
     Wi-Fi by design. Do not force it.
   - Finance and law sponsors (PNC, Goldman Sachs, Capital One, Toyota Financial, State Farm,
     Winstead): no fit unless a general "best overall/most creative" prize is attached.

Do not add sponsor APIs for optics. Two strong entries beat four thin ones.

## 3. What judges should see (mapped to typical criteria)

| Likely criterion | Evidence in LOCKED IN |
|---|---|
| Technical difficulty | perception → policy → safety → robot loop; protocol reverse-engineered from firmware source; firmware model used for protocol tests; Whisper isolated in a worker process |
| Completeness | runs end to end in three labelled modes; 209 automated tests; one-click switch between simulated and real robot |
| Design | dashboard with timer-first hierarchy, live loop trace, "why it acted" panel |
| Usefulness | the robot only acts on a confirmed reach, stays quiet when you are focused, and "give me my phone" always works |
| Presentation | 90-second script ([DEMO_SCRIPT.md](DEMO_SCRIPT.md)) with fallback modes; measured numbers in [PERFORMANCE.md](PERFORMANCE.md) |

## 4. Competitor: McLockin Jr. (MakeMIT x Harvard 2026)

Sources: Devpost https://devpost.com/software/desk-companion (fetched 2026-10-31) and Olin
College news, March 9, 2026,
https://olin.edu/articles/news-healthcare-ai-four-olin-teams-win-prizes-makemit-x-harvard-hackathon.

**Confirmed by the Olin article:** Healthcare track, 2nd place (Devpost shows "Winner:
Healthcare"). An integrated **ultrasonic sensor** detects when you reach for your phone and
the companion "rolls back twice before letting you take it". Placing the phone starts a
**Pomodoro timer and to-do list**; the phone **charges wirelessly**. A **YOLO-based vision
model** with one-time calibration tracks focus; 10+ seconds unfocused triggers an
**Expressive cloud voice** (now Fish Audio, docs/VOICE_PERSONALITY.md) reminder. **YOLO Pose** posture monitoring. A break reminder after
20+ minutes of screen time.

**From Devpost:** "uses sensors to monitor light exposure and your focus"; "drives around and
talks to you"; CAD, 3D printing, Python "with some AI", a lot of soldering; built with `cad`,
`python`; a six-person team.

Where they are ahead, honestly: a custom 3D-printed body, wireless charging, posture
detection, light sensing, and a fully custom mechanical build. Our chassis is a kit.

Where LOCKED IN should beat them (only claims we can demonstrate):

| Area | McLockin Jr. (as reported) | LOCKED IN |
|---|---|---|
| Reach detection | ultrasonic proximity | 21-point hand landmarks, calibrated zone, approach speed + dwell, cooldown, measurable precision/recall |
| Robot reaction | scripted "rolls back twice" | policy decision with a rule trace, safety-gated bounded retreat, explicit 11-state machine |
| Voice | one-way reminders | two-way: commands, questions with answers grounded in recorded data, "give me my phone" always honoured |
| Safety | not described | arming, e-stop latch, profiles, watchdogs, autonomy locked until device stop is measured |
| Evidence | not described | latency per stage on screen, 209 tests, benchmark script, honest SIM labels |

## 5. Unknowns to confirm on site

- Published tracks and whether a project may enter one company challenge plus one general track.
- Judging format (table expo vs. stage), demo length, and whether a moving robot is allowed on
  the expo floor (space, floor vs. table).
- Hardware rules: power outlets, Wi-Fi channel congestion (the car runs its own 2.4 GHz AP),
  any rules about robots that move.
- Whether projects must be started at the event: confirm the rules on pre-built work and
  disclose what was built beforehand.
