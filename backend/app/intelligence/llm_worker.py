"""Local language model worker process (MLX on Apple silicon).

A separate process, like the Whisper worker: an out-of-memory or Metal failure kills
this process, not the backend (the robot then answers from its line library), its memory
is measurable on its own, and a stuck generation can be abandoned.

The persona (prompting.PERSONA) is identical every turn, so it is run through the model
once at startup and its KV cache kept; each request only adds the per-turn context
block and is trimmed off again afterwards.

Prompt reuse: the KV cache keeps the last prompt the model read (after the persona). A new
request only reads what comes after the longest common start with it: a retry of the same
turn, the next turn's unchanged context, and the speculative prefill all reuse it.

Speculative prefill: when the user starts talking, the host sends the context block
without the line being answered ("prefill"). The worker reads it while the user is still
talking and Whisper is still transcribing; the request that follows then only has to read
the quoted sentence (it shares that start).

Protocol over a multiprocessing Pipe:
  parent -> {"op": "generate", "id": n, "context": str, "max_tokens": int, "temperature": f, "top_p": f, "seed": int|None,
             "prefix_id": str|None}
            {"op": "prefill", "id": str, "prefix": str}
            {"op": "cancel", "id": n} | {"op": "stats"} | {"op": "stop"}
  child  -> {"op": "ready", ...load stats} | {"op": "error", "error": str}
            {"op": "delta", "id": n, "text": str} ... {"op": "done", "id": n, "text": str, ...timings}
            {"op": "prefilled", "id": str, "tokens": int, "ms": float}
            {"op": "stats", ...}
"""

from __future__ import annotations

import os
import time


def _rss_mb() -> float | None:
    try:
        import psutil

        return round(psutil.Process().memory_info().rss / 2**20, 1)
    except Exception:
        return None


class _Abort(Exception):
    """A person needs the model: a background request stops between prefill chunks."""


def _json_closed(text: str) -> bool:
    """True once the first top-level JSON object in ``text`` is complete."""
    depth, in_str, esc, started = 0, False, False, False
    for ch in text:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
            started = True
        elif ch == "}":
            depth -= 1
            if started and depth == 0:
                return True
    return False


