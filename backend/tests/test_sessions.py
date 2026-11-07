import pytest

from app.sessions.manager import SessionError, SessionManager
from app.sessions.metrics import FocusAccumulator, FocusState, SessionCounters, compute_metrics
from app.telemetry.events import EventBus, EventSource, EventType


def test_timer_counts_active_time(clock):
    sm = SessionManager(clock=clock)
    sm.start("calculus", 25 * 60)
    clock.advance(60)
    assert sm.active_seconds() == pytest.approx(60)
    assert sm.remaining_seconds() == pytest.approx(24 * 60)
    assert not sm.is_due()


def test_pause_resume_excludes_paused_time(clock):
    sm = SessionManager(clock=clock)
    sm.start("essay", 10 * 60)
    clock.advance(100)
    sm.pause()
    clock.advance(500)  # paused: must not count
    assert sm.active_seconds() == pytest.approx(100)
    sm.resume()
    clock.advance(50)
    assert sm.active_seconds() == pytest.approx(150)
    assert sm.wall_seconds() == pytest.approx(650)
    assert sm.current.pause_count == 1


def test_end_while_paused_accounts_pause(clock):
    sm = SessionManager(clock=clock)
    sm.start("x", 600)
    clock.advance(30)
    sm.pause()
    clock.advance(30)
    s = sm.end("user")
    assert sm.active_seconds(s) == pytest.approx(30)
    assert s.end_reason == "user"
    assert not sm.active


def test_due_when_planned_time_elapsed(clock):
    sm = SessionManager(clock=clock)
    sm.start("x", 120)
    clock.advance(119.9)
    assert not sm.is_due()
    clock.advance(0.2)
    assert sm.is_due()
    sm.pause()
    assert not sm.is_due()  # paused sessions never auto-complete


def test_progress_and_bounds(clock):
    sm = SessionManager(clock=clock)
    sm.start("problems", 600, target_units=5, unit_label="problems")
    sm.add_progress(2)
    sm.add_progress(1)
    assert sm.current.completed_units == 3
    sm.add_progress(-10)
    assert sm.current.completed_units == 0
    with pytest.raises(SessionError):
        sm.add_progress(0)
    with pytest.raises(SessionError):
        sm.add_progress(500)


def test_invalid_session_lifecycle(clock):
    sm = SessionManager(clock=clock)
    with pytest.raises(SessionError):
        sm.pause()
    sm.start("a", 600)
    with pytest.raises(SessionError):
        sm.start("b", 600)
    with pytest.raises(SessionError):
        sm.resume()
    sm.end("user")
    with pytest.raises(SessionError):
        sm.add_progress(1)
    with pytest.raises(SessionError):
        SessionManager(clock=clock).start("too short", 10)


def test_focus_accumulator_only_counts_while_counting(clock):
    acc = FocusAccumulator(clock=clock)
    acc.set_counting(True)
    acc.set_state(FocusState.FOCUSED)
    clock.advance(30)
    acc.set_state(FocusState.AWAY)
    clock.advance(10)
    acc.set_counting(False)  # paused
    clock.advance(100)
    acc.set_counting(True)
    acc.set_state(FocusState.FOCUSED)
    clock.advance(5)
    d = acc.snapshot()
    assert d["FOCUSED"] == pytest.approx(35)
    assert d["AWAY"] == pytest.approx(10)
    assert d["UNKNOWN"] == pytest.approx(0)


def test_metrics_are_unavailable_rather_than_invented():
    focus = {"FOCUSED": 2.0, "POSSIBLY_DISTRACTED": 0.0, "AWAY": 1.0, "UNKNOWN": 300.0}
    m = compute_metrics(
        elapsed_active_s=303, remaining_s=1197, planned_s=1500, completed_units=0, target_units=None,
        focus=focus, counters=SessionCounters(), vision_available=False,
    )
    assert m["presence_ratio"] is None  # < 5 s observed: no ratio
    assert m["observed_fraction"] == pytest.approx(3 / 303, abs=1e-3)
    assert m["unknown_s"] == 300.0


def test_metrics_presence_ratio():
    focus = {"FOCUSED": 60.0, "POSSIBLY_DISTRACTED": 30.0, "AWAY": 10.0, "UNKNOWN": 0.0}
    c = SessionCounters(reach_confirmed=2, interventions=3)
    m = compute_metrics(
        elapsed_active_s=100, remaining_s=500, planned_s=600, completed_units=4, target_units=10,
        focus=focus, counters=c, vision_available=True,
    )
    assert m["presence_s"] == 90.0
    assert m["presence_ratio"] == pytest.approx(0.9)
    assert m["reach_confirmed"] == 2 and m["interventions"] == 3
    assert m["progress_fraction"] == pytest.approx(100 / 600, abs=1e-4)


def test_event_ordering_and_ids():
    bus = EventBus()
    sub = bus.subscribe()
    a = bus.publish(EventType.SESSION_STARTED, EventSource.SESSION, {"n": 1})
    b = bus.publish(EventType.SESSION_PAUSED, EventSource.SESSION, {"n": 2})
    c = bus.publish(EventType.SESSION_RESUMED, EventSource.SESSION, {"n": 3})
    assert a.seq < b.seq < c.seq
    assert len({a.event_id, b.event_id, c.event_id}) == 3
    assert [sub.queue.get_nowait().seq for _ in range(3)] == [a.seq, b.seq, c.seq]
    assert [e.payload["n"] for e in bus.history()] == [1, 2, 3]


def test_slow_subscriber_drops_oldest_not_blocks():
    bus = EventBus()
    sub = bus.subscribe(maxsize=3)
    for i in range(10):
        bus.publish(EventType.SESSION_PROGRESS, EventSource.SESSION, {"i": i})
    got = [sub.queue.get_nowait().payload["i"] for _ in range(3)]
    assert got == [7, 8, 9]
    assert sub.dropped == 7
