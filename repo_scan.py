"""
Fast, gitignore-aware repo file listing.

Borrowed pattern from legacy_coder (index._list_project_files): use ripgrep
`rg --files` when present (honours .gitignore, very fast), fall back to an
os.walk with sensible ignores. Filtered to HCL/Terragrunt extensions.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from typing import List, Tuple

_IGNORE_DIRS = {".git", ".terraform", ".terragrunt-cache", "node_modules",
                "__pycache__", ".idea", ".vscode"}


def _rg_files(root: str) -> List[str]:
    rg = shutil.which("rg")
    if not rg:
        return []
    try:
        res = subprocess.run([rg, "--files", root], capture_output=True,
                             text=True, timeout=30)
        if res.returncode <= 1 and res.stdout.strip():
            return res.stdout.strip().splitlines()
    except (subprocess.TimeoutExpired, OSError):
        pass
    return []


def _walk_files(root: str) -> List[str]:
    out: List[str] = []
    for dp, dirnames, fns in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _IGNORE_DIRS]
        for fn in fns:
            out.append(os.path.join(dp, fn))
    return out


def list_hcl_files(root: str,
                   exts: Tuple[str, ...] = (".tf", ".hcl", ".tfvars")) -> List[str]:
    files = _rg_files(root) or _walk_files(root)
    keep = []
    for f in files:
        if any(seg in _IGNORE_DIRS for seg in f.replace(os.sep, "/").split("/")):
            continue
        if f.endswith(exts):
            keep.append(f)
    return sorted(keep)
