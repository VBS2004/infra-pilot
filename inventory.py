"""inventory.py - deterministic component lookup for the Acme Terraform Assistant.

The manager's ask - "list all ECS clusters with <config> in <project>" - is an
INVENTORY query, not a retrieval query. retrieval.HybridRetriever.search() is a
RANKER: it returns a fuzzy top_k, collapses per file, and has no hard predicate,
so it can neither guarantee completeness (recall) nor exactness (precision) for
a "list all ... where ..." question. This module answers it deterministically:

    walk the real tree  ->  filter by component type + project  ->  evaluate a
    config predicate against each component's real inputs.hcl  ->  print/return.

Repo convention (confirmed across compose.py / retrieval.py):
    infra/<project>/<provider>/<env>/<component>/inputs.hcl

Scope: predicates match BOTH TOP-LEVEL AND NESTED inputs (via dot-notation) (e.g. enable_irsa=false,
cluster_version=1.34, region=ap-south-1) via hcl_override.read_top_level. Nested
config (e.g. a node group's instance_type) is NOT filterable yet - it needs the
HCL parser from the parked "typed edit-set" idea. A predicate key that is never a
top-level scalar is reported loudly so results are never silently incomplete.

Dependency-free (os + hcl_override only) -> runs on the air-gapped box.
"""
from __future__ import annotations

import os
from typing import Dict, List, Optional, Tuple

import hcl_override
import hcl_edit

# Primary-name inputs, in priority order (mirrors compose._NAME_INPUT_CANDIDATES).
_NAME_CANDIDATES = ["cluster_name", "bucket_name", "identifier",
                    "function_name", "db_name", "repository_name", "name"]


def _iter_components(repo: str, resource_type: str, project: Optional[str],
                     provider: str, infra_root: str):
    """Yield (project, env, component_dir, inputs_path) for every existing
    <resource_type> component, scoped to `project` when given."""
    base = os.path.join(repo, infra_root)
    if not os.path.isdir(base):
        return
    projects = [project] if project else sorted(
        d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d)))
    for proj in projects:
        pdir = os.path.join(base, proj, provider)
        if not os.path.isdir(pdir):
            continue
        for env in sorted(os.listdir(pdir)):
            cdir = os.path.join(pdir, env, resource_type)
            ip = os.path.join(cdir, "inputs.hcl")
            if os.path.isfile(ip):
                yield proj, env, cdir, ip


def _parse_where(where: List[str]) -> List[Tuple[str, str, str]]:
    """Parse ['enable_irsa=false', 'region!=ap-south-1'] into (key, op, value)."""
    preds: List[Tuple[str, str, str]] = []
    for w in where or []:
        if "!=" in w:
            k, v = w.split("!=", 1)
            preds.append((k.strip(), "!=", v.strip()))
        elif "=" in w:
            k, v = w.split("=", 1)
            preds.append((k.strip(), "=", v.strip()))
        else:
            # bare key -> "exists"
            preds.append((w.strip(), "exists", ""))
    return preds


def _matches(values: Dict[str, str], preds: List[Tuple[str, str, str]]) -> bool:
    for key, op, val in preds:
        actual = values.get(key)
        if op == "exists":
            if actual is None:
                return False
        elif op == "=":
            if actual is None or hcl_override.norm_value(actual) != hcl_override.norm_value(val):
                return False
        elif op == "!=":
            # present AND different (an absent key is not a confirmed mismatch)
            if actual is None or hcl_override.norm_value(actual) == hcl_override.norm_value(val):
                return False
    return True


def _pick_name(values: Dict[str, str]) -> str:
    for c in _NAME_CANDIDATES:
        if c in values:
            return values[c]
    return ""


