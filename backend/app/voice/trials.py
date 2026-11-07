"""Real-speech trials (docs/VOICE_TESTS.md): what was asked, what LOCKED IN heard.

A block is one speaker in one condition. The operator shows a prompt, the speaker says it,
and the next microphone utterance is scored against it. A quiet window scores everything
heard as a possible false activation. By default a block is a dry run: utterances are
classified and gated exactly as in normal use but not executed (stop words still halt
motion first, as always). Only text is kept, never audio, in
data/experiments/voice_trials.jsonl.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from .intents import Intent, IntentName

# The commands from the brief plus near-misses that must NOT act. Shared with the dashboard.
PROMPTS: list[dict[str, Any]] = [
    {"phrase": "Locked in, I have 25 minutes to finish calculus.", "expected": "start_session", "slots": {"duration_min": 25}},
    {"phrase": "Start a 25-minute session.", "expected": "start_session", "slots": {"duration_min": 25}},
    {"phrase": "Pause.", "expected": "pause"},
    {"phrase": "Resume.", "expected": "resume"},
    {"phrase": "Stop.", "expected": "stop_motion"},
    {"phrase": "Stop stop stop!", "expected": "stop_motion"},
    {"phrase": "Emergency stop!", "expected": "emergency_stop"},
    {"phrase": "Give me my phone.", "expected": "release"},
    {"phrase": "Okay, give my phone back.", "expected": "release"},
    {"phrase": "How much time is left?", "expected": "time_left"},
    {"phrase": "How am I doing?", "expected": "status"},
    {"phrase": "What should I do next?", "expected": "next_step"},
    {"phrase": "I finished two problems.", "expected": "progress", "slots": {"units": 2}},
    {"phrase": "Make it harder.", "expected": "harder"},
    {"phrase": "Stop roasting me.", "expected": "tone_down"},
    {"phrase": "End the session.", "expected": "end_session"},
    {"phrase": "Come back.", "expected": "return_robot"},
    {"phrase": "Shut up.", "expected": "mute"},
    {"phrase": "Chaos mode.", "expected": "set_personality"},
    # Near-misses: ordinary sentences a teammate might say near the mic.
    {"phrase": "My phone is over there somewhere.", "expected": "none"},
    {"phrase": "I'll finish this problem later.", "expected": "none"},
    {"phrase": "Can you pass me the charger?", "expected": "none"},
]

OUTCOMES = ("correct", "wrong_intent", "wrong_slots", "rejected", "no_response", "false_activation", "correct_reject")


def _pct(n: int, d: int) -> float | None:
    return None if d == 0 else round(n / d, 3)


def _q(xs: list[float], q: float) -> float | None:
    v = sorted(xs)
    return None if not v else round(v[min(len(v) - 1, int(len(v) * q))], 1)


class VoiceTrials:
    def __init__(self, path: Path, clock=time.monotonic):
        self.path = path
        self._clock = clock
        self.block: dict | None = None
        self.records: list[dict] = []  # all records this process, every block
        self.blocks: list[dict] = []

    # ------------------------------------------------------------ control
    def start_block(self, speaker: str, condition: str, execute: bool = False) -> dict:
        if self.block is not None:
            self.end_block()
        self.block = {
            "block_id": uuid.uuid4().hex[:8],
            "speaker": speaker.strip()[:24] or "?",
            "condition": condition.strip()[:60] or "?",
            "execute": bool(execute),
            "started_at": time.time(),
            "prompt": None,
            "quiet_until": None,
            "quiet_s": 0.0,
        }
        return dict(self.block)

    def set_prompt(self, expected: str, phrase: str, slots: dict | None = None) -> dict:
        b = self._need_block()
        if expected != "none":
            IntentName(expected)  # validate
        if b["prompt"] is not None:
            self._write(self._score_missing(b["prompt"]))
        b["prompt"] = {"expected": expected, "phrase": phrase[:200], "slots": slots or {}, "at": self._clock()}
        b["quiet_until"] = None
        return dict(b["prompt"])

    def miss(self) -> dict | None:
        b = self._need_block()
        if b["prompt"] is None:
            return None
        rec = self._score_missing(b["prompt"])
        b["prompt"] = None
        self._write(rec)
        return rec

    def start_quiet(self, seconds: float) -> dict:
        b = self._need_block()
        b["prompt"] = None
        b["quiet_until"] = self._clock() + seconds
        b["quiet_s"] += seconds
        return {"quiet_s": seconds}

    def end_block(self) -> dict:
        b = self._need_block()
        if b["prompt"] is not None:
            self._write(self._score_missing(b["prompt"]))
        if b["quiet_until"] is not None and b["quiet_until"] > self._clock():
            b["quiet_s"] -= b["quiet_until"] - self._clock()  # ended early
        summary = {
            "kind": "block",
            **{k: b[k] for k in ("block_id", "speaker", "condition", "execute", "started_at")},
            "ended_at": time.time(),
            "quiet_s": round(b["quiet_s"], 1),
            "summary": self.summarize([r for r in self.records if r["block_id"] == b["block_id"]], b["quiet_s"]),
        }
        self.blocks.append(summary)
        self._append(summary)
        self.block = None
        return summary

    # ------------------------------------------------------------ scoring
    def record(self, *, text: str, stt_confidence: float, stt_ms: float | None, intent: Intent, rejected_reason: str | None) -> dict | None:
        """Score one microphone utterance against the current prompt or quiet window."""
        b = self.block
        if b is None:
            return None
        now = self._clock()
        heard = intent.name.value
        accepted = rejected_reason is None
        quiet = b["quiet_until"] is not None and now <= b["quiet_until"]
        p = b["prompt"]
        if p is None and not quiet:
            return None  # between prompts: not part of the trial
        expected = "none" if quiet else p["expected"]
        if expected == "none":
            outcome = "false_activation" if accepted else "correct_reject"
        elif not accepted:
            outcome = "rejected"
        elif heard != expected:
            outcome = "wrong_intent"
        elif any(intent.slots.get(k) != v for k, v in p["slots"].items()):
            outcome = "wrong_slots"
        else:
            outcome = "correct"
        rec = {
            "kind": "utterance",
            "block_id": b["block_id"],
            "speaker": b["speaker"],
            "condition": b["condition"],
            "at": time.time(),
            "window": "quiet" if quiet else "prompt",
            "phrase": None if quiet else p["phrase"],
            "expected": expected,
            "expected_slots": {} if quiet else p["slots"],
            "heard_text": text,
            "heard_intent": heard,
            "heard_slots": intent.slots,
            "intent_confidence": round(intent.confidence, 3),
            "stt_confidence": round(stt_confidence, 3),
            "stt_ms": None if stt_ms is None else round(stt_ms, 1),
            "accepted": accepted,
            "rejected_reason": rejected_reason,
            "moves_robot": intent.moves_robot,
            "safety_critical": intent.safety_critical,
            "outcome": outcome,
            "prompt_to_heard_s": None if quiet else round(now - p["at"], 2),
        }
        if not quiet:
            b["prompt"] = None
        self._write(rec)
        return rec

    def _score_missing(self, p: dict) -> dict:
        b = self.block or {}
        return {
            "kind": "utterance",
            "block_id": b.get("block_id"),
            "speaker": b.get("speaker"),
            "condition": b.get("condition"),
            "at": time.time(),
            "window": "prompt",
            "phrase": p["phrase"],
            "expected": p["expected"],
            "expected_slots": p["slots"],
            "heard_text": None,
            "heard_intent": None,
            "outcome": "correct_reject" if p["expected"] == "none" else "no_response",
            "accepted": False,
            "moves_robot": False,
            "safety_critical": False,
            "stt_ms": None,
        }

    @staticmethod
    def summarize(recs: list[dict], quiet_s: float = 0.0) -> dict:
        prompts = [r for r in recs if r["window"] == "prompt" and r["expected"] != "none"]
        near = [r for r in recs if r["window"] == "prompt" and r["expected"] == "none"]
        quiet = [r for r in recs if r["window"] == "quiet"]
        count = {o: sum(1 for r in prompts if r["outcome"] == o) for o in OUTCOMES}
        stops = [r for r in prompts if r["expected"] in ("stop_motion", "emergency_stop")]
        fa_quiet = sum(1 for r in quiet if r["outcome"] == "false_activation")
        risky = [r for r in recs if r["outcome"] in ("wrong_intent", "false_activation") and (r["moves_robot"] or r["heard_intent"] in ("release", "end_session"))]
        stt = [r["stt_ms"] for r in recs if r.get("stt_ms") is not None]
        return {
            "prompts": len(prompts),
            **count,
            "accuracy": _pct(count["correct"], len(prompts)),
            "wrong_intent_rate": _pct(count["wrong_intent"] + count["wrong_slots"], len(prompts)),
            "rejection_rate": _pct(count["rejected"] + count["no_response"], len(prompts)),
            "stop_prompts": len(stops),
            "stop_recognised": _pct(sum(1 for r in stops if r["outcome"] == "correct"), len(stops)),
            "near_miss_prompts": len(near),
            "near_miss_false_activations": sum(1 for r in near if r["outcome"] == "false_activation"),
            "quiet_minutes": round(quiet_s / 60, 2),
            "quiet_false_activations": fa_quiet,
            "false_activations_per_10_min": None if quiet_s <= 0 else round(fa_quiet / (quiet_s / 600), 2),
            "risky_errors": len(risky),
            "stt_ms_p50": _q(stt, 0.5),
            "stt_ms_p95": _q(stt, 0.95),
        }

    def snapshot(self) -> dict:
        b = self.block
        groups: dict[tuple[str, str], list[dict]] = {}
        for r in self.records:
            groups.setdefault((r["speaker"], r["condition"]), []).append(r)
        quiet_by_group: dict[tuple[str, str], float] = {}
        for blk in self.blocks:
            key = (blk["speaker"], blk["condition"])
            quiet_by_group[key] = quiet_by_group.get(key, 0.0) + blk["quiet_s"]
        if b is not None:
            key = (b["speaker"], b["condition"])
            live_quiet = b["quiet_s"] - max(0.0, (b["quiet_until"] or 0) - self._clock())
            quiet_by_group[key] = quiet_by_group.get(key, 0.0) + max(0.0, live_quiet)
        return {
            "active": b is not None,
            "block": None
            if b is None
            else {
                "block_id": b["block_id"],
                "speaker": b["speaker"],
                "condition": b["condition"],
                "execute": b["execute"],
                "prompt": None if b["prompt"] is None else {k: b["prompt"][k] for k in ("expected", "phrase", "slots")},
                "quiet_left_s": None if b["quiet_until"] is None else round(max(0.0, b["quiet_until"] - self._clock()), 1),
            },
            "recent": self.records[-8:],
            "groups": [
                {"speaker": s, "condition": c, **self.summarize(rs, quiet_by_group.get((s, c), 0.0))} for (s, c), rs in groups.items()
            ],
        }

    # ------------------------------------------------------------ storage
    def _need_block(self) -> dict:
        if self.block is None:
            raise ValueError("start a voice trial block first")
        return self.block

    def _write(self, rec: dict) -> None:
        self.records.append(rec)
        self._append(rec)

    def _append(self, obj: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(obj, default=str) + "\n")
