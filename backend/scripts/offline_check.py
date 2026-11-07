"""Offline readiness check. Run it on home Wi-Fi, before joining the robot's Zeus_Car Wi-Fi
(which has no internet):

    make offline-check                 # or: .venv/bin/python scripts/offline_check.py
    .venv/bin/python scripts/offline_check.py --json

It looks only at local files and devices, plus two short connection attempts to the robot's
address. Nothing is downloaded, no paid API is called, and no key is printed. Exit code 1 when
something the demo needs is missing.

Checks: Python packages, model files, the built dashboard (and that it loads nothing from the
internet), the Fish Audio voice pack in the cache, the macOS fallback voice, audio devices, the
hardware settings `make hardware` will use, port 8077, the robot's address, and an in-process
start of the backend in offline mode with every non-local connection blocked and counted.
"""

from __future__ import annotations

import argparse
import importlib
import json
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))

REQUIRED_MODULES = [
    ("fastapi", "backend"),
    ("uvicorn", "backend"),
    ("pydantic_settings", "settings"),
    ("numpy", "backend"),
    ("websockets", "robot link"),
    ("httpx", "voice client"),
    ("cv2", "camera"),
    ("mediapipe", "hand / face detection"),
    ("sounddevice", "microphone and speaker"),
    ("faster_whisper", "speech recognition (fallback)"),
    ("pywhispercpp", "speech recognition (whisper.cpp)"),
    ("onnxruntime", "Silero VAD and the Kokoro voice"),
    ("kokoro_onnx", "local neural voice"),
    ("mlx_lm", "local language model"),
]
MODEL_FILES = ["hand_landmarker.task", "blaze_face_short_range.tflite"]
WHISPER_FILES = ["model.bin", "config.json", "tokenizer.json", "vocabulary.txt"]
LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "192.168.4.1", "www.w3.org"}
EXTERNAL_PATTERNS = [
    re.compile(r"""<(?:script|link|img|iframe|source)\b[^>]*\b(?:src|href)\s*=\s*["'](https?://[^"']+)""", re.I),
    re.compile(r"""url\(\s*["']?(https?://[^"')\s]+)""", re.I),
    re.compile(r"""@import\s+(?:url\()?\s*["']?(https?://[^"');\s]+)""", re.I),
    re.compile(r"""\bimport\(\s*["'](https?://[^"']+)""", re.I),
    re.compile(r"""\bimportScripts\(\s*["'](https?://[^"']+)""", re.I),
]


@dataclass
class Check:
    group: str
    name: str
    status: str  # ok | warn | fail | info
    detail: str
    fix: str = ""


# ------------------------------------------------------------------ python + models
def check_python() -> list[Check]:
    out = []
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    out.append(Check("python", "interpreter", "ok" if in_venv else "warn", f"{sys.version.split()[0]} at {sys.executable}",
                     "" if in_venv else "run with backend/.venv/bin/python (what make hardware uses)"))
    for mod, why in REQUIRED_MODULES:
        try:
            m = importlib.import_module(mod)
            out.append(Check("python", mod, "ok", f"{getattr(m, '__version__', 'installed')} ({why})"))
        except Exception as exc:
            out.append(Check("python", mod, "fail", f"cannot import ({why}): {exc}", "make setup (needs internet once)"))
    return out