def find(repo: str, resource_type: str, *, project: Optional[str] = None,
         provider: str = "aws", where: Optional[List[str]] = None,
         infra_root: str = "infra") -> Tuple[List[Dict], set]:
    """Return (rows, unresolved_keys). Each row: {project, env, dir, name, values}.
    unresolved_keys = predicate keys that were NEVER seen as a top-level scalar
    in any scanned component (i.e. nested/unknown -> filter may be incomplete).
    """
    preds = _parse_where(where or [])
    pred_keys = {k for k, _, _ in preds}
    seen_keys: set = set()
    rows: List[Dict] = []
    for proj, env, cdir, ip in _iter_components(repo, resource_type, project,
                                                provider, infra_root):
        text = open(ip, encoding="utf-8", errors="replace").read()
        values = hcl_edit.read_all_values(text)
        seen_keys |= (pred_keys & set(values))
        if not _matches(values, preds):
            continue
        rows.append({"project": proj, "env": env,
                     "dir": os.path.relpath(cdir, repo),
                     "name": _pick_name(values), "values": values})
    return rows, (pred_keys - seen_keys)


def _cell(row: Dict, col: str) -> str:
    if col in ("project", "env", "name", "dir"):
        return row.get(col, "")
    return row["values"].get(col, "—")


def _print_table(rows: List[Dict], cols: List[str]) -> None:
    if not rows:
        print("# (no matching components)")
        return
    widths = {c: len(c) for c in cols}
    for r in rows:
        for c in cols:
            widths[c] = max(widths[c], len(str(_cell(r, c))))
    line = lambda vals: "  ".join(str(v).ljust(widths[c]) for c, v in zip(cols, vals))
    print(line(cols))
    print(line(["-" * widths[c] for c in cols]))
    for r in rows:
        print(line([_cell(r, c) for c in cols]))


def run_cli(repo: str, rest: List[str]) -> int:
    """`cli.py <repo> list <resource_type> [--project P] [--provider P]
                                           [--where key=value]...`
    --where is repeatable (AND). Supports key=value, key!=value, and bare key
    (exists). Returns 0 ok, 1 usage error.
    """
    resource_type: Optional[str] = None
    project: Optional[str] = None
    provider = "aws"
    where: List[str] = []
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == "--project":
            if i + 1 >= len(rest):
                print("# error: --project needs a value"); return 1
            project = rest[i + 1]; i += 2; continue
        if tok == "--provider":
            if i + 1 >= len(rest):
                print("# error: --provider needs a value"); return 1
            provider = rest[i + 1]; i += 2; continue
        if tok == "--where":
            if i + 1 >= len(rest):
                print("# error: --where needs a value"); return 1
            where.append(rest[i + 1]); i += 2; continue
        if tok.startswith("--"):
            print(f"# error: unknown flag {tok}"); return 1
        if resource_type is None:
            resource_type = tok
        else:
            print(f"# error: unexpected argument {tok!r}"); return 1
        i += 1

    if not resource_type:
        print('usage: cli.py <repo> list <resource_type> [--project P] '
              '[--provider P] [--where key=value]...')
        return 1

    rows, unresolved = find(repo, resource_type, project=project,
                            provider=provider, where=where)

    scope = f" in project '{project}'" if project else " across all projects"
    filt = (" matching " + ", ".join(where)) if where else ""
    print(f"# {len(rows)} '{resource_type}' component(s){scope}{filt}")

    for k in sorted(unresolved):
        print(f"# WARNING: predicate key '{k}' is not a top-level scalar input in "
              f"any scanned component. Nested-config filtering isn't supported "
              f"yet, so results may be INCOMPLETE for '{k}'.")

    pred_cols = []
    for w in where:
        k = w.split("!=", 1)[0].split("=", 1)[0].strip()
        if k not in pred_cols:
            pred_cols.append(k)
    cols = ["project", "env", "name"] + pred_cols
    _print_table(rows, cols)
    return 0


if __name__ == "__main__":
    import sys
    argv = sys.argv[1:]
    repo = None
    rest: List[str] = []
    i = 0
    while i < len(argv):
        if argv[i] == "--repo-root":
            repo = argv[i + 1] if i + 1 < len(argv) else None
            i += 2
            continue
        rest.append(argv[i]); i += 1
    if not repo:
        print("error: --repo-root <path> is required")
        sys.exit(2)
    sys.exit(run_cli(repo, rest))
