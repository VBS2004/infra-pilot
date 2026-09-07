"""Hybrid-retrieval scaffolding for the Acme Terraform Assistant.

Lexical (BM25) + Reciprocal Rank Fusion + a graph-expansion step that pulls a
chunk's dependencies (variables.tf, upstream wiring) so retrieval never strands
a resource from what it needs.

Chunking is HCL-block-granular (cAST style): one chunk per top-level block.
PATCH (21 Jun 2026): only resource/module/data blocks are primary chunks --
lone variable/output/locals blocks are low-signal and reach the generator via
_expand. Each chunk carries a contextual header so dense + rerank can tell
near-identical bodies apart; the BM25 corpus includes the file path.

PATCH (22 Jun 2026) -- COMPONENT INSTANCES (the billing miss):
Terragrunt component dirs (e.g. .../billing/pre-prod/ecs/) hold their config as
`inputs = { ... }` in inputs.hcl / *.tfvars. That is an ATTRIBUTE, not a block,
so the parser emits no symbol and _build_chunks produced ZERO chunks for them --
so `search "... similar to pre-prod billing"` could never retrieve billing.
Fix: synthesize a `component` chunk for every inputs.hcl / *.tfvars that has no
primary block, so the precedent enters the BM25 (+ dense) corpus. Each file is
split into bounded line windows (CHUNK_MAX_CHARS, default 6000) so a large
inputs.hcl is NEVER sent to the embedder oversized -- this is the real cure for
the Jina-v3 8192-token HTTP 400, with no truncation / data loss. The same
windowing now guards oversized primary blocks too. terragrunt.hcl is left alone
(its include/dependency blocks are boilerplate, not a reusable precedent).
"""
from __future__ import annotations

import math
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from index import TerraPilotIndex, Symbol
import gateway


@dataclass
class Chunk:
    chunk_id: str
    file: str
    symbol: str
    kind: str
    text: str
    line_start: int
    line_end: int
    type_hint: str = ""      # module type a component instantiates (e.g. "ecs")
    source_dir: str = ""    # resolved .tf module dir the component refers to


_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[.\-]")


def tokenize_code(s: str) -> List[str]:
    toks: List[str] = []
    for m in re.finditer(r"[A-Za-z0-9_]+", s):
        w = m.group(0).lower()
        toks.append(w)
        toks.extend(p for p in w.split("_") if p)
    return toks


PRIMARY_KINDS = set(
    k.strip() for k in (os.environ.get("CHUNK_PRIMARY_KINDS")
                        or "resource,module,data").split(",") if k.strip())

# Max characters per chunk of embed text. A whole inputs.hcl can blow past
# Jina v3's 8192-token sequence limit and HTTP 400 the batch, so we window long
# files/blocks into bounded sub-chunks instead of truncating (no data loss).
MAX_CHUNK_CHARS = int(os.environ.get("CHUNK_MAX_CHARS", "6000") or "6000")

_QUERY_STOPWORDS = {
    "create", "make", "build", "add", "want", "need", "new", "similar",
    "like", "please", "the", "a", "an", "for", "to", "of", "in", "on",
    "with", "and", "me", "my", "our", "give", "set", "up", "setup", "some",
}


def _is_component_file(rel: str) -> bool:
    """Terragrunt component-instance config: holds `inputs = {...}` / tfvars,
    not HCL blocks. terragrunt.hcl is intentionally excluded."""
    base = os.path.basename(rel)
    return base == "inputs.hcl" or rel.endswith(".tfvars")


def _window_lines(lines: List[str], max_chars: int) -> List[str]:
    """Group lines into windows each <= max_chars (every sub-chunk stays under
    the embedder limit, with zero truncation)."""
    max_chars = max(500, max_chars)
    windows: List[str] = []
    cur: List[str] = []
    cur_len = 0
    for ln in lines:
        add = len(ln) + 1
        if cur and cur_len + add > max_chars:
            windows.append("\n".join(cur))
            cur, cur_len = [], 0
        cur.append(ln)
        cur_len += add
    if cur:
        windows.append("\n".join(cur))
    return windows or [""]


