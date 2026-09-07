"""
Tests for the borrowed-from-legacy_coder plumbing:
storage abstraction, file cache, ripgrep scan, embedding persistence +
incremental re-embed, dense retrieval wiring, singleton/on_file_changed,
and the agent tool wrappers. All run offline with a FakeEmbedder.
"""
import os
import sys
import tempfile
import time
import hashlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

import storage
from storage import LocalDiskStorage, set_storage
from file_cache import FileCache
from repo_scan import list_hcl_files
from persistence import EmbeddingStore, repo_namespace, content_hash
import index as index_mod
from index import TerraPilotIndex, get_index, reset_index
from lexical_search import HybridRetriever, EmbeddingDense, build_hybrid
import agent_tools

REPO = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "payments")

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


class FakeEmbedder:
    """Deterministic hashing-vectorizer so cosine reflects token overlap."""
    DIM = 64

    def encode(self, texts, **_):
        single = isinstance(texts, str)
        items = [texts] if single else list(texts)
        out = np.zeros((len(items), self.DIM), dtype=np.float32)
        for r, t in enumerate(items):
            for tok in t.lower().replace(".", " ").replace("_", " ").split():
                h = int(hashlib.md5(tok.encode()).hexdigest(), 16) % self.DIM
                out[r, h] += 1.0
        return out[0] if single else out


def test_storage():
    with tempfile.TemporaryDirectory() as d:
        s = LocalDiskStorage(d)
        check("storage missing -> None", s.read_bytes("a/b.bin") is None)
        s.write_bytes("a/b.bin", b"hello")
        check("storage round-trip", s.read_bytes("a/b.bin") == b"hello")
        check("storage exists", s.exists("a/b.bin"))
        s.write_text("a/c.txt", "world")
        check("storage text", s.read_text("a/c.txt") == "world")
        check("storage list", "a/b.bin" in s.list_keys() and "a/c.txt" in s.list_keys())
        s.delete("a/b.bin")
        check("storage delete", not s.exists("a/b.bin"))


def test_file_cache():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "f.tf")
        with open(p, "w") as f:
            f.write("v1")
        fc = FileCache()
        check("cache miss first", fc.get(p) is None)
        fc.set(p, "v1")
        check("cache hit", fc.get(p) == "v1")
        time.sleep(0.01)
        with open(p, "w") as f:
            f.write("v2")
        os.utime(p, None)
        check("cache mtime invalidation", fc.get(p) is None)
        fc.set(p, "v2")
        fc.invalidate(p)
        check("cache manual invalidate", fc.get(p) is None)


def test_repo_scan():
    files = list_hcl_files(REPO)
    check("scan finds files", len(files) > 0)
    check("scan only hcl", all(f.endswith((".tf", ".hcl", ".tfvars")) for f in files))


def test_persistence_incremental():
    with tempfile.TemporaryDirectory() as d:
        st = EmbeddingStore(LocalDiskStorage(d), "repos/x")
        check("persist empty load", st.load() is None)
        vecs = np.arange(12, dtype=np.float32).reshape(3, 4)
        st.save({"model": "m", "ids": ["a", "b", "c"],
                 "hashes": ["h1", "h2", "h3"]}, vecs)
        loaded = st.load()
        check("persist reload meta", loaded is not None and loaded[0]["ids"] == ["a", "b", "c"])
        check("persist reload vecs", np.allclose(loaded[1], vecs))


def test_dense_incremental_and_search():
    with tempfile.TemporaryDirectory() as d:
        idx = TerraPilotIndex(REPO).build()
        hr = HybridRetriever(idx)
        store = EmbeddingStore(LocalDiskStorage(d), repo_namespace(REPO))
        emb = FakeEmbedder()
        dense = EmbeddingDense(emb, store, model="fake").build(hr.chunks)
        check("dense ready", dense.ready)
        check("dense persisted", store.load() is not None)
        # second build should reuse all (no re-embed) -> same vectors
        v1 = dense._vectors.copy()
        dense2 = EmbeddingDense(emb, store, model="fake").build(hr.chunks)
        check("dense incremental reuse", np.allclose(v1, dense2._vectors))
        # wire dense into hybrid and search for an ec2 concept
        hr.dense = dense
        res = hr.search("aws instance ec2", top_k=5)
        check("hybrid+dense returns results", len(res) > 0)
        files = {r.get("file") for r in res}
        check("hybrid surfaces ec2 module", any("ec2" in (f or "") for f in files))


def test_singleton_and_change_hook():
    reset_index()
    a = get_index(REPO)
    b = get_index(REPO)
    check("singleton identity", a is b)
    n_before = len(a.files)
    a.on_file_changed(os.path.join(REPO, a.files[0]))
    check("change hook keeps index consistent", len(a.files) == n_before)


def test_agent_tools_offline():
    # offline: embedder is None -> BM25-only, tools must still work
    set_storage(LocalDiskStorage(tempfile.mkdtemp()))
    out = agent_tools.tf_find(REPO, "aws_instance")
    check("tf_find string", isinstance(out, str) and "aws_instance" in out)
    out2 = agent_tools.tf_catalog(REPO)
    check("tf_catalog string", isinstance(out2, str) and "module" in out2.lower())
    out3 = agent_tools.tf_search(REPO, "security group")
    check("tf_search string", isinstance(out3, str))
    tools = agent_tools.get_tools()
    check("get_tools returns callables", len(tools) == 6)


if __name__ == "__main__":
    for fn in [test_storage, test_file_cache, test_repo_scan,
               test_persistence_incremental, test_dense_incremental_and_search,
               test_singleton_and_change_hook, test_agent_tools_offline]:
        print(f"\n# {fn.__name__}")
        fn()
    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)
