"""retrieval.py - grounding retrieval for the compose loop (Phase 2).

Turns a resolved intent into the concrete grounding the generator needs:
  - the best REUSE module candidate for the resource type,
  - that module's variables.tf schema (the contract inputs.hcl must satisfy),
  - 1-2 sibling inputs.hcl examples from other envs/projects (few-shot anchors),
  - the project common.hcl (shared inputs we must NOT duplicate).

IMPORTANT: the repo's agent_tools.tf_* helpers return human/agent-readable
STRINGS. For structured data we go straight to the layer they wrap:
    index.get_index(repo)        -> find_symbol / get_file_outline / related
    catalog.ModuleCatalog(idx)   -> .modules {key, resource_types,
                                       required_inputs[].name, reuse_count}
These are imported lazily (like agent_tools) so this module imports fine even
where the index isn't built. Attribute access is defensive (getattr) since the
catalog's exact field set may evolve.

HYBRID RETRIEVAL (22 Jun 2026): when the index + gateway are available we also
run lexical_search.build_hybrid(idx).search(query) -- BM25 + dense (Jina-v3) +
RRF + qwen reranker over HCL-block chunks. The ranked hits are surfaced in the
grounding (so the generator sees the closest real precedents, with scores for
observability) and provide a recall FALLBACK for module selection when the
lexical catalog match comes up empty (synonyms / "similar to X"). Everything
degrades gracefully: offline / no index -> hybrid_search returns [] and we fall
back to the deterministic catalog path, so the working 38/38 reuse cases are
unchanged.
"""
from __future__ import annotations

import dataclasses
import os
import re
from typing import Dict, List, Optional, Tuple

import paths

_VAR_RE = re.compile(r'variable\s+"([^"]+)"', re.MULTILINE)


@dataclasses.dataclass
class ModuleCandidate:
    key: str
    resource_types: List[str]
    required_inputs: List[str]
    optional_inputs: List[str]
    reuse_count: int


@dataclasses.dataclass
class RetrievalBundle:
    resource_type: str
    module: Optional[ModuleCandidate]
    decision: str                       # "reuse" | "net-new"
    variables_tf_path: Optional[str]
    variables_tf_text: Optional[str]
    variable_names: List[str]
    sibling_inputs: List[Dict[str, str]]    # [{path, text}]
    common_hcl_text: Optional[str]
    notes: List[str]
    retrieved: List[Dict] = dataclasses.field(default_factory=list)  # hybrid hits


def load_index(repo: str):
    from index import get_index  # lazy - matches agent_tools.py
    return get_index(repo)


def build_catalog(repo: str):
    from catalog import ModuleCatalog  # lazy
    return ModuleCatalog(load_index(repo))


def _names(inputs) -> List[str]:
    out: List[str] = []
    for i in inputs or []:
        out.append(getattr(i, "name", None) or (i if isinstance(i, str) else str(i)))
    return out


# --------------------------------------------------------------------------- #
# Hybrid semantic retrieval (BM25 + dense Jina-v3 + RRF + qwen reranker)
# --------------------------------------------------------------------------- #
def hybrid_search(repo: str, query: str, *, top_k: int = 8) -> List[Dict]:
    """Run lexical_search.build_hybrid(idx).search(query).

    Returns the ranked result dicts (each carries file/symbol/kind/chunk_id +
    rrf and, when the reranker is wired, score; plus dep:* graph-expansion
    entries). Returns [] on ANY failure (offline, no index, gateway down) so
    callers degrade cleanly to the deterministic catalog path.
    """
    if not query or not query.strip():
        return []
    try:
        from lexical_search import build_hybrid  # lazy - lives in the indexer repo
        hr = build_hybrid(load_index(repo))
        return hr.search(query, top_k=top_k)
    except Exception:
        return []


def _primary_hits(hits: List[Dict]) -> List[Dict]:
    """Real retrieved chunks (not dep:* graph-expansion entries)."""
    return [h for h in (hits or []) if str(h.get("reason", "")).startswith("retrieved")]


def _dep_hits(hits: List[Dict]) -> List[Dict]:
    """Graph-expansion entries (dep:template / dep:module_source / dep:var_ref)."""
    return [h for h in (hits or []) if str(h.get("reason", "")).startswith("dep:")]


