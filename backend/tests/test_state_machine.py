import pytest

from app.intelligence.state_machine import (
    IGNORE,
    TRANSITIONS,
    MachineContext,
    State,
    StateMachine,
    Trigger,
)

CONNECTED = MachineContext(robot_connected=True)
IN_SESSION = MachineContext(robot_connected=True, session_active=True)


def machine(state: State) -> StateMachine:
    return StateMachine(state=state)


def test_every_state_has_a_row_and_accepts_emergency_stop():
    assert set(TRANSITIONS) == set(State)
    for state in State:
        sm = machine(state)
        r = sm.fire(Trigger.EMERGENCY_STOP, IN_SESSION)
        assert r.accepted, state
        assert sm.state == State.EMERGENCY_STOP, state


@pytest.mark.parametrize(
    "state",
    [s for s in State if s not in (State.DISCONNECTED, State.EMERGENCY_STOP)],
)
def test_release_overrides_every_operational_state(state):
    sm = machine(state)
    r = sm.fire(Trigger.RELEASE, IN_SESSION)
    assert r.accepted
    assert sm.state == State.RELEASED


def test_release_keeps_emergency_stop_latched_and_disconnected_offline():
    for state in (State.EMERGENCY_STOP, State.DISCONNECTED):
        sm = machine(state)
        r = sm.fire(Trigger.RELEASE, IN_SESSION)
        assert r.accepted and r.ignored
        assert sm.state == state


def test_full_demo_path():
    sm = machine(State.DISCONNECTED)
    assert sm.fire(Trigger.ROBOT_CONNECTED, CONNECTED).to_state == State.IDLE
    assert sm.fire(Trigger.START_SESSION, IN_SESSION).to_state == State.SESSION_ACTIVE
    assert sm.fire(Trigger.REACH_SUSPECTED, IN_SESSION).to_state == State.POSSIBLE_PHONE_REACH
    assert sm.fire(Trigger.REACH_CONFIRMED, IN_SESSION).to_state == State.PHONE_REACH_CONFIRMED
    assert sm.fire(Trigger.RETREAT_STARTED, IN_SESSION).to_state == State.RETREATING
    displaced = MachineContext(robot_connected=True, session_active=True, robot_displaced=True)
    assert sm.fire(Trigger.MOTION_COMPLETED, displaced).to_state == State.HOLDING
    assert sm.fire(Trigger.RETURN_REQUESTED, displaced).to_state == State.RETURNING
    assert sm.fire(Trigger.MOTION_COMPLETED, IN_SESSION).to_state == State.SESSION_ACTIVE
    assert sm.fire(Trigger.RELEASE, IN_SESSION).to_state == State.RELEASED
    assert sm.fire(Trigger.RESET, CONNECTED).to_state == State.IDLE


@pytest.mark.parametrize(
    "state,trigger",
    [
        (State.IDLE, Trigger.RETREAT_STARTED),
        (State.IDLE, Trigger.RESUME),
        (State.SESSION_ACTIVE, Trigger.START_SESSION),
        (State.SESSION_ACTIVE, Trigger.RETREAT_STARTED),
        (State.SESSION_ACTIVE, Trigger.RESUME),
        (State.POSSIBLE_PHONE_REACH, Trigger.RETREAT_STARTED),
        (State.EMERGENCY_STOP, Trigger.START_SESSION),
        (State.EMERGENCY_STOP, Trigger.RESUME),
        (State.EMERGENCY_STOP, Trigger.RETREAT_STARTED),
        (State.RETREATING, Trigger.HOME_SET),
        (State.RETURNING, Trigger.START_SESSION),
        (State.HOLDING, Trigger.RETREAT_STARTED),
        (State.PAUSED, Trigger.RETREAT_STARTED),
    ],
)
def test_invalid_transitions_are_rejected_without_changing_state(state, trigger):
    sm = machine(state)
    r = sm.fire(trigger, IN_SESSION)
    assert not r.accepted
    assert r.reason
    assert sm.state == state
    assert len(sm.history) == 0


