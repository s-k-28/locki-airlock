"""Fish Audio text-to-speech (https://docs.fish.audio): the optional cloud voice.

Nothing here talks to the network unless the operator put a key in ``.env``
(``FISH_AUDIO_API_KEY``), chose Fish Audio as the voice provider, and allowed it:

* cached clips play with no network at all (the robot's Wi-Fi has no internet);
* live generation happens only with the cache behaviour "cache, then live", within the
  daily byte budget, and never for safety lines;
* pre-generating the cache needs an explicit plan and confirmation.

The key travels only in the ``Authorization`` header to the Fish Audio API. It is never
logged, returned by LOCKED IN's API or shown in the dashboard.

API facts used (docs.fish.audio, checked 2026-11-01):

* ``POST /v1/tts``, ``Authorization: Bearer <key>``, a ``model`` header: s1, s2-pro,
  s2.1-pro, s2.1-pro-free, drama-3. An unknown model silently falls back to s2.1-pro, so
  the model is validated here. JSON body: ``text``, ``reference_id`` (the voice),
  ``format`` (wav), ``sample_rate`` (8-44.1 kHz for wav), ``latency`` (low / normal /
  balanced), ``prosody.speed`` (0.5-2.0), ``temperature`` and ``top_p`` (0-1),
  ``normalize``. Errors: 400 bad parameters or voice, 401 no permission, 402 no credit,
  429 too many concurrent requests (no Retry-After), 503 overloaded.
* ``GET /model/{id}``: a voice's title, languages and sample clips (404 if unknown).
  ``GET /model?self=true``: your own voices. ``GET /wallet/self/api-credit``: the account's
  API credit (a decimal string). TTS responses carry no usage headers, so usage is our own
  byte count plus the credit before and after.
* Billing counts UTF-8 bytes of the text. S2 models read ``[bracket]`` delivery cues at the
  start of a sentence; S1 used (parentheses) and is deprecated (retires 2026-12-31).
"""

from __future__ import annotations

import asyncio
import logging
import re
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .audio_cache import AudioCache
from .usage import UsageLedger, utf8_bytes

log = logging.getLogger(__name__)

API = "https://api.fish.audio"
SAMPLE_RATE = 24000
MAX_LIVE_TEXT_BYTES = 500
OFFLINE_BACKOFF_S = 30.0
OFFLINE_MODE = "offline mode: Fish Audio is never contacted; cached clips only"
REFERENCE_ID = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True)
class FishModel:
    id: str
    label: str
    usd_per_million_bytes: float
    bracket_cues: bool
    note: str = ""


MODELS: tuple[FishModel, ...] = (
    FishModel("s2.1-pro-free", "S2.1 Pro (free developer tier)", 0.0, True, "free; Fish Audio says changes are announced in advance"),
    FishModel("s2.1-pro", "S2.1 Pro", 15.0, True),
    FishModel("s2-pro", "S2 Pro", 15.0, True),
    FishModel("drama-3", "Drama 3", 50.0, False, "$10 per million bytes launch price until 2026-11-12"),
    FishModel("s1", "S1 (deprecated, retires 2026-12-31)", 15.0, False, "served by s2.1-pro after 2026-12-31"),
)
MODEL_IDS = frozenset(m.id for m in MODELS)
DEFAULT_MODEL = "s2.1-pro-free"


def model_info(model_id: str) -> FishModel | None:
    return next((m for m in MODELS if m.id == model_id), None)


def normalize_reference_id(value: str | None) -> str:
    """'' or a 32-character hex voice id (the id in a fish.audio voice URL)."""
    v = (value or "").strip().lower()
    if v.startswith("http"):
        v = v.rstrip("/").rsplit("/", 1)[-1]
    if v and not REFERENCE_ID.match(v):
        raise ValueError("a Fish Audio voice id is 32 hex characters, as in fish.audio/m/<id>")
    return v


def estimate_usd(nbytes: int, model_id: str) -> float | None:
    m = model_info(model_id)
    return None if m is None else round(nbytes / 1_000_000 * m.usd_per_million_bytes, 4)


