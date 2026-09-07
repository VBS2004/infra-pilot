"""Embedding clients with a backend toggle + graceful degradation.

Borrowed almost verbatim from legacy_coder (semantic.HTTPEmbedder): an
OpenAI-compatible /embeddings client whose .encode() is interchangeable with
sentence-transformers. This IS our Jina-v3 hook -- point LLM_GATEWAY_BASE +
LLM_EMBED_SLUG at the gateway and it speaks to jina-embeddings-v3.

Difference from legacy_coder: uses stdlib urllib (no `requests` dependency) so the
tool stays zero-dep on the lexical path.

Concurrency + resilience:
  - batch POSTs run through a thread pool (EMBED_CONCURRENCY, default 8).
  - each request retries on transient network/SSL errors
    (EMBED_RETRIES, default 3) with exponential backoff (EMBED_BACKOFF).
  - completed batches are checkpointed to disk keyed by (model, content-hash),
    so a crash / Ctrl-C / gateway timeout does NOT lose finished work -- re-run
    resumes and only embeds what's missing. Cache dir: EMBED_CACHE_DIR
    (default ~/.payments-indexer/embed_cache); disable with EMBED_CACHE=0.
  - identical texts are embedded once (dedup by content hash).

make_embedder() returns:
  - HTTPEmbedder        when the gateway is configured (backend http/auto)
  - a local ST wrapper  when sentence-transformers is installed (backend auto/st)
  - None                otherwise -> callers degrade to BM25-only retrieval
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

import gateway


def _env_int(name: str, default: int) -> int:
    try:
        raw = (os.environ.get(name) or "").strip()
        return max(0, int(raw)) if raw else default
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        raw = (os.environ.get(name) or "").strip()
        return float(raw) if raw else default
    except Exception:
        return default


def _env_flag(name: str, default: bool = True) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if raw == "":
        return default
    return raw not in ("0", "false", "no", "off")


class HTTPEmbedder:
    """OpenAI-compatible /embeddings client (Jina-v3 via the Acme gateway)."""

    def __init__(self, model: str, base_url: str, api_key: str,
                 batch: int = 16, timeout: int = 60,
                 concurrency: Optional[int] = None,
                 progress: Optional[bool] = None,
                 retries: Optional[int] = None,
                 cache_dir: Optional[str] = None):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.batch = max(1, int(batch))
        self.timeout = timeout
        self.concurrency = (concurrency if (concurrency and concurrency > 0)
                            else _env_int("EMBED_CONCURRENCY", 8)) or 1
        if progress is None:
            progress = _env_flag("EMBED_PROGRESS", True)
        self.progress = progress
        self.retries = (retries if retries is not None
                        else _env_int("EMBED_RETRIES", 3))
        self.backoff = _env_float("EMBED_BACKOFF", 1.0)
        self.url = self.base_url + "/embeddings"

        self._cache_enabled = _env_flag("EMBED_CACHE", True)
        cdir = (cache_dir or os.environ.get("EMBED_CACHE_DIR")
                or os.path.expanduser("~/.payments-indexer/embed_cache"))
        self._cache_dir = cdir
        tag = hashlib.sha1(("%s|%s" % (self.model, self.base_url)).encode("utf-8")).hexdigest()[:12]
        self._cache_path = os.path.join(cdir, "emb_" + tag + ".jsonl")
        self._cache: Optional[Dict[str, Any]] = None
        self._lock = threading.Lock()

    # ---- HTTP (with retry/backoff) ----
    def _post(self, texts: List[str]) -> List[List[float]]:
        body = json.dumps({"model": self.model, "input": texts}).encode("utf-8")
        last: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            try:
                req = urllib.request.Request(
                    self.url, data=body,
                    headers={"Authorization": "Bearer " + self.api_key,
                             "Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                rows = sorted(data["data"], key=lambda r: r.get("index", 0))
                return [r["embedding"] for r in rows]
            except Exception as e:
                last = e
                if attempt < self.retries:
                    delay = min(self.backoff * (2 ** attempt), 20.0)
                    time.sleep(delay + random.uniform(0, max(self.backoff, 0.0)))
                    continue
                raise
        raise last  # pragma: no cover

    # ---- disk checkpoint cache ----
    @staticmethod
    def _np():
        import numpy as np
        return np

    def _text_hash(self, text: str) -> str:
        return hashlib.sha1((self.model + "\x00" + text).encode("utf-8")).hexdigest()

    def _get_cache(self) -> Dict[str, Any]:
        if self._cache is not None:
            return self._cache
        cache: Dict[str, Any] = {}
        if self._cache_enabled:
            try:
                os.makedirs(self._cache_dir, exist_ok=True)
            except Exception:
                self._cache_enabled = False
            if self._cache_enabled and os.path.exists(self._cache_path):
                cache = self._load_cache_file()
        self._cache = cache
        return cache

    def _load_cache_file(self) -> Dict[str, Any]:
        np = self._np()
        out: Dict[str, Any] = {}
        try:
            with open(self._cache_path, "r", encoding="ascii") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        h, b64 = line.split(" ", 1)
                        out[h] = np.frombuffer(base64.b64decode(b64), dtype=np.float32)
                    except Exception:
                        continue
        except Exception:
            return {}
        return out

    def _store_batch(self, pairs, vecs, cache) -> None:
        np = self._np()
        lines = []
        for (h, _t), v in zip(pairs, vecs):
            arr = np.asarray(v, dtype=np.float32)
            cache[h] = arr
            if self._cache_enabled:
                lines.append(h + " " + base64.b64encode(arr.tobytes()).decode("ascii"))
        if lines and self._cache_enabled:
            with self._lock:
                try:
                    with open(self._cache_path, "a", encoding="ascii") as f:
                        f.write("\n".join(lines) + "\n")
                        f.flush()
                except Exception:
                    pass

    # ---- progress ----
    def _log(self, done: int, total: int) -> None:
        if self.progress and total > 1:
            sys.stderr.write("\r[embed] %d/%d batches" % (done, total))
            sys.stderr.flush()
            if done >= total:
                sys.stderr.write("\n")
                sys.stderr.flush()

    # ---- public API ----
    def encode(self, texts: Any, **_: Any) -> Any:
        single = isinstance(texts, str)
        items = [texts] if single else list(texts)
        if not items:
            return self._to_array([], single, empty=True)

        cache = self._get_cache()
        hashes = [self._text_hash(t) for t in items]

        missing: Dict[str, str] = {}
        for h, t in zip(hashes, items):
            if h not in cache and h not in missing:
                missing[h] = t
        if missing:
            self._embed_missing(list(missing.items()), cache)

        vecs = [cache[h] for h in hashes]
        return self._to_array(vecs, single)

    def _embed_missing(self, missing_pairs, cache) -> None:
        batch = self.batch
        batches = [missing_pairs[i:i + batch]
                   for i in range(0, len(missing_pairs), batch)]
        total = len(batches)
        workers = min(self.concurrency, total)
        done = 0

        if workers <= 1:
            for pairs in batches:
                vecs = self._post([t for (_h, t) in pairs])
                self._store_batch(pairs, vecs, cache)
                done += 1
                self._log(done, total)
            return

        ex = ThreadPoolExecutor(max_workers=workers)
        futs = {ex.submit(self._post, [t for (_h, t) in pairs]): bi
                for bi, pairs in enumerate(batches)}
        seen = set()
        err: Optional[Exception] = None
        try:
            for fut in as_completed(futs):
                bi = futs[fut]
                try:
                    vecs = fut.result()
                except Exception as e:
                    err = e
                    break
                self._store_batch(batches[bi], vecs, cache)
                seen.add(bi)
                done += 1
                self._log(done, total)
        finally:
            # flush any already-finished successful batches we didn't consume,
            # so a mid-run failure never discards completed work
            for f, bi in futs.items():
                if bi not in seen and f.done() and not f.cancelled() and f.exception() is None:
                    self._store_batch(batches[bi], f.result(), cache)
                    seen.add(bi)
            ex.shutdown(wait=False, cancel_futures=True)
        if err is not None:
            raise err

    @staticmethod
    def _to_array(vecs: Any, single: bool, empty: bool = False) -> Any:
        try:
            import numpy as np
            arr = np.asarray(vecs, dtype=np.float32)
            if empty:
                return arr
            return arr[0] if single else arr
        except Exception:
            if empty:
                return vecs
            return vecs[0] if single else vecs


class _LocalST:
    """Thin wrapper around sentence-transformers, .encode() compatible."""

    def __init__(self, model):
        self._m = model

    def encode(self, texts: Any, **kw: Any) -> Any:
        return self._m.encode(texts, **kw)


def make_embedder() -> Optional[object]:
    import config
    backend = (config.EMBED_BACKEND or "auto").lower()
    if backend == "none":
        return None
    if backend in ("auto", "http"):
        base = config.endpoint(config.EMBED_SLUG)
        if base and config.API_KEY:
            model_id = gateway.resolve_model_id(config.EMBED_SLUG, config.EMBED_MODEL)
            return HTTPEmbedder(model_id, base, config.API_KEY,
                                batch=config.EMBED_BATCH, timeout=config.EMBED_TIMEOUT,
                                concurrency=getattr(config, "EMBED_CONCURRENCY", None))
        if backend == "http":
            return None  # explicitly http but not configured
    if backend in ("auto", "sentence_transformers"):
        try:
            from sentence_transformers import SentenceTransformer
            return _LocalST(SentenceTransformer(config.LOCAL_EMBED_MODEL))
        except Exception:
            return None
    return None