def _dep_template_hits(hits: List[Dict]) -> List[Dict]:
    """The .tf MODULE TEMPLATE files pulled via graph expansion -- the real
    resource definitions to model a net-new leaf module on."""
    return [h for h in (hits or []) if str(h.get("reason", "")) == "dep:template"]


def _read_file_head(repo: str, rel: str, *, max_chars: int = 3000) -> str:
    """Read the head of a repo-relative file (dep:* hits have no line range)."""
    if not rel:
        return ""
    try:
        f = os.path.join(repo, rel)
        return open(f, encoding="utf-8", errors="replace").read()[:max_chars]
    except Exception:
        return ""


def _hit_score_tag(h: Dict) -> str:
    sc = h.get("score")
    return ("score=%s" % sc) if sc is not None else ("rrf=%s" % h.get("rrf"))


def _module_key_for_file(cat, file: str) -> Optional[str]:
    """Map a hit's file path back to the catalog module dir that owns it
    (longest matching module key)."""
    if not file:
        return None
    f = file.replace("\\", "/")
    best: Optional[str] = None
    for m in getattr(cat, "modules", {}).values():
        k = str(getattr(m, "key", "") or "").replace("\\", "/")
        if not k:
            continue
        if f == k or f.startswith(k + "/"):
            if best is None or len(k) > len(best):
                best = str(getattr(m, "key", ""))
    return best


def _candidate_from_module(m) -> ModuleCandidate:
    return ModuleCandidate(
        key=str(getattr(m, "key", "")),
        resource_types=[str(x) for x in (getattr(m, "resource_types", []) or [])],
        required_inputs=_names(getattr(m, "required_inputs", [])),
        optional_inputs=_names(getattr(m, "optional_inputs", [])),
        reuse_count=int(getattr(m, "reuse_count", 0) or 0),
    )


def semantic_module(repo: str, resource_type: str, query: str,
                    *, _hits: Optional[List[Dict]] = None
                    ) -> Tuple[Optional[ModuleCandidate], List[Dict]]:
    """Pick a reuse candidate from the hybrid hits: the first ranked hit whose
    file maps to a known catalog module. Used as a recall fallback when the
    lexical catalog match is empty. Returns (candidate_or_None, hits)."""
    hits = _hits if _hits is not None else hybrid_search(repo, query)
    prim = _primary_hits(hits)
    if not prim:
        return None, hits
    try:
        cat = build_catalog(repo)
    except Exception:
        return None, hits
    modules = {str(getattr(m, "key", "")): m for m in getattr(cat, "modules", {}).values()}
    for h in prim:
        key = _module_key_for_file(cat, h.get("file") or "")
        if key and key in modules:
            return _candidate_from_module(modules[key]), hits
    return None, hits


def _read_block(repo: str, hit: Dict, *, max_chars: int = 1200) -> str:
    """Read the source text of a retrieved chunk's block from disk (best effort)."""
    try:
        f = os.path.join(repo, hit.get("file", ""))
        lines = open(f, encoding="utf-8", errors="replace").read().splitlines()
        rng = hit.get("lines") or [1, len(lines)]
        a, b = int(rng[0]), int(rng[1])
        return "\n".join(lines[max(0, a - 1):b])[:max_chars]
    except Exception:
        return ""


def find_module(repo: str, resource_type: str) -> Optional[ModuleCandidate]:
    """Best reuse candidate for a resource type, ranked by (match, reuse_count)."""
    cat = build_catalog(repo)
    rt = (resource_type or "").lower()
    matches = []
    for m in getattr(cat, "modules", {}).values():
        key = str(getattr(m, "key", "")).lower()
        rtypes = [str(x).lower() for x in (getattr(m, "resource_types", []) or [])]
        score = None
        if rt and (rt == key or rt in rtypes):
            score = 2
        elif rt and (rt in key or any(rt in t for t in rtypes)):
            score = 1
        if score is not None:
            matches.append((score, int(getattr(m, "reuse_count", 0) or 0), m))
    if not matches:
        return None
    matches.sort(key=lambda x: (x[0], x[1]), reverse=True)
    m = matches[0][2]
    return _candidate_from_module(m)


def read_variables_tf(repo: str, component: str, terraform_module: str,
                      provider: str = paths.DEFAULT_PROVIDER
                      ) -> Tuple[Optional[str], Optional[str], List[str]]:
    msrc = paths.module_source_dir(repo, terraform_module, component, provider)
    vpath = os.path.join(msrc, "variables.tf")
    if not os.path.exists(vpath):
        return (vpath if os.path.isdir(msrc) else None, None, [])
    text = open(vpath, encoding="utf-8", errors="replace").read()
    return vpath, text, _VAR_RE.findall(text)