class _StdlibBM25:
    def __init__(self, corpus_tokens: List[List[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.docs = corpus_tokens
        self.N = len(corpus_tokens)
        self.avgdl = sum(len(d) for d in corpus_tokens) / max(1, self.N)
        self.df: Dict[str, int] = defaultdict(int)
        self.tf: List[Dict[str, int]] = []
        for d in corpus_tokens:
            seen = set()
            counts: Dict[str, int] = defaultdict(int)
            for t in d:
                counts[t] += 1
                if t not in seen:
                    self.df[t] += 1
                    seen.add(t)
            self.tf.append(counts)
        self.idf = {
            t: math.log(1 + (self.N - n + 0.5) / (n + 0.5))
            for t, n in self.df.items()
        }

    def get_scores(self, query_tokens: List[str]) -> List[float]:
        scores = [0.0] * self.N
        for i, counts in enumerate(self.tf):
            dl = len(self.docs[i])
            s = 0.0
            for t in query_tokens:
                if t not in counts:
                    continue
                idf = self.idf.get(t, 0.0)
                freq = counts[t]
                denom = freq + self.k1 * (1 - self.b + self.b * dl / self.avgdl)
                s += idf * freq * (self.k1 + 1) / denom
            scores[i] = s
        return scores


def _make_bm25(corpus_tokens: List[List[str]]):
    try:
        from rank_bm25 import BM25Okapi
        return BM25Okapi(corpus_tokens)
    except Exception:
        return _StdlibBM25(corpus_tokens)


DenseRetriever = Callable[[str, List[Chunk], int], List[Tuple[int, float]]]
Reranker = Callable[[str, List[Chunk]], List[Tuple[int, float]]]


def reciprocal_rank_fusion(
    ranked_lists: List[List[int]], k: int = 60) -> List[Tuple[int, float]]:
    """RRF over several ranked lists of chunk indices."""
    scores: Dict[int, float] = defaultdict(float)
    for lst in ranked_lists:
        for rank, idx in enumerate(lst):
            scores[idx] += 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)


class HybridRetriever:
    def __init__(self, idx: TerraPilotIndex,
                 dense: Optional[DenseRetriever] = None,
                 reranker: Optional[Reranker] = None):
        self.idx = idx
        self.dense = dense
        self.reranker = reranker
        self.chunks: List[Chunk] = []
        self._bm25 = None
        self._build_chunks()

    def _build_chunks(self) -> None:
        self.chunks = []
        for rel in self.idx.files:
            info = self.idx._index[rel]
            lines = info.text.splitlines()
            produced = False
            for s in info.symbols:
                if s.parent is not None:
                    continue  # one chunk per top-level block
                if s.kind not in PRIMARY_KINDS:
                    continue  # lone variable/output/locals -> pulled via _expand
                body_lines = lines[s.line_start - 1:s.line_end]
                header = "# %s %s | module: %s | file: %s" % (
                    s.kind, s.name, os.path.dirname(rel) or ".", rel)
                # window the body so an oversized block never 400s the embedder;
                # the header rides on every sub-chunk to keep context for rerank.
                windows = _window_lines(body_lines, MAX_CHUNK_CHARS - len(header) - 1)
                for pi, win in enumerate(windows):
                    suffix = "" if pi == 0 else "::part%d" % pi
                    self.chunks.append(Chunk(
                        chunk_id="%s::%s%s" % (rel, s.name, suffix),
                        file=rel, symbol=s.name, kind=s.kind,
                        text=header + "\n" + win,
                        line_start=s.line_start, line_end=s.line_end))
                produced = True
            # component instances (inputs.hcl / *.tfvars) carry `inputs = {...}`,
            # which is an attribute -> no primary block exists -> synthesize a
            # bounded `component` chunk so the precedent is retrievable at all.
            if not produced and _is_component_file(rel):
                comp_dir = os.path.dirname(rel) or "."
                comp_name = comp_dir.rsplit("/", 1)[-1] if "/" in comp_dir else comp_dir
                # A terragrunt component keeps its module reference in the
                # SIBLING terragrunt.hcl (terraform { source = ".../ecs" }), not
                # in inputs.hcl. Resolve it so the component is typed (type: ecs
                # -> matches an "ecs" query even when the dir isn't named ecs)
                # and so _expand can pull the .tf template it instantiates.
                source_dir, type_hint = self._component_source(comp_dir)
                header = "# component %s | type: %s | file: %s" % (
                    comp_dir, type_hint or "?", rel)
                windows = _window_lines(lines, MAX_CHUNK_CHARS - len(header) - 1)
                for pi, win in enumerate(windows):
                    suffix = "" if pi == 0 else "::part%d" % pi
                    self.chunks.append(Chunk(
                        chunk_id="%s::component%s" % (rel, suffix),
                        file=rel, symbol=comp_name, kind="component",
                        text=header + "\n" + win,
                        line_start=1, line_end=len(lines) or 1,
                        type_hint=type_hint, source_dir=source_dir))
        corpus = [tokenize_code(c.file + " " + c.symbol + "\n" + c.text)
                  for c in self.chunks]
        self._bm25 = _make_bm25(corpus) if self.chunks else None

    def _component_source(self, comp_dir: str) -> Tuple[str, str]:
        """Resolve (module_dir_key, type) for a terragrunt component from its
        sibling terragrunt.hcl `terraform { source = ... }`. ('', '') if none."""
        tg = (comp_dir + "/terragrunt.hcl") if comp_dir else "terragrunt.hcl"
        try:
            imps = self.idx.get_imports(tg)
        except Exception:
            imps = []
        for imp in imps:
            if imp.edge_type == "module_source" and imp.module:
                return imp.module, imp.module.rsplit("/", 1)[-1]
        return "", ""

    def _query_terms(self, query: str) -> List[str]:
        """Distinctive query tokens (stopwords + 1-char noise removed)."""
        return [t for t in dict.fromkeys(tokenize_code(query))
                if t not in _QUERY_STOPWORDS and len(t) > 1]

    def _locality(self, i: int, q_terms: List[str]) -> float:
        """Fraction of distinctive query terms that appear in a chunk's path /
        type / source / symbol, by SUBSTRING containment. Reranker-independent.

        Substring (not exact-token) matching is deliberate: terragrunt dirs
        concatenate the env+project (e.g. `preproddrbilling`), which never
        tokenizes into `pre`/`prod`/`billing`. Containment lets `.../billing/
        preproddrbilling/ecs` match all of ecs+pre+prod+billing, so the
        precedent whose LOCATION matches the ask is lifted to the top -- which
        is precisely what a human means by "similar to pre-prod billing". Equal
        weight (no IDF): a project name that recurs across many components must
        not be penalised for being common."""
        if not q_terms:
            return 0.0
        c = self.chunks[i]
        hay = (c.file + " " + c.type_hint + " "
               + c.source_dir + " " + c.symbol).lower()
        hits = sum(1 for t in q_terms if t in hay)
        return hits / float(len(q_terms))

    def _bm25_rank(self, query: str, top: int) -> List[int]:
        if not self._bm25:
            return []
        q_toks = [t for t in tokenize_code(query) if t not in _QUERY_STOPWORDS]
        scores = self._bm25.get_scores(q_toks or tokenize_code(query))
        order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        return [i for i in order if scores[i] > 0][:top]

    def search(self, query: str, top_k: int = 8, candidates: int = 40,
               expand_graph: bool = True) -> List[Dict]:
        """Hybrid search: BM25 (+ optional dense) -> RRF -> optional rerank, then
        a final BLEND of rerank + RRF + an IDF-weighted path/type locality bonus,
        collapsed to ONE result per file. The blend stops a noisy cross-encoder
        from burying the precedent whose location matches the ask (the
        billing/ecs miss) and stops a large multi-part inputs.hcl from being
        diluted. Tune with RANK_W_{RERANK,RRF,PATH}."""
        q_terms = self._query_terms(query)
        ranked_lists = [self._bm25_rank(query, candidates)]
        if self.dense:
            dense_ranked = [i for i, _ in self.dense(query, self.chunks, candidates)]
            ranked_lists.append(dense_ranked)
        fused = reciprocal_rank_fusion(ranked_lists)
        fused_score = {i: s for i, s in fused}
        cand_idx = [i for i, _ in fused[:candidates]] or self._bm25_rank(query, candidates)

        rr_score: Dict[int, float] = {}
        if self.reranker and cand_idx:
            cand_chunks = [self.chunks[i] for i in cand_idx]
            reranked = self.reranker(query, cand_chunks)
            rr_score = {cand_idx[j]: sc for j, sc in reranked}

        def _norm(d: Dict[int, float]) -> Dict[int, float]:
            if not d:
                return {}
            vals = list(d.values())
            lo, hi = min(vals), max(vals)
            rng = (hi - lo) or 1.0
            return {k: (v - lo) / rng for k, v in d.items()}

        # RRF is min-maxed (raw values are tiny); reranker scores are already a
        # calibrated [0,1] relevance, so use them raw (min-maxing them would
        # over-amplify a noisy 0.49-0.69 spread into the full range).
        rrf_n = _norm({i: fused_score.get(i, 0.0) for i in cand_idx})
        # Path/locality is weighted enough that when the cross-encoder is
        # uncertain (scores bunched, as in the billing case) the exact env/
        # project/type match decides. All three are env-overridable.
        w_r = float(os.environ.get("RANK_W_RERANK", "0.45"))
        w_f = float(os.environ.get("RANK_W_RRF", "0.15"))
        w_p = float(os.environ.get("RANK_W_PATH", "0.40"))
        if not rr_score:                  # offline / rerank failed -> lexical+path
            w_r = 0.0
        blended: Dict[int, float] = {}
        loc_of: Dict[int, float] = {}
        for i in cand_idx:
            loc = self._locality(i, q_terms)
            loc_of[i] = loc
            blended[i] = (w_r * rr_score.get(i, 0.0)
                          + w_f * rrf_n.get(i, 0.0)
                          + w_p * loc)
        # collapse multiple windows/parts of the same file to its best chunk so a
        # large multi-part inputs.hcl is neither diluted nor crowds the results.
        best_by_file: Dict[str, int] = {}
        for i in cand_idx:
            f = self.chunks[i].file
            if f not in best_by_file or blended[i] > blended[best_by_file[f]]:
                best_by_file[f] = i
        ordered = sorted(best_by_file.values(), key=lambda i: blended[i], reverse=True)
        top = ordered[:top_k]
        results = []
        for i in top:
            r = self._as_result(i, "retrieved")
            r["rrf"] = round(fused_score.get(i, 0.0), 5)
            if rr_score:
                r["score"] = round(rr_score.get(i, 0.0), 5)
            r["locality"] = round(loc_of[i], 3)
            r["final"] = round(blended[i], 5)
            results.append(r)
        if expand_graph:
            results.extend(self._expand(top))
        return results

    def _expand(self, idxs: List[int]) -> List[Dict]:
        out: List[Dict] = []
        seen_files = {self.chunks[i].file for i in idxs}
        for i in idxs:
            ch = self.chunks[i]
            # a retrieved component instance -> pull the .tf MODULE TEMPLATE it
            # instantiates (resolved from its sibling terragrunt source), so the
            # generator sees the real resource definitions to copy, not just the
            # inputs precedent.
            if ch.source_dir:
                for f in self.idx.files:
                    if f.startswith(ch.source_dir + "/") and f.endswith(".tf"):
                        if f not in seen_files:
                            seen_files.add(f)
                            out.append({"reason": "dep:template",
                                        "module": ch.source_dir,
                                        "via": ch.chunk_id, "file": f})
            for imp in self.idx.get_imports(ch.file):
                tgt = None
                if imp.edge_type == "module_source" and imp.module:
                    for f in self.idx.files:
                        if f.startswith(imp.module + "/") and (
                            f.endswith("variables.tf") or f.endswith("outputs.tf")):
                            if f not in seen_files:
                                seen_files.add(f)
                                out.append({"reason": f"dep:{imp.edge_type}",
                                            "via": ch.chunk_id, "file": f})
                elif imp.edge_type == "var_ref":
                    tgt = self._find_var_def(ch.file, imp.name)
                    if tgt and tgt not in seen_files:
                        seen_files.add(tgt)
                        out.append({"reason": "dep:var_ref", "var": imp.name,
                                    "via": ch.chunk_id, "file": tgt})
        return out

    def _find_var_def(self, referrer_file: str, var_name: str) -> Optional[str]:
        d = os.path.dirname(referrer_file)
        for rel, s in self.idx.all_symbols():
            if s.kind == "variable" and s.name == var_name and os.path.dirname(rel) == d:
                return rel
        return None

    def _as_result(self, i: int, reason: str) -> Dict:
        c = self.chunks[i]
        r = {"reason": reason, "chunk_id": c.chunk_id, "file": c.file,
             "symbol": c.symbol, "kind": c.kind,
             "lines": [c.line_start, c.line_end]}
        if c.type_hint:
            r["type"] = c.type_hint
        return r


def _np():
    try:
        import numpy as np
        return np
    except Exception:
        return None


class EmbeddingDense:
    """DenseRetriever impl backed by an embedder + a persisted EmbeddingStore."""

    def __init__(self, embedder, store=None, model: str = ""):
        self.embedder = embedder
        self.store = store
        self.model = model
        self._vectors = None
        self._ids: List[str] = []

    def build(self, chunks: List[Chunk]) -> "EmbeddingDense":
        import hashlib
        np = _np()
        if np is None or self.embedder is None or not chunks:
            return self
        ids = [c.chunk_id for c in chunks]
        hashes = [hashlib.md5(c.text.encode("utf-8", "replace")).hexdigest() for c in chunks]
        reuse = {}
        cached = self.store.load() if self.store else None
        if cached:
            meta, vecs = cached
            if meta.get("model") == self.model:
                c_ids = meta.get("ids", [])
                c_hashes = meta.get("hashes", [])
                for i, cid in enumerate(c_ids):
                    if i < len(vecs) and i < len(c_hashes):
                        reuse[(cid, c_hashes[i])] = vecs[i]
        to_embed = [i for i, (cid, h) in enumerate(zip(ids, hashes)) if (cid, h) not in reuse]
        new_vecs = None
        if to_embed:
            texts = [chunks[i].text for i in to_embed]
            new_vecs = np.asarray(self.embedder.encode(texts), dtype=np.float32)
            if new_vecs.ndim == 1:
                new_vecs = new_vecs.reshape(1, -1)
        dim = 0
        if new_vecs is not None and len(new_vecs):
            dim = int(new_vecs.shape[1])
        elif reuse:
            dim = int(next(iter(reuse.values())).shape[0])
        if dim == 0:
            return self
        out = np.zeros((len(chunks), dim), dtype=np.float32)
        j = 0
        for i, (cid, h) in enumerate(zip(ids, hashes)):
            if (cid, h) in reuse:
                out[i] = reuse[(cid, h)]
            else:
                out[i] = new_vecs[j]
                j += 1
        self._vectors = out
        self._ids = ids
        if self.store:
            self.store.save({"model": self.model, "ids": ids, "hashes": hashes}, out)
        return self

    @property
    def ready(self) -> bool:
        return self._vectors is not None

    def __call__(self, query: str, chunks: List[Chunk], top: int) -> List[Tuple[int, float]]:
        np = _np()
        if np is None or self._vectors is None or self.embedder is None:
            return []
        q = np.asarray(self.embedder.encode([query]), dtype=np.float32).reshape(-1)
        qn = float(np.linalg.norm(q)) or 1.0
        mat = self._vectors
        mn = np.linalg.norm(mat, axis=1)
        mn[mn == 0] = 1.0
        sims = (mat @ q) / (mn * qn)
        order = np.argsort(sims)[::-1][:top]
        return [(int(i), float(sims[i])) for i in order]


class HttpReranker:
    """Cross-encoder reranker over the payments gateway. Tries /rerank then /score."""

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


def build_hybrid(idx: TerraPilotIndex, persist: bool = True) -> HybridRetriever:
    """Construct a HybridRetriever, auto-wiring dense + rerank when configured.
    Offline this is a no-op -> BM25-only."""
    import config
    hr = HybridRetriever(idx)
    embedder = None
    if config.EMBED_ENABLED:
        try:
            from embedder import make_embedder
            embedder = make_embedder()
        except Exception:
            pass
    if embedder is None:
        return hr
    store = None
    if persist:
        try:
            from storage import get_storage
            from persistence import EmbeddingStore, repo_namespace
            store = EmbeddingStore(get_storage(), repo_namespace(idx.root))
        except Exception:
            store = None
    dense = EmbeddingDense(embedder, store, model=gateway.resolve_model_id(config.EMBED_SLUG, config.EMBED_MODEL)).build(hr.chunks)
    if dense.ready:
        hr.dense = dense
    if config.RERANK_ENABLED:
        hr.reranker = HttpReranker(config.endpoint(config.RERANK_SLUG),
                                   config.API_KEY, gateway.resolve_model_id(config.RERANK_SLUG))
    return hr
