"""livecheck.py - live connectivity check against the LLM gateway.

    export OPENAI_API_KEY=<your-api-key>
    export LLM_GATEWAY_BASE=https://api.deepseek.com   # default
    export LLM_GEN_MODEL=deepseek-chat                 # default
    python cli.py fixtures/payments livecheck
"""
from __future__ import annotations

import sys
import urllib.request

import config
from index import TerraPilotIndex
from lexical_search import build_hybrid


class _Doc:
    def __init__(self, text: str):
        self.text = text


def _get(url: str, key: str):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=30) as r:
        return getattr(r, "status", 200)


def main(repo: str) -> int:
    print(f"gateway     : {config.GATEWAY_BASE or '(unset)'}")
    print(f"model       : {config.GEN_MODEL}")
    print(f"configured  : {config.is_configured()}")
    if not config.is_configured():
        print("\nNot configured. Set:")
        print("  $env:OPENAI_API_KEY  = 'sk-...'")
        print("  $env:LLM_GATEWAY_BASE = 'https://api.deepseek.com'   # or your gateway")
        print("  $env:LLM_GEN_MODEL    = 'deepseek-chat'")
        return 1

    print("\n== /v1/models ==")
    try:
        st = _get(config.models_url(), config.API_KEY)
        print(f"  GET /v1/models -> {st}")
    except Exception as e:
        print(f"  ERROR {e}")

    print("\n== chat completion (smoke test) ==")
    import generator
    try:
        reply = generator.complete(
            [{"role": "user", "content": "Reply with the single word: ok"}],
            max_tokens=8,
        )
        print(f"  ok -> {reply!r}")
    except Exception as e:
        print(f"  ERROR {e}")

    print("\n== embeddings ==")
    if not config.EMBED_ENABLED:
        print("  disabled (LLM_EMBED_ENABLED=0)")
    else:
        from embedder import make_embedder
        emb = make_embedder()
        try:
            v = emb.encode(["aws_instance ec2 compute module", "security group ingress rules"])
            shape = getattr(v, "shape", None) or (len(v), len(v[0]))
            print(f"  ok -> shape {shape}")
        except Exception as e:
            print(f"  ERROR {e}")

    print("\n== hybrid search ==")
    idx = TerraPilotIndex(repo).build()
    hr = build_hybrid(idx)
    print(f"  dense wired   : {hr.dense is not None}")
    print(f"  reranker wired: {hr.reranker is not None}")
    for r in hr.search("create a bastion ec2 similar to existing projects", top_k=5):
        if "chunk_id" in r:
            print(f"    [{r['reason']}] {r['kind']} {r['symbol']}  {r['file']}")
    return 0


if __name__ == "__main__":
    repo = sys.argv[1] if len(sys.argv) > 1 else "fixtures/payments"
    sys.exit(main(repo))
