"""Provider model discovery; recommendations are never an authorization list.

The web ConfigHandler registry remains the canonical recommendation source.
Discovery uses only configured credentials/endpoints, never follows redirects,
and does not make inference requests or infer capabilities from model names.
"""
import copy
import hashlib
import json
import threading
import time
from collections import OrderedDict
from urllib.parse import urlsplit

import requests


# Only documented model-list protocols belong here. Other providers keep their
# recommendations and custom IDs until their discovery contract is verified.
STRATEGIES = {
    "openai": "openai", "deepseek": "openai", "qianfan": "openai",
    "claudeAPI": "anthropic", "gemini": "gemini", "mimo": "mimo",
}
_cache = OrderedDict()
_lock = threading.Lock()
_TTL = 300
_LIMIT = 128
_MAX_BYTES = 2 * 1024 * 1024


def _read_json(url, headers, params):
    with requests.get(url, headers=headers, params=params, timeout=(3, 5),
                      allow_redirects=False, stream=True) as response:
        if response.status_code != 200:
            raise ValueError("http_error")
        chunks, size = [], 0
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > _MAX_BYTES:
                raise ValueError("response_too_large")
            chunks.append(chunk)
        data = json.loads(b"".join(chunks))
        if not isinstance(data, dict):
            raise ValueError("invalid_response")
        return data


def _discover(strategy, base, key):
    headers = {"Authorization": "Bearer " + key}
    params = {}
    if strategy == "mimo":
        headers = {"api-key": key}
    if strategy == "anthropic":
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        params = {"limit": 1000}
    elif strategy == "gemini":
        headers = {"x-goog-api-key": key}
        base = base.rstrip("/") + "/v1beta"
        params = {"pageSize": 1000}
    url = base.rstrip("/") + "/models"
    models, seen_cursors = {}, set()
    for _ in range(5):
        data = _read_json(url, headers, params)
        entries = data.get("models" if strategy == "gemini" else "data")
        if not isinstance(entries, list):
            raise ValueError("invalid_response")
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("invalid_response")
            model = entry.get("name" if strategy == "gemini" else "id")
            if not isinstance(model, str) or not model or len(model) > 256:
                raise ValueError("invalid_response")
            if strategy == "gemini":
                if "generateContent" not in entry.get("supportedGenerationMethods", []):
                    continue
                model = model.removeprefix("models/")
            # Null means UNKNOWN, not supported/unsupported. Listing a model
            # never establishes compatibility with the agent's tool protocol.
            capabilities = dict.fromkeys(("vision", "reasoning", "tools", "multimodal", "streaming", "context_window"))
            if strategy == "gemini":
                capabilities["context_window"] = entry.get("inputTokenLimit")
            elif strategy == "anthropic":
                metadata = entry.get("capabilities") or {}
                capabilities["vision"] = (metadata.get("image_input") or {}).get("supported")
                capabilities["reasoning"] = (metadata.get("thinking") or {}).get("supported")
                capabilities["context_window"] = entry.get("max_input_tokens")
            models[model] = {"id": model, "capabilities": capabilities}
        cursor = None
        if strategy == "gemini":
            cursor = data.get("nextPageToken")
            params["pageToken"] = cursor
        elif strategy == "anthropic" and data.get("has_more"):
            cursor = data.get("last_id")
            if not cursor:
                raise ValueError("invalid_pagination")
            params["after_id"] = cursor
        if not cursor:
            return list(models.values())
        if not isinstance(cursor, str) or cursor in seen_cursors:
            raise ValueError("invalid_pagination")
        seen_cursors.add(cursor)
    raise ValueError("page_limit")


def get_catalog(provider_id, meta, config, *, discover=False):
    recommendations = list(meta.get("models") or [])
    base_field, key_field = meta.get("api_base_key"), meta.get("api_key_field")
    base = str((config.get(base_field, "") if base_field else "") or meta.get("api_base_default") or "").rstrip("/")
    key = str((config.get(key_field, "") if key_field else "") or "")
    strategy = STRATEGIES.get(provider_id)
    result = {"models": recommendations, "source": "builtin", "discovery": "not_supported",
              "custom_model_allowed": True, "metadata": []}
    if not strategy:
        return result
    if not key or key in ("YOUR API KEY", "YOUR_API_KEY"):
        result["discovery"] = "no_key"
        return result
    # Do not retain the raw credential in the cache or expose it in errors.
    cache_key = (provider_id, base, hashlib.sha256(key.encode()).hexdigest())
    with _lock:
        cached = _cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < _TTL:
            return copy.deepcopy(cached[1])
    result["discovery"] = "not_requested"
    if not discover:
        return result
    try:
        parsed = urlsplit(base)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("invalid_endpoint")
        entries = _discover(strategy, base, key)
        if not entries:
            raise ValueError("empty_response")
        result.update(models=[entry["id"] for entry in entries], metadata=entries,
                      source="discovered", discovery="success")
    except (requests.RequestException, ValueError, TypeError, AttributeError):
        # A failed catalog refresh must be visible without leaking credentials,
        # server response bodies or URLs. Keep recommendations, never fake a
        # successful inference response or silently erase a saved model ID.
        result["discovery"] = "failed"
    with _lock:
        _cache[cache_key] = (time.monotonic(), copy.deepcopy(result))
        _cache.move_to_end(cache_key)
        while len(_cache) > _LIMIT:
            _cache.popitem(last=False)
    return result
