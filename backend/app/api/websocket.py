"""Realtime channel to the dashboard.

Server -> client messages:

* ``{"type": "snapshot", "data": {...}}`` - full state, sent on connect, after any
  event (coalesced to at most every 60 ms) and as a 2 Hz heartbeat.
* ``{"type": "event", "data": Event}`` - each event as it happens.
* ``{"type": "audio", "data": {"rms_db": ..}}`` - microphone level (~12 Hz while listening).
* ``{"type": "pong", ...}`` - reply to a client ping, for latency measurement.

Client -> server: ``{"type": "ping", "t": <client ms>}`` and
``{"type": "ptt", "action": "start"|"stop"}``. Commands go over REST.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time

from fastapi import WebSocket, WebSocketDisconnect

from ..runtime import LockedInRuntime

log = logging.getLogger(__name__)

SNAPSHOT_MIN_INTERVAL_S = 0.06
SNAPSHOT_HEARTBEAT_S = 0.5
AUDIO_INTERVAL_S = 0.08


async def handle_ws(ws: WebSocket, rt: LockedInRuntime) -> None:
    await ws.accept()
    sub = rt.bus.subscribe(maxsize=500)
    dirty = asyncio.Event()
    send_lock = asyncio.Lock()

    async def send(obj: dict) -> None:
        async with send_lock:
            await ws.send_text(json.dumps(obj, default=str))

    async def pump_events() -> None:
        while True:
            event = await sub.get()
            await send({"type": "event", "data": event.model_dump(mode="json")})
            dirty.set()

    async def pump_snapshots() -> None:
        last = 0.0
        while True:
            try:
                await asyncio.wait_for(dirty.wait(), timeout=SNAPSHOT_HEARTBEAT_S)
            except TimeoutError:
                pass
            wait = SNAPSHOT_MIN_INTERVAL_S - (time.monotonic() - last)
            if wait > 0:
                await asyncio.sleep(wait)
            dirty.clear()
            t0 = time.perf_counter()
            snap = rt.snapshot()
            await send({"type": "snapshot", "data": snap})
            rt.perf.add("ws_snapshot_build_send_ms", (time.perf_counter() - t0) * 1000)
            last = time.monotonic()

    async def pump_audio() -> None:
        while True:
            await asyncio.sleep(AUDIO_INTERVAL_S)
            v = rt.voice
            if v is not None and v.running:
                await send({"type": "audio", "data": v.level()})

    async def receive() -> None:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = msg.get("type")
            if kind == "ping":
                await send({"type": "pong", "t": msg.get("t"), "server_time": time.time()})
            elif kind == "ptt" and rt.voice is not None:
                if msg.get("action") == "start":
                    rt.ptt(True)  # barge-in: answers and jokes stop when the user talks
                    rt.voice.ptt_start()
                elif msg.get("action") == "stop":
                    rt.voice.ptt_stop()

    await send({"type": "snapshot", "data": rt.snapshot()})
    for e in rt.bus.history(limit=150):
        await send({"type": "event", "data": e.model_dump(mode="json"), "replay": True})

    tasks = [asyncio.create_task(c()) for c in (pump_events, pump_snapshots, pump_audio, receive)]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for t in done:
            exc = t.exception()
            if exc and not isinstance(exc, WebSocketDisconnect | RuntimeError):
                log.warning("websocket task ended: %r", exc)
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(BaseException):
                await t
        sub.close()
