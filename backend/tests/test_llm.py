"""LLM path without network: a fake responder exercises the grounding guard and fallback."""

from app.voice.conversation import SessionFacts
from app.voice.llm import facts_payload


class FakeLLM:
    def __init__(self, reply):
        self._reply = reply
        self.calls = 0

    async def reply(self, text, facts, personality):
        self.calls += 1
        return self._reply

    def describe(self):
        return {"provider": "fake"}


async def test_grounded_llm_reply_is_used_for_unknown_questions(rt, tts):
    await rt.start_session("calculus", duration_min=25, speak=False)
    rt.llm = FakeLLM("Focus is a muscle. Keep going on calculus.")
    r = await rt.handle_utterance("why is studying so hard")
    assert r.reply == "Focus is a muscle. Keep going on calculus."
    assert rt.llm.calls == 1


async def test_ungrounded_llm_reply_is_discarded(rt, tts):
    await rt.start_session("calculus", duration_min=25, speak=False)
    rt.llm = FakeLLM("You've done 9 problems and reached for your phone 4 times.")
    r = await rt.handle_utterance("why is studying so hard")
    assert r.reply in ("Say that again?", "Didn't catch that.", "What?")
    assert any(e.payload.get("numbers") == [4, 9] for e in rt.bus.history(100) if e.event_type == "voice.command_rejected")


async def test_safety_commands_never_reach_the_llm(rt):
    rt.llm = FakeLLM("whatever")
    for text in ("stop", "give me my phone", "pause", "emergency stop"):
        await rt.handle_utterance(text)
    assert rt.llm.calls == 0


def test_facts_payload_marks_unknowns():
    f = SessionFacts(session_active=True, task="essay", elapsed_s=600, remaining_s=900, vision_available=False)
    p = facts_payload(f)
    assert p["phone_reaches_caught"] == "unknown (camera off)"
    assert p["elapsed"] == "10 minutes"
    assert facts_payload(SessionFacts()) == {"session_running": False}
