"""Local open-source model backend, via Ollama.

Deliberately the only place that talks to a model, and deliberately optional:
``composer.compose`` degrades to pure deterministic assembly if this is
unavailable, unreachable, or slow.

No API key, no metered spend, no external service receiving merchant or customer
data -- Ollama runs on localhost, which also satisfies the privacy rule in
testing brief §11 more strictly than a commercial LLM API would.

Determinism: ``temperature: 0`` plus a fixed ``seed``, and every response is
cached on disk keyed by a hash of (model, system, prompt), so identical inputs
return byte-identical output even across restarts.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
MODEL = os.getenv("VERA_LLM_MODEL", "qwen2.5:7b-instruct")
SEED = int(os.getenv("VERA_LLM_SEED", "42"))
TIMEOUT_S = float(os.getenv("VERA_LLM_TIMEOUT_S", "25"))
NUM_PREDICT = int(os.getenv("VERA_LLM_NUM_PREDICT", "220"))

CACHE_DIR = Path(os.getenv("VERA_LLM_CACHE", Path(__file__).parent / ".llm_cache"))

_stats = {"calls": 0, "cache_hits": 0, "errors": 0, "timeouts": 0}


def stats() -> dict[str, int]:
    return dict(_stats)


def _key(prompt: str, system: Optional[str]) -> str:
    h = hashlib.sha256()
    for part in (MODEL, str(SEED), system or "", prompt):
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def generate(prompt: str, system: Optional[str] = None) -> Optional[str]:
    """Return the model's completion, or None on any failure.

    None is a normal outcome, not an error path -- the caller keeps its
    deterministic draft.
    """
    key = _key(prompt, system)
    cache_file = CACHE_DIR / f"{key}.txt"
    if cache_file.exists():
        _stats["cache_hits"] += 1
        return cache_file.read_text(encoding="utf-8")

    body = {
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0,
            "top_p": 1,
            "seed": SEED,
            "num_predict": NUM_PREDICT,
            "repeat_penalty": 1.05,
        },
    }
    if system:
        body["system"] = system

    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    _stats["calls"] += 1
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError):
        _stats["timeouts"] += 1
        return None
    except Exception:
        _stats["errors"] += 1
        return None

    text = (payload.get("response") or "").strip()
    if not text:
        return None

    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(text, encoding="utf-8")
    except OSError:
        pass  # cache is an optimisation, never a requirement
    return text


def available() -> bool:
    try:
        req = urllib.request.Request(f"{OLLAMA_URL}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            tags = json.loads(resp.read().decode("utf-8"))
        names = {m.get("name", "") for m in tags.get("models", [])}
        return any(n == MODEL or n.startswith(MODEL.split(":")[0]) for n in names)
    except Exception:
        return False
