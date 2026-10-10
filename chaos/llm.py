"""
Local LLM client (Ollama API) used by the planner and the analyst. No cloud API is used at runtime.

Returns parsed JSON plus metadata. Model output is data that deterministic code validates; it is
never executed. Standard library only.

  OLLAMA_URL   default http://localhost:11434. Any server that speaks the Ollama API can be used,
               e.g. Msty's Local AI service if its endpoint (shown in Msty > Settings > Local AI
               Service) is Ollama-compatible - check with: python chaos\\cli.py health
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")
_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # localhost: never via a proxy


class LLMError(RuntimeError):
    pass


def ollama_models(timeout: float = 5) -> list[str]:
    try:
        with _LOCAL.open(f"{OLLAMA_URL}/api/tags", timeout=timeout) as r:
            return sorted(m["name"] for m in json.loads(r.read()).get("models", []))
    except Exception as e:  # noqa: BLE001
        raise LLMError(f"Ollama not reachable at {OLLAMA_URL} ({e})") from e


def ollama_json(model: str, system: str, user: dict, schema: dict | None = None, timeout: float = 240) -> tuple[dict, dict]:
    """Ollama chat with output constrained to `schema` (Ollama structured outputs), else plain JSON mode."""
    body = json.dumps({"model": model, "stream": False, "format": schema or "json",
                       "options": {"temperature": 0.2},
                       "messages": [{"role": "system", "content": system},
                                    {"role": "user", "content": json.dumps(user)}]}).encode()
    req = urllib.request.Request(f"{OLLAMA_URL}/api/chat", data=body, method="POST",
                                 headers={"content-type": "application/json"})
    try:
        with _LOCAL.open(req, timeout=timeout) as r:
            resp = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise LLMError(f"Ollama HTTP {e.code}: {e.read()[:200]!r} (is '{model}' pulled? ollama pull {model})") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise LLMError(f"Ollama not reachable or timed out at {OLLAMA_URL} ({e})") from e
    text = (resp.get("message") or {}).get("content", "")
    return parse_json_object(text), {"provider": "ollama", "model": resp.get("model", model), "raw": text[:4000],
                                     "tokens_in": resp.get("prompt_eval_count"), "tokens_out": resp.get("eval_count")}


def parse_json_object(text: str) -> dict:
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        raise LLMError("model output contained no JSON object")
    try:
        obj = json.loads(t[start:end + 1])
    except json.JSONDecodeError as e:
        raise LLMError(f"malformed JSON from model: {e}") from e
    if not isinstance(obj, dict):
        raise LLMError("model output is not a JSON object")
    return obj
