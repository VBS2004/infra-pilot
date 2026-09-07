"""generator.py - thin OpenAI-compatible chat client.

Calls any OpenAI-compatible gateway (DeepSeek, OpenAI, local vLLM, etc.)
using only the stdlib (urllib) — no openai SDK, no extra deps.

Configure via env vars:
    OPENAI_API_KEY   — bearer token / API key
    LLM_GATEWAY_BASE — base URL, e.g. https://api.deepseek.com  (default)
    LLM_GEN_MODEL    — model name, e.g. deepseek-chat            (default)
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

import config


class GeneratorError(RuntimeError):
    """Raised when the gateway is unreachable or returns an unusable reply."""


def _post(url: str, payload: dict, timeout: int) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": "Bearer " + config.API_KEY,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def complete(
    messages,
    *,
    model: str | None = None,
    temperature: float = 0.1,
    max_tokens: int = 4096,
    response_format=None,
    timeout: int = 120,
    retries: int = 2,
):
    """Run a chat completion and return the assistant message text.

    Args:
        messages:        OpenAI-style [{"role": ..., "content": ...}, ...].
        model:           Model name override (defaults to config.GEN_MODEL).
        temperature:     Low by default — we want deterministic HCL/JSON.
        response_format: Pass {"type": "json_object"} to force JSON output.
        retries:         Transient-failure retries before raising.
    """
    if not config.is_configured():
        raise GeneratorError(
            "Gateway not configured: set OPENAI_API_KEY "
            "(and LLM_GATEWAY_BASE if not using DeepSeek)."
        )

    url = config.endpoint().rstrip("/") + "/chat/completions"
    payload = {
        "model": model or config.GEN_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if response_format is not None:
        payload["response_format"] = response_format

    last_err = None
    for attempt in range(retries + 1):
        try:
            data = _post(url, payload, timeout)
            choices = data.get("choices") or []
            if not choices:
                raise GeneratorError("No choices in reply: " + json.dumps(data)[:500])
            content = (choices[0].get("message") or {}).get("content")
            if not content:
                raise GeneratorError("Empty content in reply: " + json.dumps(data)[:500])
            return content
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            last_err = GeneratorError(f"HTTP {e.code} from gateway: {detail}")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_err = GeneratorError(f"{type(e).__name__}: {e}")
        except (KeyError, ValueError, json.JSONDecodeError) as e:
            last_err = GeneratorError(f"Bad gateway response: {type(e).__name__}: {e}")

    raise last_err or GeneratorError("Unknown generation failure")


def _strip_code_fence(text: str) -> str:
    """Strip a leading ```json / ``` fence and trailing ``` if present."""
    t = text.strip()
    if t.startswith("```"):
        t = t[3:]
        if t[:4].lower() == "json":
            t = t[4:]
        if t.endswith("```"):
            t = t[:-3]
    return t.strip()


def complete_json(messages, **kw):
    """Force + parse JSON output (planner intent, reuse decision, etc.).

    Tries native response_format json mode first; tolerates servers that wrap
    JSON in a code fence or ignore the flag.
    """
    kw.setdefault("response_format", {"type": "json_object"})
    text = complete(messages, **kw)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(_strip_code_fence(text))


# ---------------------------------------------------------------------------
# Backward-compat shims for code that still uses slug-based args
# ---------------------------------------------------------------------------
# Some older call sites pass `slug=` or use GEN_SLUG / ESCALATION_SLUG.
# These stubs keep them working without changes.
GEN_SLUG = config.GEN_SLUG
ESCALATION_SLUG = config.GEN_SLUG


if __name__ == "__main__":
    # Smoke test: python generator.py "Say the single word: ok"
    import sys
    prompt = sys.argv[1] if len(sys.argv) > 1 else "Reply with the single word: ok"
    print("endpoint      :", config.endpoint())
    print("model         :", config.GEN_MODEL)
    print("configured    :", config.is_configured())
    print("---")
    print(complete([{"role": "user", "content": prompt}], max_tokens=32))
