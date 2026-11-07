"""A scripted stand-in for the local language model (intelligence/local_llm.py).

It streams the reply in small chunks with an optional delay, exactly through the same
``generate`` coroutine the dialogue engine calls, so tests exercise the real parsing,
validation, guards, streaming and action execution without loading 2.3 GB of weights.
``reply`` may be a string, a dict (dumped as JSON) or a function of the context block.
"""

from __future__ import annotations

import asyncio
import json
import time

from app.intelligence.local_llm import Generation


class FakeLLM:
    state = "ready"

    def __init__(self, reply=None, *, chunk: int = 6, delay_s: float = 0.0, finish: str = "stop", fail: str | None = None):
        self.reply = reply if reply is not None else {"action": "none", "args": {}, "say": "Okay."}
        self.chunk = chunk
        self.delay_s = delay_s
        self.finish = finish
        self.fail = fail
        self.contexts: list[str] = []
        self.backgrounds: list[bool] = []
        self.calls = 0
        self._current = None

    def _text(self, context: str) -> str:
        r = self.reply(context) if callable(self.reply) else self.reply
        return r if isinstance(r, str) else json.dumps(r)

    async def generate(self, context, *, max_tokens=96, temperature=0.7, top_p=0.9, timeout_s=6.0, on_delta=None, background=False, seed=None):
        self.calls += 1
        self.contexts.append(context)
        self.backgrounds.append(background)
        t0 = time.monotonic()
        if self.fail:
            await asyncio.sleep(0)
            return Generation(finish=self.fail, error="scripted failure")
        text = self._text(context)
        out = Generation()
        self._current = (self.calls, background)
        try:
            for i in range(0, len(text), self.chunk):
                if time.monotonic() - t0 > timeout_s:
                    out.finish, out.error = "timeout", "scripted timeout"
                    return out
                piece = text[i : i + self.chunk]
                if out.first_token_ms is None:
                    out.first_token_ms = round((time.monotonic() - t0) * 1000, 1)
                out.text += piece
                if on_delta is not None:
                    on_delta(piece)
                await asyncio.sleep(self.delay_s)
        finally:
            self._current = None
        out.finish = self.finish
        out.total_ms = round((time.monotonic() - t0) * 1000, 1)
        out.gen_tokens = len(text) // 4
        return out

    def describe(self) -> dict:
        return {"provider": "fake", "state": self.state, "calls": self.calls}

    def close(self) -> None:
        pass


def install(rt, reply=None, timeout_s: float = 3.0, **kw) -> FakeLLM:
    """Give a test runtime a scripted model through the real dialogue engine."""
    from app.intelligence.dialogue import DialogueEngine

    llm = FakeLLM(reply, **kw)
    rt.companion.llm = llm
    rt.companion.dialogue = DialogueEngine(llm, timeout_s=timeout_s)
    return llm
