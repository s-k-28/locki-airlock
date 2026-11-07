"""Optional Claude replies for open-ended questions. Off unless configured.

Scope is deliberately narrow:
* only used for utterances no deterministic intent matched (small talk, "why is
  focus hard", ...); stop / pause / release / every robot command never reach it;
* it returns text only - nothing it says is parsed for actions;
* the runtime runs ``grounding_violations`` on the reply and discards it if it states a
  number that is not in the session facts.

It is a paid API, so it only runs when LOCKEDIN_LLM_PROVIDER=anthropic and a key (or
an `ant auth login` profile) is available.
"""

from __future__ import annotations

import json
import logging
import time

from .conversation import SessionFacts, fmt_duration

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are LOCKED IN, a small wheeled robot on a student's desk. During a focus \
session you hold their phone and roll away if they reach for it. You are funny and expressive, \
like a friend who talks trash but is on their side, and you keep it short because your reply \
is spoken aloud.

How to answer:
- One or two short sentences. No lists, no emoji, no stage directions.
- Style for this reply: {style}
- Swearing: {profanity}
- {insults}
- Never use slurs, threats, attacks on identity or appearance, or humiliating words like \
"worthless" or "pathetic". Tease the behaviour (reaching for the phone), not the person.
- Only mention a number if it appears in the session facts. If the user asks about something \
the facts do not cover, say you don't know rather than guessing.
- You cannot move, start or stop anything from this reply. If the user wants an action, tell \
them the voice command, for example "say 'lock in, 25 minutes'" or "say 'give me my phone'".
- If the question has nothing to do with studying or the session, answer briefly and steer \
back to the work."""


STYLES = {
    "chill": "chill: warm, relaxed, lightly sarcastic.",
    "roast": "roast: casual, funny trash talk, like an actual friend.",
    "unhinged": "unhinged: high-energy roasting the user explicitly opted into.",
    "judge_demo": "judge demo: energetic and memorable, presentation-safe.",
    "plain": "serious: plain and factual, no jokes.",
}
PROFANITY_RULES = ["none at all.", "mild words only (damn, hell, ass), and rarely.", "allowed, but not in every sentence."]


def system_prompt(style) -> str:
    """``style`` is a personality.Style (or a mode name)."""
    from .conversation import as_style

    st = as_style(style)
    if st.supportive:
        tone = "the user asked for help: be genuinely helpful and kind, no insults, no shaming."
    else:
        tone = STYLES.get(st.pool, STYLES["roast"])
    insults = (
        "Playful insults aimed at the user (like 'dumbass') are allowed, at most one per reply."
        if st.insults_allowed
        else "No insults aimed at the user."
    )
    return SYSTEM_PROMPT.format(style=tone, profanity=PROFANITY_RULES[st.max_profanity], insults=insults)


def facts_payload(f: SessionFacts) -> dict:
    if not f.session_active:
        return {"session_running": False}
    out = {
        "session_running": True,
        "paused": f.paused,
        "task": f.task,
        "elapsed": fmt_duration(f.elapsed_s),
        "remaining": fmt_duration(f.remaining_s),
        "completed_units": f.completed_units,
        "unit_label": f.unit_label,
        "robot_moves": f.robot_moves,
    }
    if f.target_units is not None:
        out["target_units"] = f.target_units
    if f.vision_available:
        out["phone_reaches_caught"] = f.reaches_confirmed
    else:
        out["phone_reaches_caught"] = "unknown (camera off)"
    return out


class ClaudeResponder:
    provider = "anthropic"

    def __init__(self, api_key: str | None, model: str, timeout_s: float):
        import anthropic

        self._anthropic = anthropic
        # With no explicit key the SDK resolves ANTHROPIC_API_KEY / auth token / `ant auth login`.
        self.client = anthropic.AsyncAnthropic(api_key=api_key) if api_key else anthropic.AsyncAnthropic()
        self.model = model
        self.timeout_s = timeout_s
        self.calls = 0
        self.failures = 0
        self.last_latency_ms: float | None = None
        self.last_error: str | None = None

    async def reply(self, text: str, facts: SessionFacts, style) -> str | None:
        anthropic = self._anthropic
        t0 = time.perf_counter()
        self.calls += 1
        content = (
            f"<session_facts>{json.dumps(facts_payload(facts))}</session_facts>\n"
            f"<user_said>{text}</user_said>"
        )
        try:
            response = await self.client.with_options(timeout=self.timeout_s, max_retries=0).beta.messages.create(
                model=self.model,
                max_tokens=2000,
                system=system_prompt(style),
                messages=[{"role": "user", "content": content}],
                output_config={"effort": "low"},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.AuthenticationError:
            self._fail("invalid or missing Anthropic credentials")
            return None
        except anthropic.RateLimitError:
            self._fail("rate limited")
            return None
        except anthropic.APIStatusError as exc:
            self._fail(f"API error {exc.status_code}: {exc.message}")
            return None
        except anthropic.APIConnectionError:
            # Expected when the laptop is joined to the robot's offline Wi-Fi.
            self._fail("no network connection")
            return None
        finally:
            self.last_latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        if response.stop_reason == "refusal":
            self._fail("model declined")
            return None
        reply = " ".join(b.text for b in response.content if b.type == "text").strip()
        self.last_error = None
        return reply or None

    def _fail(self, why: str) -> None:
        self.failures += 1
        self.last_error = why
        log.info("LLM reply unavailable: %s", why)

    def describe(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "calls": self.calls,
            "failures": self.failures,
            "last_latency_ms": self.last_latency_ms,
            "last_error": self.last_error,
        }


def make_llm(settings) -> ClaudeResponder:
    if settings.llm_provider != "anthropic":
        raise ValueError(f"unsupported LLM provider {settings.llm_provider!r}")
    return ClaudeResponder(settings.anthropic_api_key, settings.llm_model, settings.llm_timeout_s)
