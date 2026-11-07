"""Mode C: synthetic perception for demos and tests without a camera.

Every event produced here carries ``simulated=True`` and shows up in the
dashboard with a SIMULATED tag. Nothing in this module talks to hardware; the
robot side is whatever transport is connected (normally the mock).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from ..intelligence.state_machine import State
from ..sessions.metrics import FocusState
from ..telemetry.events import EventSource, EventType

if TYPE_CHECKING:
    from ..runtime import LockedInRuntime

log = logging.getLogger(__name__)


async def simulate_reach(rt: LockedInRuntime, *, confirm: bool = True, approach_ms: int = 220) -> None:
    """A hand approaches the phone zone and (optionally) dwells long enough to confirm."""
    from ..runtime import VisionEvent

    t0 = time.monotonic()
    await rt.handle_vision_event(
        VisionEvent("approaching", {"distance": 0.06, "speed": 0.9, "track": "sim"}, confidence=0.9, frame_mono=t0, simulated=True)
    )
    await asyncio.sleep(approach_ms / 1000)
    if confirm:
        await rt.handle_vision_event(
            VisionEvent(
                "confirmed",
                {"dwell_ms": approach_ms, "approach_seen": True, "track": "sim", "inside_fraction": 0.4},
                confidence=0.9,
                frame_mono=time.monotonic(),
                simulated=True,
            )
        )
    else:
        await rt.handle_vision_event(VisionEvent("cleared", {"track": "sim"}, simulated=True))


async def simulate_focus(rt: LockedInRuntime, state: str) -> None:
    from ..runtime import VisionEvent

    FocusState(state)  # validate
    await rt.handle_vision_event(VisionEvent("focus", {"state": state, "reason": "simulated"}, confidence=1.0, simulated=True))


DEMO_SCRIPT = [
    (0.0, "say", "Locked in, I have 25 minutes to finish calculus. Let's go."),
    (5.0, "reach", None),
    (7.0, "progress", 1),
    (4.0, "progress", 1),
    (3.0, "say", "How am I doing?"),
    (6.0, "say", "Okay, give my phone back."),
]


MAX_COOLDOWN_WAIT_S = 60.0


def _step(rt: LockedInRuntime, step: str, detail: str = "") -> None:
    rt.bus.publish(EventType.DEMO_STEP, EventSource.SYSTEM, {"step": step, "detail": detail}, simulated=True)


async def prepare_demo(rt: LockedInRuntime) -> tuple[bool, str]:
    """Put the runtime where the scripted demo can start, every time.

    Ends a running session, puts a displaced simulated robot back home (a displaced robot
    would start the session in HOLDING and never retreat), and refuses, with the reason,
    when it cannot do that safely: e-stop latched, robot moving, or a real robot away
    from home (only a person can put it back).
    """
    if rt.safety.estop_latched:
        return False, "emergency stop is latched: reset it first"
    if rt.safety.active is not None or rt.sm.state in (State.RETREATING, State.RETURNING):
        return False, "the robot is moving: wait for it to stop"
    if rt.sessions.current is not None:
        await rt.end_session(reason="demo_restart", origin="demo", speak=False)
        _step(rt, "ended the running session")
    simulated = rt.transport is None or rt.transport.simulated
    if rt.displacement:
        if not simulated:
            return False, "the robot is away from home: put it back and press Set home (or Return) first"
        r = await rt.set_home(origin="demo")
        if not r.ok:
            return False, r.message
        _step(rt, "simulated robot reset to home")
    if rt.transport is not None and rt.transport.connected:
        if not rt.safety.armed:
            await rt.arm(origin="demo")
        if simulated and not rt.safety.autonomy_enabled:
            await rt.set_autonomy(True, clear_path_confirmed=False, origin="demo")
    return True, "ready"


def reach_ready_in_s(rt: LockedInRuntime) -> float:
    """Seconds until a confirmed reach could trigger a retreat (policy and safety cooldowns)."""
    ctx = rt._policy_ctx()
    policy_left = 0.0 if ctx.seconds_since_intervention is None else ctx.cooldown_s - ctx.seconds_since_intervention
    safety_left = rt.safety.snapshot()["autonomous_cooldown_left_s"]
    return max(0.0, policy_left, safety_left)


async def run_demo(rt: LockedInRuntime, *, time_scale: float = 1.0, prepared: bool = False) -> None:
    """Plays the 90-second demo path with typed commands and simulated reaches.

    Used to rehearse and to smoke-test the full software loop. Labelled simulation
    throughout; it never pretends to be a live demo. Repeatable: it prepares the runtime
    first and waits out the cooldowns a previous run left behind. ``time_scale`` < 1
    shortens the pauses between steps (tests, soak runs).
    """
    log.info("demo scenario started (simulated perception)")
    try:
        if not prepared:
            ok, why = await prepare_demo(rt)
            if not ok:
                _step(rt, "demo not started", why)
                return
        _step(rt, "demo started")
        for delay, action, arg in DEMO_SCRIPT:
            await asyncio.sleep(delay * time_scale)
            if action == "say":
                await rt.handle_utterance(arg, source="typed")
            elif action == "reach":
                wait = reach_ready_in_s(rt)
                if wait > 0:
                    if wait > MAX_COOLDOWN_WAIT_S:
                        _step(rt, "reach skipped", f"cooldown has {wait:.0f}s left")
                        continue
                    _step(rt, "waiting for the cooldown", f"{wait:.1f}s")
                    await asyncio.sleep(wait + 0.05)
                await simulate_reach(rt)
            elif action == "progress":
                await rt.add_progress(int(arg), origin="demo", speak=False)
        _step(rt, "demo finished")
    except asyncio.CancelledError:
        log.info("demo scenario cancelled")
        raise
