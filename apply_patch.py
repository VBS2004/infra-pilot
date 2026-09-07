#!/usr/bin/env python3
"""
One-shot patcher: wires the payments indexer to the live Jina embedder + Qwen3
reranker.

What it does (idempotent, makes *.bak backups first):
  1. Installs gateway.py (served-model-id resolver) if missing.
  2. lexical_search.py : import gateway; replace HttpReranker with the
     self-healing version (/rerank -> /score, no silent failures); use the
     resolved served id for the reranker + embedder model fields.
  3. livecheck.py      : import gateway; use the resolved reranker id.
  4. embedder.py       : import gateway; use the resolved embedder id
     (so the LLM_EMBED_MODEL env var becomes optional).

Run it from INSIDE your payments_indexer folder:
    python3 apply_patch.py
Then:
    python3 cli.py fixtures/payments livecheck
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

GATEWAY_PY = r'''"""
Resolve the gateway's ACTUAL served model id from GET <base>/v1/models.
"""
from __future__ import annotations

import json
import urllib.request

import config

_CACHE = {}


def resolve_model_id(slug, fallback=None):
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
'''

NEW_RERANKER = r'''class HttpReranker:
    """Cross-encoder reranker over the payments gateway.

    Tries vLLM /rerank (Jina/Cohere schema) then /score. Returns a list of
    (original_index, score) sorted best-first. On failure it prints to stderr
    (no longer silent) and returns identity order so retrieval cleanly degrades
    to the fused BM25+dense ranking.
    """

    def __init__(self, base_url, api_key, model, timeout=60):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def _post(self, path, payload):
        import json as _json
        import urllib.request as _u
        data = _json.dumps(payload).encode("utf-8")
        req = _u.Request(
            self.base_url + path, data=data, method="POST",
            headers={"Authorization": "Bearer " + self.api_key,
                     "Content-Type": "application/json"})
        with _u.urlopen(req, timeout=self.timeout) as r:
            return _json.loads(r.read().decode("utf-8", "replace"))

    def __call__(self, query, chunks):
        import sys as _sys
        docs = [getattr(c, "text", str(c)) for c in chunks]
        if not docs:
            return []
        rerank_err = None
        try:
            j = self._post("/rerank", {"model": self.model, "query": query,
                                       "documents": docs})
            results = j.get("results")
            if results:
                out = []
                for item in results:
                    idx = item.get("index")
                    if idx is None:
                        continue
                    score = item.get("relevance_score", item.get("score", 0.0))
                    out.append((idx, float(score)))
                if out:
                    out.sort(key=lambda t: t[1], reverse=True)
                    return out
        except Exception as e:
            rerank_err = e
        try:
            j = self._post("/score", {"model": self.model, "text_1": query,
                                      "text_2": docs})
            data = j.get("data")
            if data:
                out = []
                for i, item in enumerate(data):
                    idx = item.get("index", i)
                    score = item.get("score", item.get("relevance_score", 0.0))
                    out.append((idx, float(score)))
                if out:
                    out.sort(key=lambda t: t[1], reverse=True)
                    return out
        except Exception as e:
            print("[rerank] both routes failed (/rerank: %r ; /score: %r); "
                  "keeping fused order" % (rerank_err, e), file=_sys.stderr)
        return [(i, 0.0) for i in range(len(docs))]
'''


def backup(path):
    b = path + ".bak"
    if not os.path.exists(b):
        with open(path) as f:
            data = f.read()
        with open(b, "w") as f:
            f.write(data)


def ensure_import(src, mod="gateway"):
    if re.search(r"^\s*import " + mod + r"\b", src, re.M):
        return src, False
    lines = src.splitlines(keepends=True)
    idx = 0
    for i, ln in enumerate(lines[:80]):
        if ln.startswith("import ") or ln.startswith("from "):
            idx = i + 1
    lines.insert(idx, "import " + mod + "\n")
    return "".join(lines), True


def replace_class(src, classname, newblock):
    lines = src.splitlines(keepends=True)
    start = None
    for i, ln in enumerate(lines):
        if ln.startswith("class " + classname):
            start = i
            break
    if start is None:
        return src, False
    end = len(lines)
    for j in range(start + 1, len(lines)):
        s = lines[j]
        if s[:1] not in ("", " ", "\t", "\n", "#") and (
                s.startswith("class ") or s.startswith("def ") or s.startswith("@")):
            end = j
            break
    new = "".join(lines[:start]) + newblock.rstrip("\n") + "\n\n\n" + "".join(lines[end:])
    return new, True


def patch_file(name, fn):
    path = os.path.join(HERE, name)
    if not os.path.exists(path):
        print("  skip " + name + " (not found)")
        return
    with open(path) as f:
        src = f.read()
    new, changed = fn(src)
    if changed and new != src:
        backup(path)
        with open(path, "w") as f:
            f.write(new)
        print("  patched " + name)
    else:
        print("  ok " + name + " (already patched)")


def _use_resolved_rerank(src):
    if "resolve_model_id(config.RERANK_SLUG)" in src:
        return src
    return re.sub(r"config\.API_KEY,\s*config\.RERANK_SLUG\)",
                  "config.API_KEY, gateway.resolve_model_id(config.RERANK_SLUG))",
                  src)


def _use_resolved_embed(src):
    if "resolve_model_id(config.EMBED_SLUG" in src:
        return src
    return src.replace(
        "model=config.EMBED_MODEL",
        "model=gateway.resolve_model_id(config.EMBED_SLUG, config.EMBED_MODEL)")


def lex(src):
    src, a = ensure_import(src)
    src, b = replace_class(src, "HttpReranker", NEW_RERANKER)
    before = src
    src = _use_resolved_rerank(src)
    src = _use_resolved_embed(src)
    return src, (a or b or src != before)


def live(src):
    src, a = ensure_import(src)
    before = src
    src = _use_resolved_rerank(src)
    return src, (a or src != before)


def emb(src):
    if "resolve_model_id(config.EMBED_SLUG" in src:
        return src, False
    src, a = ensure_import(src)
    before = src
    src = _use_resolved_embed(src)
    return src, (a or src != before)


def main():
    gp = os.path.join(HERE, "gateway.py")
    if not os.path.exists(gp):
        with open(gp, "w") as f:
            f.write(GATEWAY_PY)
        print("  wrote gateway.py")
    else:
        print("  ok gateway.py (exists)")
    patch_file("lexical_search.py", lex)
    patch_file("livecheck.py", live)
    patch_file("embedder.py", emb)
    print("\nDone. Now run:  python3 cli.py fixtures/payments livecheck")
    return 0


if __name__ == "__main__":
    sys.exit(main())
