"""Shared plumbing for the script-style tests: env isolation, check(), fixtures.

Import this BEFORE terra_pilot so the env vars below are set when
`terra_pilot.core.config` reads them at import time.
"""
import atexit
import contextlib
import io
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURES = os.path.join(ROOT, "fixtures")

# Offline, hermetic defaults: stdlib parser, no embedder, no reranker, private caches.
# Every temp dir any test creates is removed at exit (fixtures, fake bins, tiny models,
# Spark scratch): /tmp is often a small tmpfs and leaked dirs once filled it.
_real_mkdtemp = tempfile.mkdtemp


def _tracked_mkdtemp(*a, **k):
    d = _real_mkdtemp(*a, **k)
    atexit.register(shutil.rmtree, d, ignore_errors=True)
    return d


tempfile.mkdtemp = _tracked_mkdtemp

_SCRATCH = [tempfile.mkdtemp(prefix="tp_embed_"), tempfile.mkdtemp(prefix="tp_store_")]
for _d in _SCRATCH:
    atexit.register(shutil.rmtree, _d, ignore_errors=True)
os.environ.update({
    "FORCE_PY_PARSER": "1",
    "LLM_EMBED_BACKEND": "none",
    "LLM_EMBED_ENABLED": "0",
    "EMBED_CACHE_DIR": _SCRATCH[0],
    "STORAGE_DIR": _SCRATCH[1],
})
for _v in ("LLM_RERANK_GATEWAY_BASE", "LLM_RERANK_ENABLED", "LLM_EMBED_GATEWAY_BASE",
           "LOCAL_MODEL"):
    os.environ.pop(_v, None)
sys.path.insert(0, HERE)
if not any(p.endswith("src") for p in sys.path):
    sys.path.insert(0, os.path.join(ROOT, "src"))

_passed = _failed = 0


def check(name, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ok   {name}")
    else:
        _failed += 1
        print(f"  FAIL {name}  {detail}")


def finish():
    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)


def copy_fixture(name):
    """Copy fixtures/<name> to a temp dir so --apply never touches the checkout."""
    base = tempfile.mkdtemp(prefix="tp_repo_")
    atexit.register(shutil.rmtree, base, ignore_errors=True)
    dst = os.path.join(base, name)
    shutil.copytree(os.path.join(FIXTURES, name), dst)
    return dst


def point_at(gateway):
    """Aim the generator at a FakeGateway (module attrs are read at call time)."""
    from terra_pilot.core import config
    from terra_pilot.llm import gateway as gw_mod
    config.GATEWAY_BASE = gateway.base
    config.API_KEY = "test-key"
    gw_mod.clear_cache()


@contextlib.contextmanager
def captured():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield buf


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()