def sibling_inputs(repo: str, component: str, *, limit: int = 2,
                   exclude_dir: Optional[str] = None) -> List[Dict[str, str]]:
    """Find existing <component>/inputs.hcl across the repo as few-shot anchors
    (skips infra/modules and .terragrunt-cache)."""
    infra = paths.infra_root(repo)
    found: List[Dict[str, str]] = []
    exclude_abs = os.path.abspath(exclude_dir) if exclude_dir else None
    for dirpath, _dirnames, filenames in os.walk(infra):
        probe = dirpath + os.sep
        if ".terragrunt-cache" in dirpath or (os.sep + "modules" + os.sep) in probe:
            continue
        if os.path.basename(dirpath) == component and "inputs.hcl" in filenames:
            if exclude_abs and os.path.abspath(dirpath) == exclude_abs:
                continue
            p = os.path.join(dirpath, "inputs.hcl")
            try:
                txt = open(p, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            found.append({"path": p, "text": txt})
            if len(found) >= limit:
                break
    return found


def read_common_hcl(repo: str, project: str,
                    provider: str = paths.DEFAULT_PROVIDER) -> Optional[str]:
    p = paths.common_hcl_path(repo, project, provider)
    if os.path.exists(p):
        return open(p, encoding="utf-8", errors="replace").read()
    return None


def gather(repo: str, *, resource_type: str, project: str, env: str,
           terraform_module: Optional[str] = None,
           provider: str = paths.DEFAULT_PROVIDER,
           query: Optional[str] = None) -> RetrievalBundle:
    """Assemble the full grounding bundle for one compose request."""
    import root_schema
    schema = root_schema.get_schema(repo)
    notes: List[str] = []
    if terraform_module is None:
        cfg = paths.load_env_config(repo, project, env, provider)
        # If the schema template doesn't even use {terraform_module}, default to empty
        if "{terraform_module}" not in schema.module_source_template:
            terraform_module = ""
        else:
            terraform_module = cfg.get("terraform_module") or "env"
            
        if not cfg:
            notes.append(f"env {schema.env_config_filename} not found; assumed terraform_module='{terraform_module}'")

    module: Optional[ModuleCandidate] = None
    try:
        module = find_module(repo, resource_type)
    except Exception as e:  # index/catalog unavailable - fall back to disk check
        notes.append(f"catalog lookup unavailable ({type(e).__name__}); using path check")

    # Hybrid semantic retrieval (graceful: [] when offline / no index / gateway down).
    retrieved = hybrid_search(repo, query) if query else []
    if retrieved:
        prim = _primary_hits(retrieved)
        for h in prim[:2]:
            h["_text"] = _read_block(repo, h)
        if prim:
            notes.append("semantic retrieval: %d hits (top: %s %s)" % (
                len(prim), prim[0].get("symbol"), _hit_score_tag(prim[0])))
        # Recall fallback: lexical/catalog match empty -> adopt the top semantic
        # module so synonyms / "similar to X" still resolve to reuse.
        if module is None:
            sm, _ = semantic_module(repo, resource_type, query, _hits=retrieved)
            if sm is not None:
                module = sm
                notes.append("module selected via semantic retrieval "
                             "(lexical match empty): %s" % sm.key)

    vpath, vtext, vnames = read_variables_tf(repo, resource_type, terraform_module, provider)
    exists_on_disk = bool(vtext) or paths.module_exists(repo, terraform_module, resource_type, provider)
    decision = "reuse" if (module or exists_on_disk) else "net-new"
    if decision == "net-new":
        notes.append(
            f"no module for '{resource_type}' under infrastructure/{terraform_module} "
            f"or in the catalog; net-new .tf required"
        )
        # Phase 8: for net-new there is no variables.tf contract or reusable
        # module, so the .tf module TEMPLATE reached via graph expansion is the
        # primary precedent -> read its source so the generator can model the
        # new leaf module's resources on a real one.
        tmpl = _dep_template_hits(retrieved)
        for h in tmpl[:2]:
            h["_text"] = _read_file_head(repo, h.get("file", ""))
        if tmpl:
            notes.append("net-new grounding: %d module-template file(s) via graph "
                         "expansion (e.g. %s)" % (len(tmpl), tmpl[0].get("module")))

    comp_dir = paths.component_dir(repo, project, env, resource_type, provider)
    fallback_vars = (module.required_inputs + module.optional_inputs) if module else []
    return RetrievalBundle(
        resource_type=resource_type,
        module=module,
        decision=decision,
        variables_tf_path=vpath,
        variables_tf_text=vtext,
        variable_names=vnames or fallback_vars,
        sibling_inputs=sibling_inputs(repo, resource_type, exclude_dir=comp_dir),
        common_hcl_text=read_common_hcl(repo, project, provider),
        notes=notes,
        retrieved=retrieved,
    )


def to_prompt_context(b: RetrievalBundle, *, max_sibling_chars: int = 2000,
                      max_vars_chars: int = 4000) -> str:
    """Format a bundle into a compact grounding block for the generator."""
    parts = [f"RESOURCE TYPE: {b.resource_type}", f"DECISION: {b.decision}"]
    if b.module:
        parts.append(f"MODULE: {b.module.key} (reuse_count={b.module.reuse_count})")
        parts.append("REQUIRED INPUTS: " + (", ".join(b.module.required_inputs) or "(unknown)"))
        if b.module.optional_inputs:
            parts.append("OPTIONAL INPUTS: " + ", ".join(b.module.optional_inputs))
    if b.variables_tf_text:
        parts.append("\n--- variables.tf (module contract) ---\n" + b.variables_tf_text[:max_vars_chars])
    elif b.variable_names:
        parts.append("VARIABLES: " + ", ".join(b.variable_names))
    # Semantic retrieval: ranked closest precedents (observability + grounding).
    prim = _primary_hits(getattr(b, "retrieved", []) or [])
    if prim:
        ranked = ["\n--- semantic retrieval: closest precedents (BM25+dense+rerank) ---"]
        for h in prim[:6]:
            ranked.append("  [%s] %s %s  (%s)" % (
                _hit_score_tag(h), h.get("kind"), h.get("symbol"), h.get("file")))
        parts.append("\n".join(ranked))
        for h in prim[:2]:
            bt = h.get("_text") or ""
            if bt:
                parts.append("\n--- closest precedent: %s ---\n%s" % (h.get("chunk_id"), bt))
    # Phase 8: dependency graph expansion. For net-new the .tf module template is
    # the resource-definition precedent to model the new leaf module on; we gate
    # this to net-new so the working reuse prompts stay byte-identical.
    if b.decision == "net-new":
        deps = _dep_hits(getattr(b, "retrieved", []) or [])
        if deps:
            tmpl = [h for h in deps if h.get("reason") == "dep:template"]
            srcs = [h for h in deps if h.get("reason") == "dep:module_source"]
            dsec = ["\n--- net-new grounding: dependency templates (graph expansion) ---"]
            for h in tmpl[:6]:
                dsec.append("  [template] %s  (module %s)" % (h.get("file"), h.get("module")))
            for h in srcs[:4]:
                dsec.append("  [module-source contract] %s" % h.get("file"))
            parts.append("\n".join(dsec))
            for h in tmpl[:2]:
                t = h.get("_text") or ""
                if t:
                    parts.append("\n--- module template (.tf to model the new leaf on): "
                                 "%s ---\n%s" % (h.get("file"), t))
    for i, s in enumerate(b.sibling_inputs, 1):
        parts.append(f"\n--- sibling example {i}: {s['path']} ---\n" + s["text"][:max_sibling_chars])
    if b.common_hcl_text:
        parts.append("\n--- common.hcl (shared inputs; DO NOT duplicate) ---\n"
                     + b.common_hcl_text[:max_vars_chars])
    if b.notes:
        parts.append("\nNOTES: " + " | ".join(b.notes))
    return "\n".join(parts)


if __name__ == "__main__":
    import json
    import sys
    if len(sys.argv) < 5:
        print("usage: python3 retrieval.py <repo> <resource_type> <project> <env> "
              "[terraform_module] [\"nl query\"]")
        sys.exit(2)
    repo, rt, proj, env = sys.argv[1:5]
    tm = sys.argv[5] if len(sys.argv) > 5 else None
    q = sys.argv[6] if len(sys.argv) > 6 else rt
    bundle = gather(repo, resource_type=rt, project=proj, env=env,
                    terraform_module=tm, query=q)
    print(to_prompt_context(bundle))
