"""
Offline validation of the LIVE HTTP path (embeddings + rerank) by monkeypatching
urllib so we exercise request building + response parsing without a network.
"""
import os
import sys
import json
import tempfile
import urllib.request

# Isolate the embedding disk cache: a warm cache skips the HTTP call these tests
# assert on, which made the suite pass once and fail on every later run.
import atexit
import shutil
_cache = tempfile.mkdtemp(prefix="tp_embed_cache_")
atexit.register(shutil.rmtree, _cache, ignore_errors=True)
os.environ["EMBED_CACHE_DIR"] = _cache
for _v in ("LLM_RERANK_GATEWAY_BASE", "LLM_RERANK_ENABLED", "LLM_EMBED_GATEWAY_BASE"):
    os.environ.pop(_v, None)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import numpy as np

from terra_pilot.core import config

_passed = 0
_failed = 0


def check(name, cond):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {name}")
    else:
        _failed += 1
        print(f"  FAIL {name}")


class _FakeResp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode("utf-8")
        self.status = 200

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_LAST = {}


def _fake_urlopen(req, timeout=None):
    url = req.full_url
    body = json.loads(req.data.decode("utf-8")) if req.data else {}
    _LAST["url"] = url
    _LAST["body"] = body
    _LAST["auth"] = req.headers.get("Authorization")
    if url.endswith("/embeddings"):
        texts = body["input"]
        data = [{"index": i, "embedding": [float(len(t)), 1.0, 2.0]} for i, t in enumerate(texts)]
        return _FakeResp({"data": data})
    if url.endswith("/rerank"):
        # rank documents in reverse to prove we honor server order
        n = len(body["documents"])
        results = [{"index": n - 1 - i, "relevance_score": 1.0 - i * 0.1} for i in range(n)]
        return _FakeResp({"results": results})
    raise AssertionError(f"unexpected url {url}")


def test_embedder_http_path():
    config.GATEWAY_BASE = "http://fake-gw"
    config.API_KEY = "secret-key"
    config.EMBED_BACKEND = "http"
    urllib.request.urlopen = _fake_urlopen
    from terra_pilot.llm.embedder import make_embedder
    emb = make_embedder()
    check("http embedder constructed", emb is not None)
    v = emb.encode(["abc", "abcd"])
    check("embeddings shape", hasattr(v, "shape") and v.shape == (2, 3))
    check("embeddings url", _LAST["url"] == "http://fake-gw/v1/embeddings")
    check("bearer auth sent", _LAST["auth"] == "Bearer secret-key")
    check("model in body", _LAST["body"]["model"] == config.EMBED_MODEL)


def test_reranker_http_path():
    urllib.request.urlopen = _fake_urlopen
    from terra_pilot.search.lexical_search import HttpReranker

    class C:
        def __init__(self, t):
            self.text = t

    rr = HttpReranker(config.endpoint(config.RERANK_SLUG), "secret-key", config.RERANK_SLUG)
    docs = [C("a"), C("b"), C("c")]
    order = rr("q", docs)
    check("rerank url", _LAST["url"].endswith("/v1/rerank"))
    check("rerank returns all", sorted(i for i, _ in order) == [0, 1, 2])
    check("rerank honors server order", order[0][0] == 2 and order[-1][0] == 0)


def test_rerank_only_when_configured():
    """Rerank must never fall back to the generation gateway."""
    config.GATEWAY_BASE = "http://deepseek.invalid"
    check("rerank off with only a generation gateway", config.rerank_enabled() is False)
    check("rerank url empty when unset", config.rerank_url() == "")
    os.environ["LLM_RERANK_GATEWAY_BASE"] = "http://rr:8080"
    try:
        check("rerank on once a rerank base is set", config.rerank_enabled() is True)
        check("rerank url built from its own base", config.rerank_url() == "http://rr:8080/v1/rerank")
        os.environ["LLM_RERANK_ENABLED"] = "0"
        check("LLM_RERANK_ENABLED=0 force-disables", config.rerank_enabled() is False)
    finally:
        os.environ.pop("LLM_RERANK_GATEWAY_BASE", None)
        os.environ.pop("LLM_RERANK_ENABLED", None)


def test_build_hybrid_makes_no_rerank_calls_by_default():
    from terra_pilot.search.index import TerraPilotIndex
    from terra_pilot.search import lexical_search
    from terra_pilot.llm import embedder as emb_mod

    calls = []
    real = urllib.request.urlopen
    urllib.request.urlopen = lambda req, timeout=None: calls.append(req.full_url) or _fake_urlopen(req, timeout)
    saved = (config.EMBED_ENABLED, config.EMBED_BACKEND, emb_mod.make_embedder)
    config.EMBED_ENABLED = True
    config.EMBED_BACKEND = "http"
    config.GATEWAY_BASE = "http://fake-gw"
    os.environ["LLM_EMBED_GATEWAY_BASE"] = "http://fake-gw"
    try:
        root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "fixtures", "myrepo")
        hr = lexical_search.build_hybrid(TerraPilotIndex(root).build(), persist=False)
        check("no /rerank request without a rerank server",
              not any("rerank" in u or "/score" in u for u in calls))
        check("no reranker wired", hr.reranker is None)
    finally:
        urllib.request.urlopen = real
        config.EMBED_ENABLED, config.EMBED_BACKEND, emb_mod.make_embedder = saved
        os.environ.pop("LLM_EMBED_GATEWAY_BASE", None)


def test_embed_auto_ignores_generation_gateway():
    """`auto` must not probe the generation gateway for /embeddings."""
    import types
    config.GATEWAY_BASE = "http://deepseek.invalid"
    config.API_KEY = "k"
    config.EMBED_BACKEND = "auto"
    os.environ.pop("LLM_EMBED_GATEWAY_BASE", None)
    fake_st = types.ModuleType("sentence_transformers")

    def _boom(*a, **k):
        raise RuntimeError("no local model")
    fake_st.SentenceTransformer = _boom
    saved = sys.modules.get("sentence_transformers")
    sys.modules["sentence_transformers"] = fake_st
    try:
        from terra_pilot.llm.embedder import make_embedder, HTTPEmbedder
        e = make_embedder()
        check("auto without embed base is not HTTP", not isinstance(e, HTTPEmbedder))
        os.environ["LLM_EMBED_GATEWAY_BASE"] = "http://emb:8000"
        e = make_embedder()
        check("auto with embed base uses HTTP", isinstance(e, HTTPEmbedder))
        check("embed base is used", e.base_url == "http://emb:8000/v1")
    finally:
        os.environ.pop("LLM_EMBED_GATEWAY_BASE", None)
        if saved is None:
            sys.modules.pop("sentence_transformers", None)
        else:
            sys.modules["sentence_transformers"] = saved


if __name__ == "__main__":
    for fn in [test_embedder_http_path, test_reranker_http_path,
               test_rerank_only_when_configured,
               test_build_hybrid_makes_no_rerank_calls_by_default,
               test_embed_auto_ignores_generation_gateway]:
        print(f"\n# {fn.__name__}")
        fn()
    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)
