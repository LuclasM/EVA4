"""
local_llm_detect.py — probe OpenAI-compatible / Ollama endpoints, and scan
common local ports to auto-detect a running local LLM server (Ollama,
LM Studio, vLLM, or anything else speaking the OpenAI /v1/models API), so
setup and the model manager don't require already knowing the base URL/port.

Shared by setup.py and model_manager.py — both used to carry their own
near-identical copy of the "try /v1/models then /models" probe.
"""
from __future__ import annotations

import concurrent.futures
import json
import urllib.request

_TIMEOUT      = 4     # seconds — a single deliberate probe of a known URL
_SCAN_TIMEOUT = 0.6   # seconds — the broad local-port scan (many parallel probes)

OLLAMA_PORT   = 11434
LMSTUDIO_PORT = 1234
# vLLM has no fixed default port (it's whatever --port the user launched it
# with), so it can't be probed like Ollama/LM Studio — scan a common range
# of ports instead and treat anything that answers the OpenAI schema as a
# local server.
_GENERIC_SCAN_PORTS = range(8000, 8011)


def fetch_openai_models(base_url: str, api_key: str = "", timeout: float = _TIMEOUT) -> tuple[list[str], str]:
    """Try /v1/models then /models on an OpenAI-compatible endpoint.

    Returns (sorted model ids, effective_base_url) where effective_base_url
    is the URL prefix that actually worked (always ends right before
    '/models'), so callers can save a normalized base_url. Returns
    ([], base_url) if nothing answered.
    """
    hdrs = {}
    if api_key and api_key.lower() not in ("none", ""):
        hdrs["Authorization"] = f"Bearer {api_key}"
    base = base_url.rstrip("/")
    candidates = []
    if not base.endswith("/v1"):
        candidates.append(base + "/v1/models")
    candidates.append(base + "/models")

    for url in candidates:
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read())
            models = data.get("data", [])
            if not isinstance(models, list) or not models:
                continue
            ids = sorted(m["id"] for m in models if isinstance(m, dict) and "id" in m)
            if ids:
                effective = url[: -len("/models")]
                return ids, effective
        except Exception:
            continue
    return [], base_url


def fetch_ollama_models(timeout: float = _TIMEOUT) -> list[str]:
    """Ollama's native /api/tags endpoint (not OpenAI-schema, needs its own parser)."""
    try:
        req = urllib.request.Request(f"http://localhost:{OLLAMA_PORT}/api/tags")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
        models = data.get("models", [])
        if not isinstance(models, list):
            return []
        return sorted(m["name"] for m in models if isinstance(m, dict) and "name" in m)
    except Exception:
        return []


def scan_local_llm_servers() -> list[dict]:
    """Probe common local ports for a running LLM server. Fast (parallel,
    short timeouts) and side-effect-free — just GET requests to localhost.

    Returns a list of {"provider", "base_url", "models"} dicts, one per
    server found. "provider" is "ollama"/"lmstudio" for the two servers with
    a fixed conventional port; anything else found via the generic port scan
    (e.g. vLLM) is labeled "local" since a bare OpenAI-schema response can't
    tell us which server implementation is actually behind it.
    """
    found: list[dict] = []

    def _check_ollama() -> None:
        models = fetch_ollama_models(timeout=_SCAN_TIMEOUT)
        if models:
            found.append({
                "provider": "ollama",
                "base_url": f"http://localhost:{OLLAMA_PORT}/v1",
                "models":   models,
            })

    def _check_lmstudio() -> None:
        models, effective = fetch_openai_models(f"http://localhost:{LMSTUDIO_PORT}/v1", timeout=_SCAN_TIMEOUT)
        if models:
            found.append({"provider": "lmstudio", "base_url": effective, "models": models})

    def _check_generic(port: int) -> None:
        models, effective = fetch_openai_models(f"http://localhost:{port}/v1", timeout=_SCAN_TIMEOUT)
        if models:
            found.append({"provider": "local", "base_url": effective, "models": models})

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
        futures = [ex.submit(_check_ollama), ex.submit(_check_lmstudio)]
        futures += [ex.submit(_check_generic, p) for p in _GENERIC_SCAN_PORTS]
        concurrent.futures.wait(futures)

    return found