class FishAudioError(Exception):
    """kind: no_key, invalid, auth, no_credit, not_found, rate_limited, busy, server, offline,
    budget, bad_audio."""

    def __init__(self, kind: str, message: str, status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status = status


_KINDS = {400: "invalid", 401: "auth", 402: "no_credit", 403: "auth", 404: "not_found", 422: "invalid", 429: "rate_limited", 503: "busy"}
_HINTS = {
    "auth": "the API key was rejected (check FISH_AUDIO_API_KEY)",
    "no_credit": "no API credit left for this model",
    "not_found": "voice not found (check the voice id)",
    "rate_limited": "too many requests at once",
    "busy": "Fish Audio is overloaded right now",
    "invalid": "Fish Audio rejected the request",
}


def _error_from(resp) -> FishAudioError:
    kind = _KINDS.get(resp.status_code, "server" if resp.status_code >= 500 else "invalid")
    detail = ""
    try:
        body = resp.json()
        detail = str(body.get("message") or body.get("detail") or "")[:200] if isinstance(body, dict) else ""
    except Exception:
        detail = ""
    msg = _HINTS.get(kind, f"Fish Audio error {resp.status_code}")
    return FishAudioError(kind, f"{msg}{': ' + detail if detail else ''} (HTTP {resp.status_code})", resp.status_code)


def parse_wav(data: bytes):
    """(int16 mono ndarray, sample_rate) from a WAV body. Tolerates the unknown data size a
    streamed WAV header may carry."""
    import numpy as np

    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise FishAudioError("bad_audio", "Fish Audio did not return WAV audio")
    pos, fmt = 12, None
    while pos + 8 <= len(data):
        cid = data[pos : pos + 4]
        size = int.from_bytes(data[pos + 4 : pos + 8], "little")
        start = pos + 8
        if cid == b"fmt ":
            tag, channels, rate, _byte_rate, _align, bits = struct.unpack("<HHIIHH", data[start : start + 16])
            if tag == 0xFFFE and size >= 40:  # WAVE_FORMAT_EXTENSIBLE: real tag in the sub-format GUID
                tag = int.from_bytes(data[start + 24 : start + 26], "little")
            fmt = (tag, channels, rate, bits)
        elif cid == b"data":
            if fmt is None:
                break
            end = start + size if 0 < size <= len(data) - start else len(data)
            tag, channels, rate, bits = fmt
            raw = data[start:end]
            if tag == 1 and bits == 16:
                pcm = np.frombuffer(raw[: len(raw) // 2 * 2], dtype="<i2").astype(np.int16)
            elif tag == 3 and bits == 32:
                f = np.frombuffer(raw[: len(raw) // 4 * 4], dtype="<f4")
                pcm = (np.clip(f, -1.0, 1.0) * 32767).astype(np.int16)
            else:
                raise FishAudioError("bad_audio", f"unsupported WAV encoding (format {tag}, {bits} bit)")
            if channels > 1:
                pcm = pcm[: len(pcm) // channels * channels].reshape(-1, channels).mean(axis=1).astype(np.int16)
            return pcm, rate
        if size == 0 or size >= 0xFFFFFFF0:
            break
        pos = start + size + (size & 1)
    raise FishAudioError("bad_audio", "WAV audio without a data chunk")


class FishAudioClient:
    def __init__(self, api_key: str, *, base_url: str = API, timeout_s: float = 10.0, transport: Any = None):
        self._key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._transport = transport

    def __repr__(self) -> str:  # never show the key
        return f"FishAudioClient(base_url={self.base_url!r}, key=<hidden>)"

    def _headers(self, model: str | None = None) -> dict:
        h = {"Authorization": f"Bearer {self._key}", "User-Agent": "locked-in"}
        if model:
            h["model"] = model
        return h

    async def _request(self, method: str, path: str, *, model: str | None = None, timeout: float | None = None, **kw):
        import httpx

        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=timeout or self.timeout_s, transport=self._transport) as client:
                resp = await client.request(method, path, headers=self._headers(model), **kw)
        except httpx.TimeoutException as exc:
            raise FishAudioError("offline", "Fish Audio did not answer in time") from exc
        except httpx.TransportError as exc:
            raise FishAudioError("offline", f"no connection to Fish Audio ({type(exc).__name__})") from exc
        if resp.status_code != 200:
            raise _error_from(resp)
        return resp

    async def tts(self, text: str, reference_id: str, model: str, settings: dict, timeout: float | None = None, timings: dict | None = None):
        """Synthesise ``text``; returns (pcm, sample_rate). Streams the body so ``timings`` (if
        given) gets first_byte_ms, total_ms and bytes."""
        if model not in MODEL_IDS:
            raise FishAudioError("invalid", f"unknown Fish Audio model {model!r}")
        if not REFERENCE_ID.match(reference_id or ""):
            raise FishAudioError("invalid", "no valid Fish Audio voice id")
        body = {
            "text": text,
            "reference_id": reference_id,
            "format": "wav",
            "sample_rate": SAMPLE_RATE,
            "latency": settings.get("latency", "balanced"),
            "normalize": bool(settings.get("normalize", True)),
            "prosody": {"speed": float(settings.get("speed", 1.0)), "volume": 0},
            "temperature": float(settings.get("temperature", 0.7)),
            "top_p": float(settings.get("top_p", 0.7)),
        }
        import httpx

        t0 = time.perf_counter()
        first: float | None = None
        chunks: list[bytes] = []
        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=timeout or self.timeout_s, transport=self._transport) as client:
                async with client.stream("POST", "/v1/tts", headers=self._headers(model), json=body) as resp:
                    if resp.status_code != 200:
                        await resp.aread()
                        raise _error_from(resp)
                    async for chunk in resp.aiter_bytes():
                        if first is None:
                            first = time.perf_counter()
                        chunks.append(chunk)
        except httpx.TimeoutException as exc:
            raise FishAudioError("offline", "Fish Audio did not answer in time") from exc
        except httpx.TransportError as exc:
            raise FishAudioError("offline", f"no connection to Fish Audio ({type(exc).__name__})") from exc
        data = b"".join(chunks)
        if timings is not None:
            timings.update(
                first_byte_ms=None if first is None else round((first - t0) * 1000, 1),
                total_ms=round((time.perf_counter() - t0) * 1000, 1),
                bytes=len(data),
            )
        return parse_wav(data)

    async def get_model(self, reference_id: str) -> dict:
        resp = await self._request("GET", f"/model/{reference_id}", timeout=6.0)
        return resp.json()

    async def get_api_credit(self) -> dict:
        resp = await self._request("GET", "/wallet/self/api-credit", timeout=6.0)
        return resp.json()

    async def list_models(self, *, mine: bool = True, title: str | None = None, page_size: int = 20) -> dict:
        params: dict[str, Any] = {"page_size": max(1, min(100, page_size))}
        if mine:
            params["self"] = "true"
        if title:
            params["title"] = title[:60]
        resp = await self._request("GET", "/model", params=params, timeout=6.0)
        return resp.json()


def voice_summary(m: dict) -> dict:
    samples = [s.get("audio") for s in (m.get("samples") or []) if isinstance(s, dict) and s.get("audio")]
    author = m.get("author") or {}
    return {
        "id": m.get("_id"),
        "title": m.get("title"),
        "description": (m.get("description") or "")[:200],
        "languages": m.get("languages") or [],
        "tags": (m.get("tags") or [])[:8],
        "visibility": m.get("visibility"),
        "type": m.get("type"),
        "state": m.get("state"),
        "author": author.get("nickname"),
        "sample_url": samples[0] if samples else None,
    }


class FishAudioVoice:
    """Cache, live generation, budget and status around ``FishAudioClient``."""

    provider = "fish_audio"

    def __init__(
        self,
        api_key: str | None,
        cache: AudioCache,
        usage: UsageLedger,
        *,
        base_url: str = API,
        transport: Any = None,
        clock: Callable[[], float] = time.monotonic,
        live_timeout_s: float = 6.0,
        offline: bool = False,
    ):
        self._client = FishAudioClient(api_key, base_url=base_url, transport=transport) if api_key else None
        # Offline mode (robot Wi-Fi, LOCKEDIN_OFFLINE): never open a connection; cached clips only.
        self.offline = offline
        self.cache = cache
        self.usage = usage
        self._clock = clock
        self.live_timeout_s = live_timeout_s
        self.status: dict[str, Any] = {"state": "no_key" if not api_key else "unchecked", "detail": "", "checked_at": None, "voice": None}
        if offline:
            self.status.update(state="offline_mode", detail=OFFLINE_MODE)
        self.offline_refusals = 0
        self._offline_until = 0.0
        self.generated = 0
        self.failures = 0
        self.last_latency_ms: float | None = None

    @property
    def configured(self) -> bool:
        return self._client is not None

    # ------------------------------------------------------------ text and keys
    @staticmethod
    def settings_key(p) -> dict:
        f = p.fish
        return {
            "latency": f.latency,
            "speed": round(f.speed, 2),
            "temperature": round(f.temperature, 2),
            "top_p": round(f.top_p, 2),
            "normalize": f.normalize,
            "rate": SAMPLE_RATE,
        }

    @staticmethod
    def text_for(req, p) -> str:
        m = model_info(p.model)
        cue = getattr(req, "cue", None)
        if cue and p.fish.cues and m is not None and m.bracket_cues:
            return f"[{cue}] {req.text}"
        return req.text

    def cache_key(self, tts_text: str, p) -> str:
        return AudioCache.key(self.provider, p.reference_id, p.model, self.settings_key(p), tts_text)

    def cached(self, tts_text: str, p):
        return self.cache.get(self.cache_key(tts_text, p))

    # ------------------------------------------------------------ live generation
    def live_refusal(self, req, p) -> str | None:
        """Why this line may not be generated live right now (None = it may)."""
        if self.offline:
            return OFFLINE_MODE
        if not self.configured:
            return "no Fish Audio API key"
        if not getattr(req, "allow_live", True):
            return "safety lines never wait for the network"
        if p.cache_behavior != "cache_then_live":
            return "live generation is off (cache only)"
        if self._clock() < self._offline_until:
            return "Fish Audio offline"
        n = utf8_bytes(self.text_for(req, p))
        if n > MAX_LIVE_TEXT_BYTES:
            return "line too long for live generation"
        if self.usage.used_today("live") + n > p.daily_live_bytes:
            return "today's live budget is used up"
        return None

    def _refuse_offline(self) -> None:
        if self.offline:
            self.offline_refusals += 1
            raise FishAudioError("offline_mode", OFFLINE_MODE)

    async def generate(self, tts_text: str, p, *, persist: bool, purpose: str, category: str = "") -> tuple:
        self._refuse_offline()
        if self._client is None:
            raise FishAudioError("no_key", "no Fish Audio API key")
        t0 = time.monotonic()
        try:
            clip = await self._client.tts(tts_text, p.reference_id, p.model, self.settings_key(p), timeout=self.live_timeout_s if purpose == "live" else 30.0)
        except FishAudioError as exc:
            self.failures += 1
            self._note_failure(exc)
            raise
        self.last_latency_ms = round((time.monotonic() - t0) * 1000, 1)
        nbytes = utf8_bytes(tts_text)
        self.usage.record(nbytes, purpose)
        self.generated += 1
        meta = {"text": tts_text, "category": category, "voice": p.reference_id, "model": p.model, "settings": self.settings_key(p), "bytes": nbytes}
        self.cache.put(self.cache_key(tts_text, p), clip[0], clip[1], meta, persist=persist)
        self._set_status("online", "")
        return clip

    def _note_failure(self, exc: FishAudioError) -> None:
        if exc.kind in ("offline", "busy"):
            self._offline_until = self._clock() + OFFLINE_BACKOFF_S
        state = {"offline": "offline", "auth": "auth_error", "no_credit": "no_credit", "not_found": "voice_not_found"}.get(exc.kind, "error")
        self._set_status(state, exc.message)

    def _set_status(self, state: str, detail: str, voice: dict | None = None) -> None:
        self.status = {**self.status, "state": state, "detail": detail, "checked_at": time.time()}
        if voice is not None:
            self.status["voice"] = voice

    # ------------------------------------------------------------ free lookups
    async def check(self, p) -> dict:
        """Key, network and voice in one free request (GET /model/{id})."""
        if self._client is None or self.offline:
            return self.status
        if not p.reference_id:
            self._set_status("no_voice", "paste a Fish Audio voice id")
            return self.status
        try:
            m = await self._client.get_model(p.reference_id)
        except FishAudioError as exc:
            self._note_failure(exc)
            return self.status
        self._offline_until = 0.0
        self._set_status("online", "", voice_summary(m))
        return self.status

    async def credit(self) -> dict:
        """Free: the account's remaining API credit (as Fish reports it)."""
        self._refuse_offline()
        if self._client is None:
            raise FishAudioError("no_key", "no Fish Audio API key")
        try:
            data = await self._client.get_api_credit()
        except FishAudioError as exc:
            self._note_failure(exc)
            raise
        out = {"credit": data.get("credit"), "has_free_credit": data.get("has_free_credit"), "checked_at": time.time()}
        self.status = {**self.status, "credit": out}
        return out

    async def voices(self, *, mine: bool = True, query: str | None = None) -> list[dict]:
        self._refuse_offline()
        if self._client is None:
            raise FishAudioError("no_key", "no Fish Audio API key")
        data = await self._client.list_models(mine=mine, title=query)
        return [voice_summary(m) for m in (data.get("items") or []) if isinstance(m, dict)]

    def describe(self) -> dict:
        return {
            "provider": self.provider,
            "configured": self.configured,
            "offline_mode": self.offline,
            "offline_refusals": self.offline_refusals,
            "status": self.status,
            "generated": self.generated,
            "failures": self.failures,
            "last_latency_ms": self.last_latency_ms,
            "usage_today_bytes": self.usage.today(),
            "cache_hits": self.cache.hits,
            "cache_files": len(self.cache.index),
        }


@dataclass
class CachePlanItem:
    category: str
    text: str
    tts_text: str
    key: str
    nbytes: int


class CacheJob:
    """Generates the missing clips of an approved plan, one request at a time."""

    def __init__(self, voice: FishAudioVoice, items: list[CachePlanItem], profile, on_progress: Callable[[dict], None] = lambda s: None):
        self.voice = voice
        self.items = items
        self.profile = profile
        self.on_progress = on_progress
        self.state: dict[str, Any] = {"state": "running", "total": len(items), "done": 0, "failed": 0, "bytes": 0, "error": None}
        self.task: asyncio.Task | None = None

    async def run(self) -> dict:
        try:
            for item in self.items:
                if self.voice.cache.has(item.key):
                    self.state["done"] += 1
                    continue
                for attempt in range(4):
                    try:
                        await self.voice.generate(item.tts_text, self.profile, persist=True, purpose="cache", category=item.category)
                        self.state["done"] += 1
                        self.state["bytes"] += item.nbytes
                        break
                    except FishAudioError as exc:
                        if exc.kind in ("rate_limited", "busy") and attempt < 3:
                            await asyncio.sleep(2**attempt)
                            continue
                        if exc.kind in ("auth", "no_credit", "offline", "no_key", "not_found"):
                            self.state.update(state="stopped", error=exc.message)
                            self.on_progress(dict(self.state))
                            return dict(self.state)
                        self.state["failed"] += 1
                        break
                self.on_progress(dict(self.state))
            self.state["state"] = "finished"
        except asyncio.CancelledError:
            self.state["state"] = "cancelled"
            raise
        finally:
            self.on_progress(dict(self.state))
        return dict(self.state)