def check_power() -> list[Check]:
    """macOS Low Power Mode throttles the GPU: measured 8 vs 37 tokens/s for the local model."""
    try:
        out = subprocess.run(["pmset", "-g"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return []
    mode = next((ln.split()[-1] for ln in out.splitlines() if "lowpowermode" in ln), None)
    if mode is None:
        return []
    if mode == "1":
        return [Check("machine", "Low Power Mode", "warn", "ON: the local language model runs about 4x slower (8 vs 37 tokens/s, docs/MODELS.md)",
                      "System Settings > Battery > Low Power Mode: Never")]
    return [Check("machine", "Low Power Mode", "ok", "off")]


def check_local_ai(settings) -> list[Check]:
    """Milestone 5 weights: whisper.cpp, the local language model, Kokoro, Silero VAD."""
    m = settings.models_dir
    out = []
    if settings.stt_engine == "whisper.cpp":
        g = m / "whispercpp" / f"ggml-{settings.whisper_model}.bin"
        out.append(Check("models", f"whisper.cpp {settings.whisper_model}", "ok" if g.is_file() else "fail",
                         f"{g.stat().st_size / 1e6:.0f} MB" if g.is_file() else f"missing: {g}", "" if g.is_file() else "make models"))
    if settings.llm_provider == "local":
        d = m / "llm" / settings.local_llm_model
        need = ["config.json", "model.safetensors", "tokenizer.json"]
        missing = [f for f in need if not (d / f).is_file()]
        size = (d / "model.safetensors").stat().st_size / 1e9 if (d / "model.safetensors").is_file() else 0
        out.append(Check("models", f"language model {settings.local_llm_model}", "ok" if not missing else "fail",
                         f"{size:.2f} GB in {d}" if not missing else f"missing {', '.join(missing)} in {d}",
                         "" if not missing else "make models (2.3 GB, home Wi-Fi)"))
    k = m / "kokoro"
    kmissing = [f for f in (settings.kokoro_model, "voices-v1.0.bin") if not (k / f).is_file()]
    out.append(Check("models", f"Kokoro voice {settings.kokoro_model}", "ok" if not kmissing else "warn",
                     f"in {k}" if not kmissing else f"missing {', '.join(kmissing)}: generated replies would use the macOS voice",
                     "" if not kmissing else "make models"))
    from app.voice.input import silero_model_path

    vad = silero_model_path(m)
    out.append(Check("models", "Silero VAD", "ok" if vad else "warn", str(vad) if vad else "missing: hands-free uses the energy detector",
                     "" if vad else "make models"))
    return out


def check_models(models_dir: Path, whisper_model: str) -> list[Check]:
    out = []
    for name in MODEL_FILES:
        p = models_dir / name
        ok = p.is_file() and p.stat().st_size > 10_000
        out.append(Check("models", name, "ok" if ok else "fail", f"{p.stat().st_size / 1e6:.1f} MB" if ok else f"missing: {p}",
                         "" if ok else "make models (needs internet once)"))
    wdir = models_dir / "whisper" / whisper_model
    missing = [f for f in WHISPER_FILES if not (wdir / f).is_file()]
    out.append(Check("models", f"whisper {whisper_model}", "ok" if not missing else "fail",
                     f"local copy in {wdir}" if not missing else f"missing {', '.join(missing)} in {wdir}",
                     "" if not missing else "make models (otherwise speech recognition would try to download it)"))
    return out


# ------------------------------------------------------------------ dashboard
def scan_external_refs(dist: Path) -> list[str]:
    """Resources the built dashboard would fetch from the internet (fonts, scripts, styles)."""
    found = []
    for path in sorted(dist.rglob("*")):
        if path.suffix.lower() not in (".html", ".css", ".js") or not path.is_file():
            continue
        text = path.read_text(errors="ignore")
        for pat in EXTERNAL_PATTERNS:
            for url in pat.findall(text):
                host = (urlsplit(url).hostname or "").lower()
                if host not in LOCAL_HOSTS:
                    found.append(f"{path.relative_to(dist)}: {url[:100]}")
    return found


def check_dashboard(frontend: Path) -> list[Check]:
    out = []
    dist = frontend / "dist"
    index = dist / "index.html"
    if not index.is_file():
        return [Check("dashboard", "build", "fail", f"no {index}", "make build (works offline once node_modules is installed)")]
    sources = [p for p in (frontend / "src").rglob("*") if p.is_file()] + [frontend / "index.html", frontend / "package.json"]
    newest = max((p.stat().st_mtime for p in sources if p.exists()), default=0)
    stale = newest > index.stat().st_mtime
    out.append(Check("dashboard", "build", "warn" if stale else "ok",
                     "older than the dashboard source" if stale else f"built {index.stat().st_mtime:.0f} (newer than the source)",
                     "make build (make hardware rebuilds it when stale)" if stale else ""))
    refs = scan_external_refs(dist)
    out.append(Check("dashboard", "no internet assets", "fail" if refs else "ok",
                     "; ".join(refs[:3]) if refs else "fonts, scripts and styles are all bundled",
                     "bundle the asset (fonts come from @fontsource packages)" if refs else ""))
    fonts = list(dist.rglob("*.woff2"))
    out.append(Check("dashboard", "fonts", "ok" if fonts else "warn", f"{len(fonts)} bundled font files" if fonts else "no bundled fonts found"))
    nm = frontend / "node_modules"
    can_rebuild = (nm / ".bin" / "vite").exists() and (nm / ".bin" / "tsc").exists()
    out.append(Check("dashboard", "rebuild offline", "ok" if can_rebuild else "warn",
                     "node_modules has vite + tsc" if can_rebuild else "node_modules missing: the dashboard cannot be rebuilt offline",
                     "" if can_rebuild else "cd frontend && npm install (needs internet)"))
    return out


# ------------------------------------------------------------------ voice + audio
def check_voice(settings) -> list[Check]:
    from app.runtime import LockedInRuntime
    from app.voice.tts import NullTTS

    out = []
    rt = LockedInRuntime(settings.model_copy(update={"robot_driver": "mock", "mock_autoconnect": False}),
                         tts_engine=NullTTS(), enable_vision=False, enable_voice=False, persist_settings=True)
    try:
        cfg = rt.voice_config()
        prof = rt.rs.voice_profile
        if prof.provider == "fish_audio":
            pack = cfg["cache"]["pack"]
            full = pack["lines"] > 0 and pack["cached"] == pack["lines"]
            out.append(Check("voice", "Fish Audio voice pack", "ok" if full else ("warn" if pack["cached"] else "fail"),
                             f"{pack['cached']}/{pack['lines']} clips cached for voice {prof.reference_id[:8]}..., model {prof.model}",
                             "" if full else "Personality + Voice > Offline cache > Plan: voice pack (online, approved per batch)"))
            cur = cfg["cache"]
            out.append(Check("voice", "other personality lines", "info", f"{cur['cached']} cached, {cur['missing']} would use the local voice"))
            out.append(Check("voice", "live generation", "ok" if prof.cache_behavior == "cache_only" else "info",
                             "cache only" if prof.cache_behavior == "cache_only" else "cache then live (offline mode blocks live requests anyway)"))
        else:
            out.append(Check("voice", "provider", "ok", f"{prof.provider}: no cloud voice needed"))
        out.append(check_system_voice(prof.system_voice or settings.tts_voice))
        out.extend(check_audio_devices(rt.rs.voice.output_device, rt.rs.voice.input_device))
    finally:
        rt.store.close()
    return out


def check_system_voice(voice: str | None) -> Check:
    if shutil.which("say") is None:
        return Check("voice", "macOS voice (fallback)", "fail", "`say` not found", "LOCKED IN needs macOS for the local voice")
    if not voice:
        return Check("voice", "macOS voice (fallback)", "ok", "system default voice, installed with macOS")
    try:
        listing = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, timeout=10).stdout
    except Exception as exc:
        return Check("voice", "macOS voice (fallback)", "warn", f"could not list voices: {exc}")
    names = {line.split("  ")[0].strip() for line in listing.splitlines() if line.strip()}
    ok = voice in names
    return Check("voice", "macOS voice (fallback)", "ok" if ok else "fail", f"{voice} {'installed' if ok else 'not installed'}",
                 "" if ok else "System Settings > Accessibility > Spoken Content > System voice > Manage Voices (download while online)")


def check_audio_devices(output_device: str | None, input_device) -> list[Check]:
    try:
        import sounddevice as sd

        devices = sd.query_devices()
    except Exception as exc:
        return [Check("audio", "devices", "warn", f"cannot list audio devices: {exc}")]
    outs = [d["name"] for d in devices if d.get("max_output_channels", 0) > 0]
    ins = [d["name"] for d in devices if d.get("max_input_channels", 0) > 0]
    res = [Check("audio", "speakers", "ok" if outs else "fail", ", ".join(outs) or "no output device")]
    if output_device:
        hit = any(output_device.lower() in n.lower() for n in outs)
        res.append(Check("audio", "chosen output", "ok" if hit else "warn", f"{output_device} {'connected' if hit else 'not connected'}",
                         "" if hit else "connect it, or pick another output (lines show as text while it is missing)"))
    res.append(Check("audio", "microphones", "ok" if ins else "warn", ", ".join(ins) or "no input device"))
    try:
        import asyncio

        from app.voice.tts import system_output_volume

        vol = asyncio.run(system_output_volume())
    except Exception:
        vol = None
    if vol is not None:
        default = sd.query_devices(kind="output")["name"]
        silent = vol["muted"] or vol["volume"] == 0
        res.append(Check("audio", "output volume", "warn" if silent else "ok",
                         f"{default}: {'muted' if vol['muted'] else 'volume ' + str(vol['volume'])}",
                         "unmute it (volume keys): a muted speaker takes the audio and plays nothing" if silent else ""))
    return res


# ------------------------------------------------------------------ hardware + network
def check_hardware_config(settings) -> list[Check]:
    from app.config import RuntimeSettingsStore

    rs = RuntimeSettingsStore(settings.data_dir / "runtime_settings.json").load()
    host = rs.robot_host or settings.zeus_host
    cam = settings.zeus_camera_url or f"http://{host}:9000/mjpg"
    mc = settings.zeus_motor_commands
    return [
        Check("hardware", "robot link", "ok", f"ws://{host}:{settings.zeus_port} (driver {settings.robot_driver})"),
        Check("hardware", "camera", "ok", f"{cam} (source at start: {settings.camera_source or rs.camera_source})"),
        Check("hardware", "motor commands", "ok" if mc == "disabled" else "warn",
              "disabled: monitor link, no motor or stop frames" if mc == "disabled" else f"'{mc}' (see docs/SAFETY.md section 7)"),
        Check("hardware", "offline mode", "ok" if settings.offline else "info",
              "on" if settings.offline else "off in this shell (make hardware turns it on)"),
    ]


def tcp_reachable(host: str, port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def check_port(port: int) -> Check:
    busy = tcp_reachable("127.0.0.1", port, 0.3)
    return Check("network", f"port {port}", "warn" if busy else "ok",
                 "in use (a backend is already running?)" if busy else "free",
                 f"stop it first: lsof -nP -iTCP:{port} -sTCP:LISTEN" if busy else "")


def check_robot_network(host: str, ws_port: int, cam_url: str) -> list[Check]:
    u = urlsplit(cam_url)
    out = []
    for name, h, p in (("robot WebSocket", host, ws_port), ("robot camera", u.hostname or host, u.port or 80)):
        ok = tcp_reachable(h, p)
        out.append(Check("network", name, "ok" if ok else "info",
                         f"{h}:{p} reachable" if ok else f"{h}:{p} not reachable from this network (expected until you join Zeus_Car)"))
    return out


# ------------------------------------------------------------------ in-process offline start
class NetworkGuard:
    """Blocks and records every connection that is not to this machine (tests use it too)."""

    def __init__(self):
        self.blocked: list[str] = []
        self._orig = {}

    @staticmethod
    def _local(host) -> bool:
        h = str(host)
        return h in ("localhost", "::1", "") or h.startswith("127.") or h.startswith("/")

    def __enter__(self):
        guard = self
        self._orig["connect"] = socket.socket.connect
        self._orig["connect_ex"] = socket.socket.connect_ex
        self._orig["getaddrinfo"] = socket.getaddrinfo

        def connect(sock, address):
            host = address[0] if isinstance(address, tuple) else address
            if not guard._local(host):
                guard.blocked.append(f"connect {address}")
                raise OSError(65, f"blocked by the offline check: {address}")
            return guard._orig["connect"](sock, address)

        def connect_ex(sock, address):
            host = address[0] if isinstance(address, tuple) else address
            if not guard._local(host):
                guard.blocked.append(f"connect {address}")
                return 65
            return guard._orig["connect_ex"](sock, address)

        def getaddrinfo(host, *args, **kw):
            if host is not None and not guard._local(host) and not re.fullmatch(r"[\d.]+|[0-9a-f:]+", str(host)):
                guard.blocked.append(f"dns {host}")
                raise socket.gaierror(8, f"blocked by the offline check: {host}")
            return guard._orig["getaddrinfo"](host, *args, **kw)

        socket.socket.connect = connect
        socket.socket.connect_ex = connect_ex
        socket.getaddrinfo = getaddrinfo
        return self

    def __exit__(self, *exc):
        socket.socket.connect = self._orig["connect"]
        socket.socket.connect_ex = self._orig["connect_ex"]
        socket.getaddrinfo = self._orig["getaddrinfo"]
        return False


def offline_start(settings) -> Check:
    """Start the backend in offline mode with the network blocked and use it a little."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    with tempfile.TemporaryDirectory() as tmp, NetworkGuard() as guard:
        s = settings.model_copy(update={
            "data_dir": Path(tmp), "offline": True, "robot_driver": "mock", "mock_autoconnect": True,
            "vision_preload": False, "vision_autostart": False, "voice_autostart": False, "tts_engine": "none",
            "fish_audio_base_url": "https://api.fish.audio", "llm_provider": "none",
        })
        try:
            with TestClient(create_app(s)) as c:
                ok = c.get("/api/health").json()["ok"]
                state = c.get("/api/state").json()
                c.post("/api/session/start", json={"task": "offline check", "duration_min": 25})
                c.post("/api/session/end", json={"reason": "user"})
                assert ok and state["offline"]["enabled"]
        except Exception as exc:
            return Check("offline", "backend start", "fail", f"{type(exc).__name__}: {exc}")
    if guard.blocked:
        return Check("offline", "backend start", "fail", "tried the internet: " + "; ".join(guard.blocked[:4]))
    return Check("offline", "backend start", "ok", "served health, state and a session with all non-local connections blocked (0 attempts)")


SANDBOX = (
    '(version 1)(allow default)(deny network-outbound)(allow network-outbound (remote ip "localhost:*"))'
    "(allow network-outbound (remote unix-socket))"
    '(deny network-outbound (remote unix-socket (path-literal "/private/var/run/mDNSResponder")))'
)
SPEECH_PROBE = """
import json, sys, time, wave
import numpy as np
from pathlib import Path
from app.voice.transcription import WhisperTranscriber
t0 = time.monotonic()
tr = WhisperTranscriber(sys.argv[2], sys.argv[3], Path(sys.argv[4]), engine=sys.argv[5])
while tr.state == "loading" and time.monotonic() - t0 < 120:
    time.sleep(0.05)
load_ms = (time.monotonic() - t0) * 1000
with wave.open(sys.argv[1]) as w:
    pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
t1 = time.monotonic()
out = tr.transcribe(pcm, timeout_s=60) if tr.state == "ready" else None
print(json.dumps({"state": tr.state, "error": tr.error, "text": (out or {}).get("text"),
                  "load_ms": round(load_ms), "transcribe_ms": round((time.monotonic() - t1) * 1000)}))
"""


def offline_speech(settings) -> Check:
    """Whisper on a spoken command in a process the macOS sandbox cuts off from every network
    except this machine, as on the robot's Wi-Fi (no internet, no DNS)."""
    if shutil.which("sandbox-exec") is None or shutil.which("say") is None:
        return Check("offline", "speech recognition", "info", "skipped: needs macOS (say, sandbox-exec)")
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "command.wav"
        subprocess.run(["say", "-o", str(wav), "--file-format=WAVE", "--data-format=LEI16@16000", "--", "Lock me in for 25 minutes."],
                       check=True, capture_output=True, timeout=30)
        engine = settings.stt_engine if settings.stt_engine != "none" else "faster-whisper"
        cmd = ["sandbox-exec", "-p", SANDBOX, sys.executable, "-c", SPEECH_PROBE, str(wav),
               settings.whisper_model, settings.whisper_compute_type, str(settings.models_dir), engine]
        try:
            run = subprocess.run(cmd, cwd=BACKEND, capture_output=True, text=True, timeout=180)
            res = json.loads(run.stdout.strip().splitlines()[-1])
        except Exception as exc:
            return Check("offline", "speech recognition", "fail", f"sandboxed Whisper did not run: {type(exc).__name__}: {exc}")
    heard = (res.get("text") or "").strip()
    norm = re.sub(r"[^a-z0-9 ]", "", heard.lower())
    if res.get("state") != "ready" or not ("lock me in" in norm and "25" in norm):
        return Check("offline", "speech recognition", "fail", f"offline Whisper heard {heard!r} ({res.get('state')}: {res.get('error')})",
                     "run scripts/download_models.py on home Wi-Fi")
    return Check("offline", "speech recognition", "ok",
                 f"{engine} heard {heard!r} with the network blocked (load {res['load_ms']} ms, transcribe {res['transcribe_ms']} ms)")


CONVERSE_PROBE = """
import asyncio, json, sys, time
from pathlib import Path
from app.intelligence.actions import ActionName, parse_reply
from app.intelligence.local_llm import LocalLLM
from app.intelligence.prompting import SAMPLE_CONTEXT
from app.voice.kokoro_tts import KokoroTTS

async def main():
    t0 = time.monotonic()
    llm = LocalLLM(sys.argv[1], Path(sys.argv[2]))
    while llm.state == "loading" and time.monotonic() - t0 < 180:
        await asyncio.sleep(0.1)
    out = {"state": llm.state, "error": llm.error, "load_ms": round((time.monotonic() - t0) * 1000)}
    if llm.state == "ready":
        g = await llm.generate(SAMPLE_CONTEXT.replace("{utterance}", "I have a calculus test tomorrow, help me focus."), max_tokens=100, timeout_s=30)
        r = parse_reply(g.text, frozenset(ActionName))
        out.update(finish=g.finish, first_token_ms=g.first_token_ms, total_ms=g.total_ms, say=r.say, action=r.requested, problems=r.problems)
        k = KokoroTTS(Path(sys.argv[2]), Path(sys.argv[3]), model_file=sys.argv[4])
        if r.say and k.load():
            t1 = time.monotonic()
            clip = k.render(r.say)
            out.update(voice_ms=round((time.monotonic() - t1) * 1000), voice_s=round(len(clip[0]) / clip[1], 2))
    llm.close()
    print(json.dumps(out))

asyncio.run(main())
"""


def offline_converse(settings) -> Check:
    """The local language model writes a reply and Kokoro voices it (nothing plays), in a
    process the sandbox cuts off from every network but this machine."""
    if settings.llm_provider != "local":
        return Check("offline", "conversation", "info", f"language model provider is {settings.llm_provider!r}: line library only")
    if shutil.which("sandbox-exec") is None:
        return Check("offline", "conversation", "info", "skipped: needs macOS sandbox-exec")
    with tempfile.TemporaryDirectory() as tmp:
        cmd = ["sandbox-exec", "-p", SANDBOX, sys.executable, "-c", CONVERSE_PROBE, settings.local_llm_model, str(settings.models_dir), tmp, settings.kokoro_model]
        try:
            run = subprocess.run(cmd, cwd=BACKEND, capture_output=True, text=True, timeout=300)
            res = json.loads(run.stdout.strip().splitlines()[-1])
        except Exception as exc:
            return Check("offline", "conversation", "fail", f"sandboxed language model did not run: {type(exc).__name__}: {exc}")
    if res.get("state") != "ready" or not res.get("say"):
        return Check("offline", "conversation", "fail", f"no offline reply ({res.get('state')}: {res.get('error') or res.get('problems')})", "make models")
    voice = f", Kokoro {res['voice_ms']} ms for {res['voice_s']} s of speech" if res.get("voice_ms") is not None else ", Kokoro unavailable"
    return Check("offline", "conversation", "ok",
                 f"{settings.local_llm_model} wrote {res['say']!r} (action {res.get('action')}) with the network blocked: "
                 f"load {res['load_ms']} ms, first token {res.get('first_token_ms')} ms, reply {res.get('total_ms')} ms{voice}")


# ------------------------------------------------------------------ main
def run_checks(settings, *, smoke: bool = True) -> list[Check]:
    checks = check_python()
    checks += check_models(settings.models_dir, settings.whisper_model)
    checks += check_local_ai(settings)
    checks += check_power()
    checks += check_dashboard(ROOT / "frontend")
    try:
        checks += check_voice(settings)
    except Exception as exc:
        checks.append(Check("voice", "voice settings", "fail", f"{type(exc).__name__}: {exc}"))
    checks += check_hardware_config(settings)
    checks.append(check_port(settings.port))
    host = settings.zeus_host
    checks += check_robot_network(host, settings.zeus_port, settings.zeus_camera_url or f"http://{host}:9000/mjpg")
    if smoke:
        checks.append(offline_start(settings))
        checks.append(offline_speech(settings))
        checks.append(offline_converse(settings))
    return checks


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--no-smoke", action="store_true", help="skip the in-process offline start")
    args = ap.parse_args()
    from app.config import Settings

    checks = run_checks(Settings(), smoke=not args.no_smoke)
    if args.json:
        print(json.dumps([asdict(c) for c in checks], indent=2))
    else:
        mark = {"ok": "ok  ", "warn": "WARN", "fail": "FAIL", "info": "info"}
        group = None
        for c in checks:
            if c.group != group:
                group = c.group
                print(f"\n{group.upper()}")
            print(f"  [{mark[c.status]}] {c.name}: {c.detail}" + (f"\n         fix: {c.fix}" if c.fix and c.status in ("fail", "warn") else ""))
        fails = [c for c in checks if c.status == "fail"]
        warns = [c for c in checks if c.status == "warn"]
        print(f"\n{'READY for offline use' if not fails else 'NOT READY'}: {len(fails)} missing, {len(warns)} warnings")
    return 1 if any(c.status == "fail" for c in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