def worker_main(conn, model_dir: str, stt_busy=None) -> None:
    """``stt_busy``: a multiprocessing Event the backend sets while Whisper transcribes on the
    same GPU; prefill work pauses between chunks until it clears (at most 1.5 s), so a short
    spoken command is not stuck behind a prefill."""
    os.environ.setdefault("HF_HUB_OFFLINE", "1")  # never reach for the network from here
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    t0 = time.perf_counter()
    try:
        import mlx.core as mx
        from mlx_lm import load
        from mlx_lm.generate import stream_generate
        from mlx_lm.models.cache import make_prompt_cache, trim_prompt_cache
        from mlx_lm.sample_utils import make_sampler

        from .prompting import render_prompt

        model, tok = load(model_dir)
        load_ms = (time.perf_counter() - t0) * 1000
        prefix, _ = render_prompt(tok, "warm up")
        prefix_ids = tok.encode(prefix, add_special_tokens=False)
        cache = make_prompt_cache(model)
        t1 = time.perf_counter()
        model(mx.array(prefix_ids)[None], cache=cache)
        mx.eval([c.state for c in cache])
        prefix_ms = (time.perf_counter() - t1) * 1000
        base = cache[0].offset
        # One tiny generation so the first real request does not pay for kernel compilation.
        _, rest = render_prompt(tok, 'USER JUST SAID: "hi"')
        for _ in stream_generate(model, tok, tok.encode(rest, add_special_tokens=False), max_tokens=2, prompt_cache=cache):
            pass
        trim_prompt_cache(cache, cache[0].offset - base)
        get_active = getattr(mx, "get_active_memory", None) or mx.metal.get_active_memory
        get_peak = getattr(mx, "get_peak_memory", None) or mx.metal.get_peak_memory
    except Exception as exc:
        conn.send({"op": "error", "error": f"could not load the language model at {model_dir}: {exc}"})
        return
    conn.send(
        {
            "op": "ready",
            "load_ms": round(load_ms),
            "prefix_tokens": base,
            "prefix_ms": round(prefix_ms),
            "warm_ms": round((time.perf_counter() - t0) * 1000),
            "rss_mb": _rss_mb(),
            "mlx_active_gb": round(get_active() / 1e9, 2),
        }
    )

    def head_for(prefix: str) -> str:
        """The chat-formatted text up to the end of ``prefix`` (the user header + prefix)."""
        _, rest = render_prompt(tok, prefix)
        return rest[: rest.index(prefix) + len(prefix)]

    def drop_to_base() -> None:
        trim_prompt_cache(cache, cache[0].offset - base)

    def yield_to_stt() -> None:
        if stt_busy is not None and stt_busy.is_set():
            t_wait = time.perf_counter()
            while stt_busy.is_set() and time.perf_counter() - t_wait < 1.5:
                time.sleep(0.005)

    def prefill_chunks(ids: list[int], chunk: int = 192) -> None:
        for i in range(0, len(ids), chunk):
            yield_to_stt()
            model(mx.array(ids[i : i + chunk])[None], cache=cache)
            mx.eval([c.state for c in cache])

    cached: list[int] = []  # the prompt tokens (after the persona) currently in the cache

    def reuse(ids: list[int]) -> list[int]:
        """Keep the longest common start of the cached prompt and ``ids``; return the rest
        (at least one token, so the model always has something to read before generating)."""
        common = 0
        for a, b in zip(cached, ids, strict=False):
            if a != b:
                break
            common += 1
        common = min(common, len(ids) - 1)
        trim_prompt_cache(cache, cache[0].offset - (base + common))
        del cached[common:]
        return ids[common:]

    pending: list[dict] = []
    while True:
        if pending:
            msg = pending.pop(0)
        else:
            try:
                msg = conn.recv()
            except (EOFError, OSError):
                return
        op = msg.get("op")
        if op == "stop":
            return
        if op == "stats":
            conn.send({"op": "stats", "rss_mb": _rss_mb(), "mlx_active_gb": round(get_active() / 1e9, 2), "mlx_peak_gb": round(get_peak() / 1e9, 2)})
            continue
        if op == "prefill":
            t_p = time.perf_counter()
            try:
                ids = tok.encode(head_for(msg["prefix"]), add_special_tokens=False)
                todo = reuse(ids)
                prefill_chunks(todo)
                cached[:] = ids
                conn.send({"op": "prefilled", "id": msg["id"], "tokens": len(ids), "read": len(todo), "ms": round((time.perf_counter() - t_p) * 1000, 1)})
            except Exception as exc:
                cached.clear()
                drop_to_base()
                conn.send({"op": "prefilled", "id": msg.get("id"), "error": str(exc)[:200]})
            continue
        if op != "generate":
            continue
        rid = msg["id"]
        t_req = time.perf_counter()
        text, first_ms, last, finish = "", None, None, "stop"
        try:
            if msg.get("seed") is not None:
                mx.random.seed(int(msg["seed"]))
            _, rest = render_prompt(tok, msg["context"])
            full = tok.encode(rest, add_special_tokens=False)
            ids = reuse(full)  # only what the cache does not already hold
            reused = len(full) - len(ids)
            used_spec = reused > 0
            sampler = make_sampler(temp=float(msg.get("temperature", 0.7)), top_p=float(msg.get("top_p", 0.9)))
            extra = {}
            if msg.get("background"):
                # Read the prompt in small chunks and look at the pipe between them: a person
                # starting to talk must not wait for a whole background prefill (~1 s).
                def progress(_done, _total, rid=rid):
                    yield_to_stt()
                    if conn.poll():
                        nxt = conn.recv()
                        pending.append(nxt)
                        if nxt.get("op") in ("prefill", "generate", "stop") or (nxt.get("op") == "cancel" and nxt.get("id") == rid):
                            raise _Abort()

                extra = {"prefill_step_size": 128, "prompt_progress_callback": progress}
            for r in stream_generate(model, tok, ids, max_tokens=int(msg.get("max_tokens", 96)), sampler=sampler, prompt_cache=cache, **extra):
                if first_ms is None:
                    first_ms = (time.perf_counter() - t_req) * 1000
                last = r
                if r.text:
                    text += r.text
                    conn.send({"op": "delta", "id": rid, "text": r.text})
                if r.finish_reason:
                    finish = r.finish_reason
                if _json_closed(text):
                    break
                # A cancel (or a newer request) between tokens ends this one now.
                if conn.poll():
                    nxt = conn.recv()
                    if nxt.get("op") == "cancel" and nxt.get("id") == rid:
                        finish = "cancelled"
                        break
                    if nxt.get("op") == "stop":
                        return
                    pending.append(nxt)
                    if nxt.get("op") == "generate":
                        finish = "superseded"
                        break
        except _Abort:
            finish = "cancelled"
            pending[:] = [m for m in pending if not (m.get("op") == "cancel" and m.get("id") == rid)]
        except Exception as exc:
            conn.send({"op": "error", "id": rid, "error": str(exc)[:300]})
            finish = "error"
        finally:
            try:
                # Drop what was generated, keep the prompt for the next request to reuse.
                if finish in ("stop", "length"):
                    trim_prompt_cache(cache, cache[0].offset - (base + len(full)))
                    cached[:] = full
                else:  # cancelled or failed half-way: keep only the part reused before it began
                    trim_prompt_cache(cache, cache[0].offset - (base + len(cached)))
            except Exception:
                cache = make_prompt_cache(model)  # start clean; the persona is rebuilt below
                model(mx.array(prefix_ids)[None], cache=cache)
                mx.eval([c.state for c in cache])
                cached.clear()
        if finish == "error":
            continue
        conn.send(
            {
                "op": "done",
                "id": rid,
                "text": text,
                "finish": finish,
                "prompt_tokens": getattr(last, "prompt_tokens", None),
                "prompt_tps": round(getattr(last, "prompt_tps", 0.0) or 0.0, 1),
                "gen_tokens": getattr(last, "generation_tokens", None),
                "gen_tps": round(getattr(last, "generation_tps", 0.0) or 0.0, 1),
                "first_token_ms": None if first_ms is None else round(first_ms, 1),
                "total_ms": round((time.perf_counter() - t_req) * 1000, 1),
                "peak_gb": round(getattr(last, "peak_memory", 0.0) or 0.0, 2),
                "prefilled": used_spec,
                "reused_tokens": reused,
            }
        )
