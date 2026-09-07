"""CLI for the payments indexer prototype.

Usage:
    python cli.py <repo> stats
    python cli.py <repo> outline <file>
    python cli.py <repo> find <query> [--kind resource]
    python cli.py <repo> imports <file>
    python cli.py <repo> importers <module_name_or_dir>
    python cli.py <repo> related <file>
    python cli.py <repo> search <query...>
    python cli.py <repo> catalog                # list reusable modules + inputs
    python cli.py <repo> plan <intent...>       # reuse-vs-write decision + emit
    python cli.py <repo> list <type> [--project P] [--where k=v]  # inventory existing components
    python cli.py <repo> compose <intent...>    # NL -> compose a component (dry-run)
    python cli.py <repo> compose --resource-type R --project P --env E [--apply] [--force]

These map onto the legacy_coder tool wrappers (index_outline / index_search /
index_related) plus the new hybrid `search`.
"""
import json
import sys

from index import TerraPilotIndex, BACKEND
from typing import List


def _print(obj):
    print(json.dumps(obj, indent=2, default=lambda o: o.__dict__))


def _run_list_nl(repo: str, rest: List[str]) -> int:
    """Handle NL inventory queries like: list eks with worker_node_group in billing.
    Simple pattern-based parsing - NO LLM needed."""
    import inventory
    import re
    
    text = " ".join(rest)
    print(f"# NL inventory: {text}")
    
    # Extract resource_type - look for: "eks", "network", "s3", etc.
    resource_type = None
    project = None
    where = []
    
    # Known resource types
    RESOURCE_TYPES = ["eks", "network", "s3", "vpc", "rds", "ecs", "alb", "ec2", "iam", "routing"]
    
    for rt in RESOURCE_TYPES:
        if re.search(rf'\b{rt}s?\b', text, re.IGNORECASE):
            resource_type = rt
            break
    
    # Extract project - look for "in <project>"
    proj_match = re.search(r'\bin\s+(\w+)', text)
    if proj_match:
        project = proj_match.group(1)
    
    # Extract filters - look for "named <name>"
    name_match = re.search(r'\bnamed\s+(\w+)', text)
    if name_match:
        where.append(f"cluster_name={name_match.group(1)}")
    
    # Build args for inventory.run_cli
    if not resource_type:
        print("# error: could not determine resource type")
        return 1
    
    args = [resource_type]
    if project:
        args.extend(["--project", project])
    for w in where:
        args.extend(["--where", w])
    
    print(f"# resource_type={resource_type}, project={project}, where={where}")
    return inventory.run_cli(repo, args)


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 1
    repo, cmd = argv[1], argv[2]
    rest = argv[3:]

    # compose: wire the MVP compose loop. Delegate BEFORE building the index
    # (like `livecheck`) so compose owns its own pipeline and we don't pay for
    # an eager index build the compose path may not need.
    if cmd == "compose":
        import compose
        return compose.run_cli(repo, rest)

    # list: supports both explicit args and NL intent
    #   explicit: list eks --project billing --where cluster_name=foo
    #   NL:       list all eks clusters with worker_node_group in billing
    if cmd == "list":
        # If rest contains flags like --project, --where, it's explicit args
        has_flags = any(r.startswith("--") for r in rest)
        if has_flags or not rest:
            import inventory
            return inventory.run_cli(repo, rest)
        else:
            # NL query - use intent parser
            return _run_list_nl(repo, rest)

    idx = TerraPilotIndex(repo).build()

    if cmd == "stats":
        kinds = {}
        for _, s in idx.all_symbols():
            kinds[s.kind] = kinds.get(s.kind, 0) + 1
        edge_kinds = {}
        for f in idx.files:
            for e in idx.get_imports(f):
                edge_kinds[e.edge_type] = edge_kinds.get(e.edge_type, 0) + 1
        _print({"backend": BACKEND, "files": len(idx.files),
                "symbols": len(idx.all_symbols()), "symbol_kinds": kinds,
                "edges": edge_kinds})
    elif cmd == "outline":
        _print([s.__dict__ for s in (idx.get_file_outline(rest[0]) or [])])
    elif cmd == "find":
        kind = None
        if "--kind" in rest:
            i = rest.index("--kind")
            kind = rest[i + 1]
            rest = rest[:i]
        res = idx.find_symbol(" ".join(rest), kind=kind)
        _print([{"file": f, **s.__dict__} for f, s in res])
    elif cmd == "imports":
        _print([e.__dict__ for e in idx.get_imports(rest[0])])
    elif cmd == "importers":
        _print(idx.get_importers(rest[0]))
    elif cmd == "related":
        _print(idx.related(rest[0]))
    elif cmd == "search":
        from lexical_search import build_hybrid
        hr = build_hybrid(idx)            # auto-wires dense+rerank when configured
        _print(hr.search(" ".join(rest)))
    elif cmd == "tool":
        # exercise the agent-facing tool wrappers (borrowed @tool shape)
        from agent_tools import tf_search
        print(tf_search(repo, " ".join(rest)))
    elif cmd == "livecheck":
        import livecheck
        return livecheck.main(repo)
    elif cmd == "catalog":
        from catalog import ModuleCatalog
        cat = ModuleCatalog(idx)
        _print([{"module": m.key, "resources": m.resource_types,
                 "required": [i.name for i in m.required_inputs],
                 "optional": [i.name for i in m.optional_inputs],
                 "outputs": m.outputs, "reuse_count": m.reuse_count,
                 "used_by": m.used_by} for m in cat.modules.values()])
    elif cmd == "plan":
        from planner import Planner
        p = Planner(idx).plan(" ".join(rest))
        print(f"# DECISION: {p.decision}")
        for n in p.notes:
            print(f"# {n}")
        if p.module:
            print(f"# module: {p.module.key} (reused by {p.module.reuse_count})")
        print("# candidates: " + ", ".join(c.key for c in p.candidates))
        print()
        print(p.rendered)
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))


