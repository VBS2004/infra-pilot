"""hcl_override.py - deterministic, dependency-free read/write for TOP-LEVEL
scalar entries inside a Terragrunt `inputs = { ... }` block.

Why this exists: a requested value (a cluster name, a boolean flag, a number)
must be GUARANTEED to land in / be read from the emitted inputs.hcl, independent
of any LLM. Prompt-level "please set X" is best-effort; this is authoritative.

Scope on purpose - only TOP-LEVEL scalar assignments (depth == 1, i.e. direct
children of the outer `inputs = { }`) are handled. Nested block/list values are
left to a real HCL parser (see the parked "typed edit-set" idea).

Used by:
  * compose.py     - enforce requested scalar values on generated/edited HCL.
  * inventory.py   - read scalar config for `list <type> --where key=value`.

Brace/bracket depth is tracked while ignoring characters inside double-quoted
strings and after # or // comments. Heredocs (<<EOT) are NOT interpreted.
"""
from __future__ import annotations

import re
from typing import Dict, List, Tuple

_SCALAR = (str, int, float, bool)

# key = value   (with an optional trailing # / // comment captured separately)
_ASSIGN = re.compile(r'^(\s*)([A-Za-z_][\w-]*)(\s*=\s*)(.*?)(\s*(?:#|//).*)?$')


def fmt_value(v) -> str:
    """Render a Python scalar as an HCL literal."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return '"%s"' % str(v)


def norm_value(raw) -> str:
    """Normalize an HCL scalar literal (or a Python value) for comparison:
    strip surrounding double-quotes, lower-case booleans, trim whitespace. Lets
    `enable_irsa=false` match `enable_irsa = false` and `v=1.34` match `"1.34"`."""
    if isinstance(raw, bool):
        return "true" if raw else "false"
    s = str(raw).strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1]
    low = s.lower()
    if low in ("true", "false"):
        return low
    return s


def _line_start_depths(lines: List[str]) -> List[int]:
    """Brace/bracket depth at the START of each line, string- and comment-aware."""
    depths: List[int] = []
    d = 0
    in_str = False
    for ln in lines:
        depths.append(d)
        j, L = 0, len(ln)
        while j < L:
            c = ln[j]
            if in_str:
                if c == "\\":
                    j += 2
                    continue
                if c == '"':
                    in_str = False
                j += 1
                continue
            if c == '"':
                in_str = True
            elif c == "#":
                break
            elif c == "/" and j + 1 < L and ln[j + 1] == "/":
                break
            elif c in "{[(":
                d += 1
            elif c in "}])":
                d -= 1
            j += 1
    return depths


def _is_scalar_value(val: str) -> bool:
    """True when `val` is a self-contained single-line scalar (not opening a
    block/list and internally balanced)."""
    s = val.strip()
    if not s or s[-1] in "{[(":
        return False
    return (s.count("{") == s.count("}")
            and s.count("[") == s.count("]")
            and s.count("(") == s.count(")"))


def read_top_level(inputs_hcl: str) -> Dict[str, str]:
    """Return {key: normalized_value} for every TOP-LEVEL scalar entry inside the
    outer `inputs = { }` block. Nested / block / list values are omitted (their
    keys simply won't appear), so callers can detect "not a top-level scalar"."""
    lines = inputs_hcl.splitlines(keepends=True)
    depths = _line_start_depths(lines)
    out: Dict[str, str] = {}
    for idx, ln in enumerate(lines):
        if depths[idx] != 1:
            continue
        m = _ASSIGN.match(ln.rstrip("\n"))
        if not m:
            continue
        key, val = m.group(2), m.group(4)
        if _is_scalar_value(val):
            out[key] = norm_value(val)
    return out


def set_top_level(inputs_hcl: str,
                  overrides: Dict[str, object]) -> Tuple[str, Dict[str, str]]:
    """Force `overrides` onto the top-level of `inputs_hcl`.

    Returns (new_hcl, applied) where applied maps each key to one of:
    'replaced' (existing scalar rewritten), 'injected' (added after the opening
    brace), or 'skipped(non-scalar)' (a same-named key existed but held a block/
    list value - left alone to avoid corrupting structure).
    """
    lines = inputs_hcl.splitlines(keepends=True)
    depths = _line_start_depths(lines)
    applied: Dict[str, str] = {}
    remaining = dict(overrides)
    out: List[str] = []

    for idx, ln in enumerate(lines):
        body = ln.rstrip("\n")
        nl = ln[len(body):]  # preserve the exact line ending (or none)
        handled = False
        if depths[idx] == 1:
            m = _ASSIGN.match(body)
            if m and m.group(2) in remaining:
                key = m.group(2)
                val = m.group(4)
                comment = m.group(5) or ""
                if _is_scalar_value(val):
                    out.append(f"{m.group(1)}{key}{m.group(3)}"
                               f"{fmt_value(remaining[key])}{comment}{nl}")
                    applied[key] = "replaced"
                    del remaining[key]
                    handled = True
                else:
                    applied[key] = "skipped(non-scalar)"
        if not handled:
            out.append(ln)

    # Inject keys that were not found, right after `inputs = {`.
    if remaining:
        open_re = re.compile(r'^\s*inputs\s*=\s*\{')
        inject_idx = None
        for idx, ln in enumerate(out):
            if open_re.match(ln):
                inject_idx = idx + 1
                break
        if inject_idx is not None:
            inj = [f"  {k} = {fmt_value(v)}\n" for k, v in remaining.items()]
            out[inject_idx:inject_idx] = inj
            for k in remaining:
                applied[k] = "injected"
        # If there is no recognizable `inputs = {` opener we leave the file be;
        # the caller's brace-balance guard will catch anything malformed.

    return "".join(out), applied


if __name__ == "__main__":
    sample = ('inputs = {\n  cluster_name = "old001"  # keep style\n'
              '  enable_irsa  = true\n  cluster_version = "1.34"\n'
              '  node_groups = [\n    { name = "ng1" }\n  ]\n}\n')
    print("read:", read_top_level(sample))
    new, ap = set_top_level(sample, {"cluster_name": "santosh123",
                                     "enable_irsa": False,
                                     "region": "ap-south-1"})
    print(new)
    print("applied:", ap)
