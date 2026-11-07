"""Bounded local Nemotron transport. No sensor policy, shell, or OS actions.

Invoked by scripts/airlock_nemotron.ts; the authenticated TS adapter owns tools
and publishing. Importing this module never loads a model or touches hardware.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import psutil

MODEL = "nemotron-3.5-bolt:e4b"
MIN_AVAILABLE_BYTES = 10 * 1024**3  # conservative 7.9GB weights + bounded context/headroom
MAX_SWAP_PERCENT = 30
MAX_REQUEST_BYTES = 96_000


class ModelUnavailable(RuntimeError):
    """An honest unavailable state, never a substitute model response."""


def resource_preflight() -> dict[str, Any]:
    memory, swap = psutil.virtual_memory(), psutil.swap_memory()
    ready = memory.available >= MIN_AVAILABLE_BYTES and swap.percent <= MAX_SWAP_PERCENT
    return {
        "ready": ready,
        "model": MODEL,
        "available_bytes": memory.available,
        "total_bytes": memory.total,
        "swap_percent": swap.percent,
        "minimum_available_bytes": MIN_AVAILABLE_BYTES,
        "maximum_swap_percent": MAX_SWAP_PERCENT,
        "num_ctx": 2048,
        "concurrency": 1,
        "reason": "resource guard passed" if ready else "Model not loaded: insufficient free memory or excessive swap pressure",
    }


def local_endpoint(value: str) -> str:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Only an explicitly local HTTP Ollama endpoint is supported")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("Ollama endpoint must be a loopback origin")
    return value.rstrip("/")


def infer(payload: dict[str, Any], endpoint: str = "http://127.0.0.1:11434") -> dict[str, Any]:
    endpoint = local_endpoint(endpoint)
    preflight = resource_preflight()
    if not preflight["ready"]:
        raise ModelUnavailable(preflight["reason"])
    if set(payload) - {"messages", "tools"} or not isinstance(payload.get("messages"), list):
        raise ValueError("Expected only bounded messages and tool schemas")
    body = json.dumps({
        "model": MODEL, "messages": payload["messages"], "tools": payload.get("tools", []),
        "stream": False, "think": False, "keep_alive": 0,
        "options": {"num_ctx": 2048, "num_predict": 400, "temperature": 0},
    }).encode()
    if len(body) > MAX_REQUEST_BYTES:
        raise ValueError("Inference request exceeds bounded context transport budget")
    request = urllib.request.Request(endpoint + "/api/chat", data=body, headers={"Content-Type": "application/json"})
    # Disable ambient proxies; evidence never leaves the explicitly loopback server.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=45) as response:
        raw = response.read(256_001)
    if len(raw) > 256_000:
        raise ValueError("Model response too large")
    result = json.loads(raw)
    if result.get("model") != MODEL:
        raise ValueError("Actual model id does not match the required Nemotron tag")
    if result.get("done") is not True or result.get("done_reason") == "length":
        raise ModelUnavailable("Model response incomplete or output token budget reached")
    message = result.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise ValueError("Invalid Ollama assistant response")
    # Deliberately exclude thinking from transport, persistence, and traces.
    return {
        "model": result["model"],
        "message": {key: message[key] for key in ("role", "content", "tool_calls") if key in message},
        "metrics": {key: result.get(key) for key in (
            "total_duration", "load_duration", "prompt_eval_count", "eval_count", "eval_duration"
        )},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--endpoint", default="http://127.0.0.1:11434")
    args = parser.parse_args()
    try:
        if args.preflight:
            result = resource_preflight()
        else:
            raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
            if len(raw) > MAX_REQUEST_BYTES:
                raise ValueError("Input exceeds request budget")
            result = infer(json.loads(raw), args.endpoint)
        print(json.dumps(result))
    except (ValueError, OSError, RuntimeError, urllib.error.URLError) as error:
        print(json.dumps({"error": type(error).__name__, "detail": str(error)[:500], "model": MODEL}), file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
