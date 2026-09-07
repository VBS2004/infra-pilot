"""
Acme Terraform Assistant — dependency-graph-aware structural index (Layer 1).

This is the net-new, critical-path piece from the design plan: reimplement
`legacy_coder`'s structural index for HCL/Terragrunt. It produces:

  * a Symbol table  (resource / module / variable / output / data / locals /
                     provider / terraform / include / dependency / generate +
                     nested blocks like dynamic / ingress / lifecycle), and
  * the 5 edge types that make retrieval dependency-aware:
        1. module_source   module "x" { source = "../resource/ec2" }
        2. remote_state     module.remote_state.components[var.upstream_modules.<n>]["<out>"]
        3. var_ref          var.<name>
        4. output_ref       module.<inst>.<out>  /  dependency.<n>.outputs.<out>
        5. terragrunt       include / dependency / find_in_parent_folders() / inputs.hcl

It exposes the exact interface the legacy_coder tool wrappers depend on:

    idx.find_symbol(query, kind=None)   -> list[(file_path, Symbol)]
    idx.get_file_outline(file_path)     -> list[Symbol] | None
    idx.get_imports(file_path)          -> list[ImportEntry]   # .module
    idx.get_importers(module_name)      -> list[file_path]
    idx._resolve_relative(...) / idx._path_to_module_names(...) / idx._index

Parser backend is pluggable: tree-sitter when installed (laptop), otherwise the
bundled stdlib parser (hcl_parser.py). Both emit the same Block AST.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from hcl_parser import Block, parse_hcl as _py_parse_hcl, iter_blocks
from file_cache import FileCache
from repo_scan import list_hcl_files


# --------------------------------------------------------------------------- #
# Parser backend selection
# --------------------------------------------------------------------------- #
def _select_backend():
    """Return (parse_fn, backend_name). Prefer tree-sitter, fall back to stdlib."""
    if os.environ.get("FORCE_PY_PARSER") == "1":
        return _py_parse_hcl, "python-fallback"
    try:
        import ts_backend  # local module; only imports cleanly if tree-sitter is present
        return ts_backend.parse_hcl, "tree-sitter"
    except Exception:
        return _py_parse_hcl, "python-fallback"


_PARSE, BACKEND = _select_backend()


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class Symbol:
    name: str
    kind: str
    line_start: int
    line_end: int
    signature: str
    parent: Optional[str] = None      # name of enclosing symbol, if nested
    file: str = ""


EDGE_TYPES = ("module_source", "remote_state", "var_ref", "output_ref", "terragrunt")


@dataclass
class ImportEntry:
    """One dependency edge out of a file.

    `module` keeps backward-compat with legacy_coder's ImportEntry.module: for
    module_source edges it is the resolved module directory key; for other edge
    types it is the most useful target identifier (upstream name, var name, etc).
    """
    edge_type: str          # one of EDGE_TYPES
    module: str             # resolved target key (compat field)
    name: str = ""          # symbol/instance/var name involved
    output: str = ""        # output name for remote_state / output_ref
    line: int = 0
    raw: str = ""
    target_file: str = ""   # resolved file/dir when known


@dataclass
class FileInfo:
    path: str
    text: str
    clean: str
    blocks: List[Block]
    symbols: List[Symbol] = field(default_factory=list)
    imports: List[ImportEntry] = field(default_factory=list)
    file_hash: str = ""


# --------------------------------------------------------------------------- #
# Reference regexes
# --------------------------------------------------------------------------- #
_RE_SOURCE = re.compile(r'source\s*=\s*"([^"]+)"')
_RE_CONFIG_PATH = re.compile(r'config_path\s*=\s*"([^"]+)"')
_RE_VAR = re.compile(r'\bvar\.([A-Za-z_][\w-]*)')
_RE_REMOTE_STATE = re.compile(
    r'module\.remote_state\.components\[\s*var\.upstream_modules\.'
    r'([A-Za-z_][\w-]*)\s*\]\s*\[\s*"([^"]+)"\s*\]'
)
_RE_MODULE_OUT = re.compile(r'\bmodule\.([A-Za-z_][\w-]*)\.([A-Za-z_][\w-]*)')
_RE_DEP_OUT = re.compile(r'\bdependency\.([A-Za-z_][\w-]*)\.outputs\.([A-Za-z_][\w-]*)')
_RE_TG_FUNCS = re.compile(
    r'\b(find_in_parent_folders|read_terragrunt_config|path_relative_to_include|'
    r'path_relative_from_include|get_repo_root|get_terragrunt_dir|get_parent_terragrunt_dir)\s*\('
)
# strip ${...} and HCL function wrappers from a source string to recover a path
_RE_INTERP = re.compile(r'\$\{[^}]*\}')


LEAF_KINDS = {"resource", "module", "variable", "output", "data", "locals",
              "provider", "terraform", "include", "dependency", "generate",
              "remote_state", "inputs", "dependencies"}
NESTED_KINDS = {"dynamic", "ingress", "egress", "lifecycle", "provisioner",
                "connection", "timeouts", "setting", "filter", "content",
                "root_block_device", "ebs_block_device", "network_interface"}


def _symbol_name(b: Block) -> str:
    if b.type in ("resource", "data") and len(b.labels) >= 2:
        return f"{b.labels[0]}.{b.labels[1]}"
    if b.labels:
        return b.labels[0]
    return b.type


# --------------------------------------------------------------------------- #
# The index
# --------------------------------------------------------------------------- #
class TerraPilotIndex:
    HCL_EXTS = (".tf", ".hcl", ".tfvars")

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self._index: Dict[str, FileInfo] = {}        # relpath -> FileInfo
        self._symbols: List[Tuple[str, Symbol]] = []  # (relpath, Symbol)
        self._importers: Dict[str, set] = {}          # module_key -> {relpath}
        self._module_dirs: set = set()                # module dir keys that exist
        self._file_cache = FileCache()                # borrowed: LRU + mtime cache

    # ---- build / refresh -------------------------------------------------- #
    def build(self) -> "TerraPilotIndex":
        # borrowed: ripgrep --files (gitignore-aware) with os.walk fallback
        for full in list_hcl_files(self.root, self.HCL_EXTS):
            self._index_file(full)
        self._build_reverse_maps()
        return self

    def refresh(self, changed_paths: Optional[Iterable[str]] = None) -> None:
        if changed_paths is None:
            self._index.clear()
            self.build()
            return
        for p in changed_paths:
            full = os.path.abspath(p)
            if os.path.exists(full):
                self._index_file(full)
            else:
                self._index.pop(self._rel(full), None)
        self._build_reverse_maps()

    def on_file_changed(self, path: str) -> None:
        """Borrowed incremental-refresh hook: re-index one changed file."""
        full = os.path.abspath(path)
        self._file_cache.invalidate(full)
        self.refresh([full])

    def _rel(self, full: str) -> str:
        return os.path.relpath(full, self.root).replace(os.sep, "/")

    def _index_file(self, full: str) -> None:
        rel = self._rel(full)
        text = self._file_cache.get(full)        # borrowed: cache hit avoids re-read
        if text is None:
            with open(full, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            self._file_cache.set(full, text)
        blocks, clean = _PARSE(text)
        info = FileInfo(path=rel, text=text, clean=clean, blocks=blocks,
                        file_hash=hashlib.md5(text.encode("utf-8", "replace")).hexdigest())
        info.symbols = self._extract_symbols(rel, blocks)
        info.imports = self._extract_imports(rel, blocks, clean)
        self._index[rel] = info
        # a directory with .tf files defining resources/variables is a module dir
        self._module_dirs.add(os.path.dirname(rel))

    def _build_reverse_maps(self) -> None:
        self._symbols = []
        self._importers = {}
        for rel, info in self._index.items():
            for s in info.symbols:
                self._symbols.append((rel, s))
            for imp in info.imports:
                if imp.edge_type == "module_source" and imp.module:
                    self._importers.setdefault(imp.module, set()).add(rel)

    # ---- extraction ------------------------------------------------------- #
    def _extract_symbols(self, rel: str, blocks: List[Block]) -> List[Symbol]:
        syms: List[Symbol] = []
        for b in iter_blocks(blocks):
            is_top = b.parent is None
            if is_top and b.type in LEAF_KINDS:
                kind = b.type
            elif (not is_top) and (b.type in NESTED_KINDS or True):
                # any nested block is recorded as a 'block' child
                kind = b.type if b.type in NESTED_KINDS else "block"
            else:
                kind = b.type
            name = _symbol_name(b)
            parent = _symbol_name(b.parent) if b.parent is not None else None
            sig = b.header
            syms.append(Symbol(name=name, kind=kind, line_start=b.line_start,
                               line_end=b.line_end, signature=sig,
                               parent=parent, file=rel))
        return syms

    def _extract_imports(self, rel: str, blocks: List[Block], clean: str) -> List[ImportEntry]:
        edges: List[ImportEntry] = []
        seen = set()

        def add(e: ImportEntry):
            key = (e.edge_type, e.module, e.name, e.output, e.line)
            if key not in seen:
                seen.add(key)
                edges.append(e)

        # 1) module_source — from `module` blocks and terragrunt `terraform`/top-level source
        for b in iter_blocks(blocks):
            body = clean[b.body_start:b.body_end]
            if b.type in ("module", "terraform"):
                m = _RE_SOURCE.search(body)
                if m:
                    src = m.group(1)
                    key = self._resolve_relative(rel, src)
                    add(ImportEntry(edge_type="module_source", module=key,
                                    name=_symbol_name(b), line=b.line_start,
                                    raw=src, target_file=key))
        # terragrunt files sometimes set source at top level inside terraform{} only,
        # already handled above. Also handle a bare top-level `source = ` (rare).

        # 2) remote_state cross-stack edges
        for m in _RE_REMOTE_STATE.finditer(clean):
            line = clean.count("\n", 0, m.start()) + 1
            add(ImportEntry(edge_type="remote_state", module=m.group(1),
                            name=m.group(1), output=m.group(2), line=line,
                            raw=m.group(0)))

        # 3) var refs
        for m in _RE_VAR.finditer(clean):
            line = clean.count("\n", 0, m.start()) + 1
            add(ImportEntry(edge_type="var_ref", module=m.group(1),
                            name=m.group(1), line=line, raw=m.group(0)))

        # 4) output refs:  module.<inst>.<out>  (excluding remote_state) and
        #                  dependency.<n>.outputs.<out>
        for m in _RE_MODULE_OUT.finditer(clean):
            inst, out = m.group(1), m.group(2)
            if inst == "remote_state":
                continue
            line = clean.count("\n", 0, m.start()) + 1
            add(ImportEntry(edge_type="output_ref", module=inst, name=inst,
                            output=out, line=line, raw=m.group(0)))
        for m in _RE_DEP_OUT.finditer(clean):
            line = clean.count("\n", 0, m.start()) + 1
            add(ImportEntry(edge_type="output_ref", module=m.group(1),
                            name=m.group(1), output=m.group(2), line=line,
                            raw=m.group(0)))

        # 5) terragrunt wiring: include / dependency blocks + parent functions + inputs.hcl
        for b in iter_blocks(blocks):
            if b.type == "include":
                body = clean[b.body_start:b.body_end]
                pm = _RE_SOURCE.search(body) or re.search(r'path\s*=\s*([^\n]+)', body)
                raw = pm.group(0).strip() if pm else "include"
                add(ImportEntry(edge_type="terragrunt", module="include",
                                name=(b.labels[0] if b.labels else ""),
                                line=b.line_start, raw=raw))
            elif b.type == "dependency":
                body = clean[b.body_start:b.body_end]
                cp = _RE_CONFIG_PATH.search(body)
                dep_key = self._resolve_relative(rel, cp.group(1)) if cp else ""
                add(ImportEntry(edge_type="terragrunt", module=dep_key or "dependency",
                                name=(b.labels[0] if b.labels else ""),
                                line=b.line_start,
                                raw=(cp.group(0) if cp else "dependency"),
                                target_file=dep_key))
        for m in _RE_TG_FUNCS.finditer(clean):
            line = clean.count("\n", 0, m.start()) + 1
            add(ImportEntry(edge_type="terragrunt", module=m.group(1),
                            name=m.group(1), line=line, raw=m.group(0) + ")"))
        if os.path.basename(rel) == "inputs.hcl":
            add(ImportEntry(edge_type="terragrunt", module="inputs.hcl",
                            name="inputs.hcl", line=1, raw="inputs.hcl"))

        return edges

    # ---- path resolution -------------------------------------------------- #
    def _resolve_relative(self, referrer_file: str, source: str) -> str:
        """Resolve a (possibly interpolated, relative) source path to a repo-root
        relative, normalized module directory key.
        """
        s = _RE_INTERP.sub("", source).strip()
        # drop leading slashes left by stripped ${get_repo_root()}/...
        s = s.lstrip("/")
        if not s:
            return ""
        base = os.path.dirname(referrer_file)
        joined = os.path.normpath(os.path.join(base, s)) if (s.startswith(".") ) else os.path.normpath(s)
        return joined.replace(os.sep, "/")

    def _path_to_module_names(self, path: str) -> List[str]:
        """Canonical keys for a module directory: the normalized relpath and its
        basename (so lookups by either work)."""
        p = path.replace(os.sep, "/").strip("/")
        names = [p]
        if "/" in p:
            names.append(p.rsplit("/", 1)[1])
        return names

    # ---- public query interface (legacy_coder-compatible) ------------------- #
    def find_symbol(self, query: str, kind: Optional[str] = None) -> List[Tuple[str, Symbol]]:
        q = query.lower()
        out = []
        for rel, s in self._symbols:
            if kind and s.kind != kind:
                continue
            if q in s.name.lower() or q in s.signature.lower():
                out.append((rel, s))
        return out

    def get_file_outline(self, file_path: str) -> Optional[List[Symbol]]:
        info = self._index.get(self._norm_key(file_path))
        if not info:
            return None
        return info.symbols

    def get_imports(self, file_path: str) -> List[ImportEntry]:
        info = self._index.get(self._norm_key(file_path))
        return list(info.imports) if info else []

    def get_importers(self, module_name: str) -> List[str]:
        # match by full key or basename
        direct = self._importers.get(module_name)
        if direct:
            return sorted(direct)
        out = set()
        for key, refs in self._importers.items():
            if key.rsplit("/", 1)[-1] == module_name:
                out |= refs
        return sorted(out)

    def related(self, file_path: str) -> Dict[str, object]:
        """1-hop dependency view used by the `index_related` tool wrapper."""
        key = self._norm_key(file_path)
        imports = self.get_imports(key)
        module_dir = os.path.dirname(key)
        importers = []
        for name in self._path_to_module_names(module_dir):
            importers.extend(self.get_importers(name))
        return {
            "file": key,
            "imports": imports,
            "importers": sorted(set(importers)),
        }

    # ---- helpers ---------------------------------------------------------- #
    def _norm_key(self, file_path: str) -> str:
        fp = file_path.replace(os.sep, "/")
        if fp in self._index:
            return fp
        # accept absolute or partial paths
        if os.path.isabs(file_path):
            return self._rel(file_path)
        for key in self._index:
            if key.endswith(fp):
                return key
        return fp

    @property
    def files(self) -> List[str]:
        return sorted(self._index.keys())

    def all_symbols(self) -> List[Tuple[str, Symbol]]:
        return list(self._symbols)

    # ---- structural accessors used by the module catalog ------------------ #
    def iter_file_blocks(self, file_path: str):
        """Return (flat_blocks, clean_text) for a file, or ([], '') if unknown."""
        info = self._index.get(self._norm_key(file_path))
        if not info:
            return [], ""
        return list(iter_blocks(info.blocks)), info.clean

    def block_body(self, file_path: str, block: Block) -> str:
        info = self._index.get(self._norm_key(file_path))
        if not info:
            return ""
        return info.clean[block.body_start:block.body_end]

    def list_module_dirs(self) -> List[str]:
        """Directories that look like leaf Terraform modules: contain >=1 .tf
        file defining a resource or variable (i.e. reusable templates)."""
        dirs = set()
        for rel, info in self._index.items():
            if not rel.endswith(".tf"):
                continue
            if any(s.kind in ("resource", "variable", "data") for s in info.symbols):
                dirs.add(os.path.dirname(rel))
        return sorted(dirs)


# --------------------------------------------------------------------------- #
# Singleton accessor (borrowed pattern: get_*_index) — build once per repo root
# --------------------------------------------------------------------------- #
_INSTANCES: Dict[str, "TerraPilotIndex"] = {}


def get_index(root: str) -> "TerraPilotIndex":
    key = os.path.abspath(root)
    idx = _INSTANCES.get(key)
    if idx is None:
        idx = TerraPilotIndex(key).build()
        _INSTANCES[key] = idx
    return idx


def reset_index(root: Optional[str] = None) -> None:
    if root is None:
        _INSTANCES.clear()
    else:
        _INSTANCES.pop(os.path.abspath(root), None)
