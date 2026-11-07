from app.intelligence.adaptation import AdaptationEngine
from app.intelligence.policy import InterventionPolicy, PolicyContext
from app.intelligence.state_machine import State
from app.robot.commands import MotionOrigin, MotionProfileName
from app.sessions.metrics import FocusState
from app.telemetry.events import EventBus, EventType

policy = InterventionPolicy()


def ctx(**kw) -> PolicyContext:
    base = dict(
        state=State.PHONE_REACH_CONFIRMED, session_active=True, session_paused=False, elapsed_active_s=600.0,
        remaining_s=900.0, planned_s=1500.0, focus_state=FocusState.FOCUSED, robot_connected=True,
        robot_can_move=True, robot_displaced=False, reaches_confirmed=1, seconds_since_intervention=None,
        base_cooldown_s=20.0,
    )
    base.update(kw)
    return PolicyContext(**base)


def test_confirmed_reach_requests_bounded_retreat_through_safety():
    d = policy.decide_reach(ctx(), 0.9)
    assert d.action == "retreat"
    assert d.motion.origin == MotionOrigin.POLICY
    assert d.motion.profile == MotionProfileName.SHORT_RETREAT
    assert any(r.startswith("R6") for r in d.rules)


def test_no_session_or_paused_means_no_reaction():
    assert policy.decide_reach(ctx(session_active=False), 0.9).action == "none"
    assert policy.decide_reach(ctx(session_paused=True, state=State.PAUSED), 0.9).action == "none"


def test_cooldown_suppresses_reaction():
    d = policy.decide_reach(ctx(seconds_since_intervention=5.0), 0.9)
    assert d.action == "none" and not d.speak


def test_intensity_scales_cooldown():
    assert ctx(intensity="strict").cooldown_s == 10.0
    assert ctx(intensity="gentle").cooldown_s == 60.0
    assert ctx(intensity="standard", cooldown_multiplier=0.5).cooldown_s == 10.0


def test_voice_only_when_robot_cannot_move():
    for kw in ({"robot_can_move": False}, {"robot_connected": False}, {"autonomy_suspended": True}):
        d = policy.decide_reach(ctx(**kw), 0.9)
        assert d.action == "voice_only" and d.motion is None, kw


def test_holding_never_moves_again():
    d = policy.decide_reach(ctx(state=State.HOLDING, robot_displaced=True), 0.9)
    assert d.action == "voice_only" and d.category == "reach_holding"


def test_line_categories():
    assert policy.decide_reach(ctx(elapsed_active_s=30), 0.9).category == "reach_early"
    assert policy.decide_reach(ctx(reaches_confirmed=1), 0.9).category == "reach_first"
    assert policy.decide_reach(ctx(reaches_confirmed=2), 0.9).category == "reach_repeat"
    assert policy.decide_reach(ctx(reaches_confirmed=4), 0.9).category == "reach_repeat_many"


def test_muted_still_acts_and_composes_the_line():
    # Mute silences audio in the Speaker; the line is still shown in the dashboard.
    d = policy.decide_reach(ctx(muted=True), 0.9)
    assert d.action == "retreat" and d.speak and d.category == "reach_first"


def test_focus_rules_never_talk_to_empty_room_and_ignore_glances():
    away = policy.decide_focus(ctx(state=State.SESSION_ACTIVE, focus_state=FocusState.AWAY), FocusState.FOCUSED, 300)
    assert away.action == "none"
    glance = policy.decide_focus(ctx(state=State.SESSION_ACTIVE, focus_state=FocusState.POSSIBLY_DISTRACTED), FocusState.FOCUSED, 10)
    assert glance.action == "none"
    sustained = policy.decide_focus(ctx(state=State.SESSION_ACTIVE, focus_state=FocusState.POSSIBLY_DISTRACTED), FocusState.FOCUSED, 60)
    assert sustained.action == "voice_only" and sustained.category == "nudge_distracted"
    focused = policy.decide_focus(ctx(state=State.SESSION_ACTIVE, focus_state=FocusState.FOCUSED), FocusState.FOCUSED, 600)
    assert focused.action == "none"


def test_adaptation_repeat_reaches_escalate_and_shorten_cooldown(clock):
    bus = EventBus()
    a = AdaptationEngine(bus, clock=clock)
    for _ in range(3):
        a.record_confirmed_reach()
        clock.advance(30)
    assert a.params.escalation == 1
    assert a.params.cooldown_multiplier == 0.75
    changes = [e for e in bus.history() if e.event_type == EventType.COACH_ADAPTATION]
    assert {c.payload["rule"] for c in changes} == {"A1"}


def test_adaptation_uncertain_movement_raises_strictness(clock):
    a = AdaptationEngine(EventBus(), clock=clock)
    for _ in range(4):
        a.record_uncertain()
        clock.advance(10)
    assert a.params.confirm_dwell_bonus_ms == 120
    assert a.params.min_confidence_bonus == 0.05


def test_false_triggers_suspend_autonomy(clock):
    a = AdaptationEngine(EventBus(), clock=clock)
    a.record_false_trigger()
    assert not a.params.autonomy_suspended
    a.record_false_trigger()
    assert a.params.autonomy_suspended
    a.resume_autonomy()
    assert not a.params.autonomy_suspended


def test_calm_decay(clock):
    a = AdaptationEngine(EventBus(), clock=clock)
    for _ in range(3):
        a.record_confirmed_reach()
    clock.advance(901)
    a.tick(focused_now=True)
    assert a.params.escalation == 0
    assert a.params.cooldown_multiplier == 1.0
