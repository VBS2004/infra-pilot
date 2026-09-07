"""
Offline validation of the LIVE HTTP path (embeddings + rerank) by monkeypatching
urllib so we exercise request building + response parsing without a network.
"""
import os
import sys
import json
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

import config

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
    from embedder import make_embedder
    emb = make_embedder()
    check("http embedder constructed", emb is not None)
    v = emb.encode(["abc", "abcd"])
    check("embeddings shape", hasattr(v, "shape") and v.shape == (2, 3))
    check("embeddings url", _LAST["url"] == "http://fake-gw/jina-embeddings-v3/v1/embeddings")
    check("bearer auth sent", _LAST["auth"] == "Bearer secret-key")
    check("model in body", _LAST["body"]["model"] == config.EMBED_MODEL)


def test_reranker_http_path():
    urllib.request.urlopen = _fake_urlopen
    from lexical_search import HttpReranker

    class C:
        def __init__(self, t):
            self.text = t

    rr = HttpReranker(config.endpoint(config.RERANK_SLUG), "secret-key", config.RERANK_SLUG)
    docs = [C("a"), C("b"), C("c")]
    order = rr("q", docs)
    check("rerank url", _LAST["url"].endswith("/qwen3-reranker-4b-svc/v1/rerank"))
    check("rerank returns all", sorted(i for i, _ in order) == [0, 1, 2])
    check("rerank honors server order", order[0][0] == 2 and order[-1][0] == 0)


if __name__ == "__main__":
    for fn in [test_embedder_http_path, test_reranker_http_path]:
        print(f"\n# {fn.__name__}")
        fn()
    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)