def test_retreat_requires_confirmation_first():
    sm = machine(State.SESSION_ACTIVE)
    assert not sm.can(Trigger.RETREAT_STARTED, IN_SESSION)
    sm.fire(Trigger.REACH_CONFIRMED, IN_SESSION)
    assert sm.can(Trigger.RETREAT_STARTED, IN_SESSION)


def test_holding_ignores_further_reaches():
    sm = machine(State.HOLDING)
    for trig in (Trigger.REACH_SUSPECTED, Trigger.REACH_CONFIRMED, Trigger.REACH_CLEARED):
        r = sm.fire(trig, IN_SESSION)
        assert r.accepted and r.ignored
        assert sm.state == State.HOLDING


def test_return_guard_requires_displacement_and_link():
    sm = machine(State.SESSION_ACTIVE)
    r = sm.fire(Trigger.RETURN_REQUESTED, IN_SESSION)
    assert not r.accepted and "guard" in r.reason
    displaced_offline = MachineContext(robot_connected=False, session_active=True, robot_displaced=True)
    assert not sm.can(Trigger.RETURN_REQUESTED, displaced_offline)


def test_reconnect_restores_session_context():
    sm = machine(State.DISCONNECTED)
    paused = MachineContext(robot_connected=True, session_active=True, session_paused=True)
    assert sm.fire(Trigger.ROBOT_CONNECTED, paused).to_state == State.PAUSED
    sm = machine(State.DISCONNECTED)
    displaced = MachineContext(robot_connected=True, session_active=True, robot_displaced=True)
    assert sm.fire(Trigger.ROBOT_CONNECTED, displaced).to_state == State.HOLDING


def test_session_can_start_while_robot_offline():
    sm = machine(State.DISCONNECTED)
    r = sm.fire(Trigger.START_SESSION, MachineContext(robot_connected=False, session_active=True))
    assert r.accepted and sm.state == State.DISCONNECTED


def test_disconnect_during_motion_goes_to_disconnected():
    for state in (State.RETREATING, State.RETURNING, State.HOLDING, State.SESSION_ACTIVE):
        sm = machine(state)
        assert sm.fire(Trigger.ROBOT_DISCONNECTED, IN_SESSION).to_state == State.DISCONNECTED


def test_estop_latches_until_reset_and_reset_settles_on_context():
    sm = machine(State.SESSION_ACTIVE)
    sm.fire(Trigger.EMERGENCY_STOP, IN_SESSION)
    for trig in (Trigger.ROBOT_CONNECTED, Trigger.ROBOT_DISCONNECTED, Trigger.REACH_CONFIRMED, Trigger.PAUSE):
        sm.fire(trig, IN_SESSION)
        assert sm.state == State.EMERGENCY_STOP
    assert sm.fire(Trigger.RESET, IN_SESSION).to_state == State.SESSION_ACTIVE
    sm = machine(State.EMERGENCY_STOP)
    assert sm.fire(Trigger.RESET, MachineContext(robot_connected=False)).to_state == State.DISCONNECTED


def test_pause_during_retreat_and_resume_into_holding():
    sm = machine(State.RETREATING)
    assert sm.fire(Trigger.PAUSE, IN_SESSION).to_state == State.PAUSED
    displaced_paused = MachineContext(robot_connected=True, session_active=True, session_paused=False, robot_displaced=True)
    assert sm.fire(Trigger.RESUME, displaced_paused).to_state == State.HOLDING


def test_home_set_in_holding_returns_to_session():
    sm = machine(State.HOLDING)
    assert sm.fire(Trigger.HOME_SET, IN_SESSION).to_state == State.SESSION_ACTIVE


def test_history_records_only_applied_transitions():
    sm = machine(State.IDLE)
    sm.fire(Trigger.RETREAT_STARTED, CONNECTED)  # rejected
    sm.fire(Trigger.REACH_CONFIRMED, CONNECTED)  # ignored in IDLE
    sm.fire(Trigger.START_SESSION, IN_SESSION)
    assert [h.to_state for h in sm.history] == ["SESSION_ACTIVE"]


def test_ignore_entries_are_explicit_objects():
    # Guard against a typo turning an IGNORE into an accidental transition.
    for row in TRANSITIONS.values():
        for target in row.values():
            assert target is IGNORE or isinstance(target, State) or callable(target)
