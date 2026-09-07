"""
Resolve the gateway's ACTUAL served model id from GET <base>/v1/models.

vLLM-style servers (the payments gateway) 404 a request whose `model` field does
not match the served id -- and that id is an opaque path like
"/app/models/jina-embeddings-v3", not the route slug. This asks the gateway what
it actually serves and caches the answer, so embeddings + rerank stop 404ing
without anyone hand-pasting magic strings.
"""
from __future__ import annotations

import json
import urllib.request

import config

_CACHE: dict = {}


def resolve_model_id(slug, fallback=None):
    """Return the served model id for a route slug (cached). Falls back to
    `fallback` (or the slug itself) if discovery fails."""
    if slug in _CACHE:
        return _CACHE[slug]
    resolved = fallback or slug
    url = config.endpoint(slug) + "/models"
    try:
        req = urllib.request.Request(
            url, headers={"Authorization": "Bearer " + config.API_KEY})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        items = data.get("data") or []
        if items and items[0].get("id"):
            resolved = items[0]["id"]
    except Exception:
        pass
    _CACHE[slug] = resolved
    return resolved


def clear_cache():
    _CACHE.clear()
