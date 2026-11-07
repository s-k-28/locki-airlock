"""End-to-end demo check through the production app: microphone -> Whisper -> intent ->
personality -> speaker -> dashboard state, offline. Plays sound out loud; run it at the laptop.

    .venv/bin/python scripts/demo_check.py --robot-output "JLab GO Party"
    .venv/bin/python scripts/demo_check.py --robot-output "JLab GO Party" --user-output "MacBook Air Speakers"

1. Starts the real backend (uvicorn app.main:app) with what `make hardware` sets: Zeus profile,
   ESP32 camera, offline mode, voice on in push-to-talk, the robot's voice on --robot-output.
   Its data folder is a scratch copy (your saved settings + a link to the voice cache), every
   Fish Audio key variable is blanked, and it runs in a macOS sandbox that refuses every
   connection except to this machine (no internet, no DNS).
2. For each command: push-to-talk down over HTTP (what the dashboard button does), the command
   spoken by a macOS voice on --user-output (default: the Mac's own speakers, next to the
   microphone, like a person at the laptop), push-to-talk up. Then /api/state is read: the
   recognised turn, its intent, the state change, the reply, its real voice source and the
   backend's measured "end of speech -> playback started".
3. The microphone is also recorded here, separately. The reply is found in that recording by
   correlating it with the exact clip that was played, searching only between the end of that
   command and the start of the next: "speech finished -> robot audible", measured at the mic.
   Afterwards Whisper checks that the reply really came out of the speaker.

The voice saying the commands is synthetic: this proves the real microphone, recogniser, app and
speaker path, not recognition accuracy for a person's voice. Results: data/voice_tests/
demo-check-<time>.json, and the microphone recording next to it (.wav) to listen to.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "scripts"))

import httpx  # noqa: E402
import numpy as np  # noqa: E402
import voice_loop_check as vlc  # noqa: E402

SANDBOX = (
    '(version 1)(allow default)(deny network-outbound)(allow network-outbound (remote ip "localhost:*"))'
    "(allow network-outbound (remote unix-socket))"
    '(deny network-outbound (remote unix-socket (path-literal "/private/var/run/mDNSResponder")))'
)
HW_ENV = {
    "LOCKEDIN_ROBOT_DRIVER": "zeus",
    "LOCKEDIN_ZEUS_AUTOCONNECT": "true",
    "LOCKEDIN_ZEUS_HOST": "192.168.4.1",
    "LOCKEDIN_ZEUS_PORT": "8765",
    "LOCKEDIN_ZEUS_CAMERA_URL": "http://192.168.4.1:9000/mjpg",
    "LOCKEDIN_CAMERA_SOURCE": "zeus",
    "LOCKEDIN_VISION_AUTOSTART": "true",
    "LOCKEDIN_OFFLINE": "true",
    "LOCKEDIN_VOICE_AUTOSTART": "true",
    "LOCKEDIN_LLM_PROVIDER": "none",
    "FISH_AUDIO_API_KEY": "",
    "LOCKEDIN_FISH_AUDIO_API_KEY": "",
    "FISH_API_KEY": "",
}


def check_personality(b, a):
    return a["voice"]["personality"]["mode"] == "roast", f"personality {a['voice']['personality']['mode']}"


def check_session(b, a):
    s = a["session"]
    return bool(s and s.get("active")), f"session active={bool(s and s.get('active'))}"


def check_25(b, a):
    s = a["session"] or {}
    return bool(s.get("active")) and abs(float(s.get("planned_duration_s", 0)) - 1500) < 1, f"session planned {s.get('planned_duration_s')} s"


def check_paused(b, a):
    s = a["session"] or {}
    return bool(s.get("paused")), f"paused={s.get('paused')} state {a['state']['name']}"


def check_resumed(b, a):
    s = a["session"] or {}
    return bool(s.get("active")) and not s.get("paused"), f"paused={s.get('paused')} state {a['state']['name']}"


def check_muted(b, a):
    return bool(a["voice"]["tts"]["muted"]), f"muted={a['voice']['tts']['muted']}"


def check_unmuted(b, a):
    return not a["voice"]["tts"]["muted"], f"muted={a['voice']['tts']['muted']}"


def check_stop(b, a):
    left = a["safety"].get("motion_suppressed_s")
    return left is not None, f"motion suppressed {left} s; robot stop: {a['robot'].get('hardware_stop') or 'no robot connected'}"


def check_released(b, a):
    # With no robot connected the state machine stays DISCONNECTED by design (nothing to hand
    # back); the session must still end.
    robot_up = bool((a["robot"].get("status") or {}).get("connected"))
    state = a["state"]["name"]
    ok_state = state == "RELEASED" or (state == "DISCONNECTED" and not robot_up)
    return ok_state and not a["session"], f"state {state} (robot {'connected' if robot_up else 'not connected'}), session {'ended' if not a['session'] else 'still active'}"


def check_estop(b, a):
    return bool(a["safety"]["estop_latched"]) and a["state"]["name"] == "EMERGENCY_STOP", f"estop={a['safety']['estop_latched']} state {a['state']['name']}"


def check_time_answer(b, a):
    turn = a["voice"]["turns"][0] if a["voice"]["turns"] else {}
    return bool(turn.get("replies")), "answered" if turn.get("replies") else "no answer"


STEPS = [
    ("Roast me.", "set_personality", check_personality),
    ("Start my focus session.", "start_session", check_session),
    ("How much time do I have left?", "time_left", check_time_answer),
    ("Pause my session.", "pause", check_paused),
    ("Resume.", "resume", check_resumed),
    ("Stop talking.", "mute", check_muted),
    ("You can talk again.", "unmute", check_unmuted),
    ("Stop.", "stop_motion", check_stop),
    ("Give me my phone back.", "release", check_released),
    ("@reset", None, None),
    ("Lock me in for 25 minutes.", "start_session", check_25),
    ("Emergency stop.", "emergency_stop", check_estop),
]


def prepare_data(robot_output: str) -> Path:
    from app.config import RuntimeSettings

    data = Path(tempfile.mkdtemp(prefix="lockedin-demo-"))
    src = ROOT / "data" / "runtime_settings.json"
    rs = RuntimeSettings.model_validate_json(src.read_text()) if src.exists() else RuntimeSettings()
    rs.voice.listen_mode = "push_to_talk"
    rs.voice.muted = False
    rs.voice.output_device = robot_output
    rs.coach.personality, rs.coach.profanity, rs.coach.tone = "roast", "off", "normal"
    (data / "runtime_settings.json").write_text(rs.model_dump_json(indent=2))
    (data / "tts_cache").symlink_to(ROOT / "data" / "tts_cache")
    return data


def reply_clip(reply: dict, source: str | None, data: Path):
    """The exact audio the backend played for this reply, to find it in the recording."""
    from app.config import RuntimeSettingsStore
    from app.voice.audio_cache import AudioCache
    from app.voice.fish_audio import FishAudioVoice
    from app.voice.tts import CachedSystemTTS, SpeechRequest
    from app.voice.usage import UsageLedger

    if source == "Cached Fish Audio":
        p = RuntimeSettingsStore(data / "runtime_settings.json").load().voice_profile
        fish = FishAudioVoice(None, AudioCache(data / "tts_cache" / "fish_audio"), UsageLedger(data / "u.json"))
        clip = fish.cached(fish.text_for(SpeechRequest(reply["text"], reply.get("cue")), p), p)
        if clip is not None:
            return clip
    sys_tts = CachedSystemTTS(data / "tts_cache")
    return sys_tts._load(reply["text"]) or sys_tts.render(reply["text"])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--robot-output", required=True, help="the robot's speaker, e.g. 'JLab GO Party'")
    ap.add_argument("--user-output", default="MacBook Air Speakers", help="where the stand-in user voice plays")
    ap.add_argument("--port", type=int, default=8078)
    ap.add_argument("--no-sandbox", action="store_true")
    a = ap.parse_args()
    import sounddevice as sd

    data = prepare_data(a.robot_output)
    env = {**os.environ, **HW_ENV, "LOCKEDIN_DATA_DIR": str(data), "LOCKEDIN_AUDIO_OUTPUT": a.robot_output, "LOCKEDIN_PORT": str(a.port)}
    cmd = [str(BACKEND / ".venv" / "bin" / "python"), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(a.port)]
    sandboxed = not a.no_sandbox and shutil.which("sandbox-exec") is not None
    if sandboxed:
        cmd = ["sandbox-exec", "-p", SANDBOX, *cmd]
    log = open(data / "backend.log", "w")
    t_launch = time.monotonic()
    proc = subprocess.Popen(cmd, cwd=BACKEND, env=env, stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{a.port}"
    client = httpx.Client(base_url=base, timeout=10)
    results: dict = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "robot_output": a.robot_output, "user_output": a.user_output,
                     "sandboxed_offline": sandboxed, "steps": []}
    rec_ctx = None
    try:
        for _ in range(300):  # backend + Whisper model ready
            try:
                st = client.get("/api/state").json()
                if st["voice"]["input"].get("running") and (st["voice"]["input"].get("stt") or {}).get("state") == "ready":
                    break
            except Exception:
                pass
            time.sleep(0.2)
        else:
            raise SystemExit("backend or speech recognition did not come up; see " + str(data / "backend.log"))
        results["startup_s"] = round(time.monotonic() - t_launch, 1)
        st = client.get("/api/state").json()
        results["startup_state"] = {
            "mode": st["mode"]["label"], "offline": st["offline"]["enabled"], "armed": st["safety"]["armed"],
            "motion_permitted": st["robot"]["gate"]["motion_permitted"], "camera": st["vision"].get("source_label"),
            "voice": st["voice"]["input"]["mode"], "output": st["voice"]["output"]["label"],
            "fish_key_configured": st["voice"]["cloud"]["configured"],
        }
        print(f"Backend up in {results['startup_s']} s (sandboxed offline: {sandboxed}): {results['startup_state']}")
        rec_ctx = vlc.Recorder(None)
        rec_ctx.__enter__()
        time.sleep(0.5)
        for text, expected, check in STEPS:
            if text == "@reset":
                client.post("/api/reset")
                time.sleep(0.5)
                continue
            before = client.get("/api/state").json()
            seen = {t["id"] for t in before["voice"]["turns"]}
            client.post("/api/voice/ptt", json={"action": "start"})
            time.sleep(0.15)
            pcm, sr = vlc.spoken(text)
            sd.play(np.clip(pcm.astype(np.float32) * 0.9, -32768, 32767).astype(np.int16), sr, device=a.user_output)
            sd.wait()
            t_spoken = time.monotonic()
            time.sleep(0.15)
            client.post("/api/voice/ptt", json={"action": "stop"})
            turn, after = None, None
            end = time.monotonic() + 12
            while time.monotonic() < end:
                after = client.get("/api/state").json()
                new = [t for t in after["voice"]["turns"] if t["id"] not in seen]
                if new:
                    turn = new[0]
                    spoken_done = after["voice"]["state"] != "speaking" and (turn.get("reply_to_audio_ms") is not None or not turn.get("replies"))
                    if spoken_done:
                        break
                time.sleep(0.1)
            time.sleep(0.5)
            t_end = time.monotonic()
            after = client.get("/api/state").json()
            if turn is not None:
                turn = next((t for t in after["voice"]["turns"] if t["id"] == turn["id"]), turn)
            got = (turn or {}).get("intent") or {}
            ok_state, state_note = check(before, after) if check else (True, "")
            row = {
                "said": text, "expected": expected, "heard": (turn or {}).get("text"), "intent": got.get("intent"),
                "confidence": got.get("confidence"), "action": (turn or {}).get("action"), "state": state_note,
                "state_ok": ok_state, "reply": [r["text"] for r in (turn or {}).get("replies", [])],
                "reply_source": (turn or {}).get("reply_source"), "stt_ms": (turn or {}).get("stt_ms"),
                "backend_speech_end_to_playback_ms": (turn or {}).get("reply_to_audio_ms"),
                "output_latency_ms": (turn or {}).get("output_latency_ms"), "t_spoken": t_spoken, "t_end": t_end,
                "_replies": (turn or {}).get("replies", []),
            }
            row["ok"] = bool(turn) and got.get("intent") == expected and ok_state
            results["steps"].append(row)
            print(f"{'PASS' if row['ok'] else 'FAIL'} “{text}” → heard “{row['heard']}” → {row['intent']} | {state_note} | "
                  f"reply {row['reply'][:1]} [{row['reply_source']}] backend {row['backend_speech_end_to_playback_ms']} ms (STT {row['stt_ms']} ms)")
        time.sleep(1.0)
    finally:
        audio = None
        if rec_ctx is not None:
            rec_ctx.__exit__(None, None, None)
            audio, t_first = rec_ctx.audio(), rec_ctx.t_first
        t_stop = time.monotonic()
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=20)
            results["shutdown_s"] = round(time.monotonic() - t_stop, 1)
        except subprocess.TimeoutExpired:
            proc.kill()
            results["shutdown_s"] = "killed after 20 s"
        log.close()
        client.close()
    blocked = [ln.strip() for ln in (data / "backend.log").read_text(errors="ignore").splitlines() if "Operation not permitted" in ln]
    results["backend_network_denials_logged"] = len(blocked)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = ROOT / "data" / "voice_tests"
    out.mkdir(parents=True, exist_ok=True)
    # Find each reply in the microphone recording and check it with Whisper.
    if audio is not None and t_first is not None:
        import wave

        with wave.open(str(out / f"demo-check-{stamp}.wav"), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(vlc.SR)
            w.writeframes(np.clip(audio * 32767, -32768, 32767).astype(np.int16).tobytes())
        results["recording"] = f"data/voice_tests/demo-check-{stamp}.wav"
        tr = vlc.transcriber(__import__("app.config", fromlist=["Settings"]).Settings())
        try:
            for row in results["steps"]:
                reps = row.pop("_replies")
                t_spoken, t_end = row.pop("t_spoken"), row.pop("t_end")
                if not reps or row["reply_source"] is None:
                    continue
                (pcm, sr) = reply_clip(reps[0], row["reply_source"], data)
                clip = vlc.to_16k(pcm, sr)
                i0 = max(0, int((t_spoken - t_first) * vlc.SR))
                window = audio[i0 : max(i0, int((t_end - t_first) * vlc.SR))]  # this command's turn only
                row["mic_window_s"] = round(len(window) / vlc.SR, 1)
                if len(window) < len(clip):
                    row["mic_note"] = "reply longer than the time left in this step's recording"
                    continue
                at, peak = vlc.onset_s(window, clip)
                seg = window[int(at * vlc.SR) : int(at * vlc.SR) + len(clip) + int(0.3 * vlc.SR)]
                heard = ((tr.transcribe(seg, timeout_s=30) or {}).get("text") or "").strip()
                from app.voice.tts import echo_similarity

                row["mic_speech_end_to_audible_ms"] = round(at * 1000) if peak >= 0.15 else None
                row["mic_correlation"] = round(peak, 2)
                row["mic_heard_reply"] = heard
                row["mic_reply_match"] = round(echo_similarity(heard, reps[0]["text"]), 2) if heard else 0.0
        finally:
            tr.close()
    else:
        for row in results["steps"]:
            row.pop("_replies", None)
            row.pop("t_spoken", None)
            row.pop("t_end", None)
    ok = sum(r["ok"] for r in results["steps"])
    results["summary"] = {"passed": ok, "total": len(results["steps"])}
    path = out / f"demo-check-{stamp}.json"
    path.write_text(json.dumps(results, indent=2))
    print(f"\n{ok}/{len(results['steps'])} steps passed. Shutdown {results.get('shutdown_s')} s. Saved {path}")
    for r in results["steps"]:
        print(f"  {r['said']:32s} audible after {r.get('mic_speech_end_to_audible_ms')} ms (corr {r.get('mic_correlation')}), mic heard “{r.get('mic_heard_reply')}” match {r.get('mic_reply_match')}")
    shutil.rmtree(data, ignore_errors=True)
    return 0 if ok == len(results["steps"]) else 1


if __name__ == "__main__":
    sys.exit(main())
