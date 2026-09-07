"""
Agent-facing tool wrappers (the legacy_coder `@tool` shape).

Borrowed pattern from legacy_coder tools/code_index.py + tools/semantic_search.py:
thin functions that take simple args and return a formatted STRING for the agent
to read. Kept dependency-free -- if langchain is installed they get wrapped as
LangChain tools via get_tools(); otherwise the plain callables are used as-is.

These are Terraform-scoped: tf_find / tf_outline / tf_related / tf_search /
tf_catalog / tf_plan.
"""
from __future__ import annotations

from typing import List, Optional

from index import get_index


def tf_find(repo: str, query: str, kind: str = "") -> str:
    """Find HCL symbols (resource/module/variable/output/...) by name."""
    idx = get_index(repo)
    res = idx.find_symbol(query, kind=kind or None)
    if not res:
        return f"No symbols matching '{query}'."
    lines = [f"{len(res)} symbol(s) for '{query}':"]
    for f, s in res[:30]:
        lines.append(f"  {s.kind:<10} {s.name}  {f}:{s.line_start}-{s.line_end}")
    return "\n".join(lines)


def tf_outline(repo: str, file_path: str) -> str:
    """Structured outline (symbols + line ranges) for one HCL file."""
    idx = get_index(repo)
    syms = idx.get_file_outline(file_path)
    if syms is None:
        return f"'{file_path}' is not indexed."
    if not syms:
        return f"No symbols in '{file_path}'."
    lines = [f"Outline of {file_path} ({len(syms)} symbols):"]
    for s in syms:
        indent = "    " if s.parent else "  "
        lines.append(f"{indent}{s.kind:<10} {s.name}  ({s.line_start}-{s.line_end})")
    return "\n".join(lines)


def tf_related(repo: str, file_path: str) -> str:
    """Show 1-hop dependency edges in/out of a file (blast radius)."""
    idx = get_index(repo)
    rel = idx.related(file_path)
    lines = [f"Related files for {rel['file']}:"]
    imps = rel["imports"]
    if imps:
        lines.append(f"  Edges out ({len(imps)}):")
        for e in imps[:20]:
            tail = f" -> {e.output}" if getattr(e, "output", "") else ""
            lines.append(f"    {e.edge_type:<14} {e.module}{tail}")
    if rel["importers"]:
        lines.append(f"  Imported by ({len(rel['importers'])}):")
        for f in rel["importers"][:20]:
            lines.append(f"    {f}")
    return "\n".join(lines)


def tf_search(repo: str, query: str, top_k: int = 8) -> str:
    """Hybrid search (BM25 + dense when configured) with graph expansion."""
    from lexical_search import build_hybrid
    idx = get_index(repo)
    hr = build_hybrid(idx)
    res = hr.search(query, top_k=top_k)
    if not res:
        return f"No matches for '{query}'."
    lines = [f"{len(res)} result(s) for '{query}':"]
    for r in res:
        if "chunk_id" in r:
            ls = r["lines"]
            lines.append(f"  [{r['reason']}] {r['kind']} {r['symbol']}  "
                         f"{r['file']}:{ls[0]}-{ls[1]}")
        else:
            lines.append(f"  [{r['reason']}] {r['file']}  (via {r.get('via','')})")
    return "\n".join(lines)


def tf_catalog(repo: str) -> str:
    """List reusable modules with required/optional inputs + reuse counts."""
    from catalog import ModuleCatalog
    idx = get_index(repo)
    cat = ModuleCatalog(idx)
    if not cat.modules:
        return "No reusable modules found."
    lines = [f"{len(cat.modules)} reusable module(s):"]
    for m in cat.modules.values():
        lines.append(f"  {m.key}  resources={m.resource_types}  "
                     f"required={[i.name for i in m.required_inputs]}  "
                     f"reuse={m.reuse_count}")
    return "\n".join(lines)


def tf_plan(repo: str, intent: str) -> str:
    """Reuse-vs-write decision + emitted terragrunt.hcl / scaffold."""
    from planner import Planner
    idx = get_index(repo)
    p = Planner(idx).plan(intent)
    head = [f"# DECISION: {p.decision}"]
    for n in p.notes:
        head.append(f"# {n}")
    if p.module:
        head.append(f"# module: {p.module.key} (reused by {p.module.reuse_count})")
    return "\n".join(head) + "\n\n" + p.rendered


_PLAIN = [tf_find, tf_outline, tf_related, tf_search, tf_catalog, tf_plan]


def get_tools():
    """Return LangChain tools if langchain is installed, else plain callables."""
    try:
        from langchain_core.tools import tool
        return [tool(fn) for fn in _PLAIN]
    except Exception:
        return list(_PLAIN)
