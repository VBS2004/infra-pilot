"""
Module catalog — the "reuse-vs-write" knowledge layer.

Indexes every reusable leaf module (a dir with .tf defining resources/variables)
and records, per module:
  * resource_types  it provisions  (aws_instance, aws_security_group, ...)
  * inputs          required vs optional (a variable is REQUIRED iff it has no
                    `default`), plus type + description
  * outputs         names it exposes (what upstream consumers can wire)
  * reuse_count     how many env-tier configs already `source` it
                    (the strongest reuse signal — from get_importers)

`match(intent)` ranks modules for a natural-language request so the planner can
decide reuse-vs-write. Uses the bundled BM25 (rank_bm25 if installed).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from index import TerraPilotIndex
from lexical_search import _make_bm25, tokenize_code

_RE_DEFAULT = re.compile(r'(^|\n)[ \t]*default[ \t]*=')
_RE_TYPE = re.compile(r'(^|\n)[ \t]*type[ \t]*=[ \t]*([^\n]+)')
_RE_DESC = re.compile(r'description[ \t]*=[ \t]*"([^"]*)"')


@dataclass
class Input:
    name: str
    required: bool
    type: str = ""
    description: str = ""


@dataclass
class ModuleEntry:
    key: str                       # repo-root relative dir, e.g. modules/aws/resource/ec2
    name: str                      # basename, e.g. ec2
    resource_types: List[str] = field(default_factory=list)
    inputs: List[Input] = field(default_factory=list)
    outputs: List[str] = field(default_factory=list)
    reuse_count: int = 0
    used_by: List[str] = field(default_factory=list)

    @property
    def required_inputs(self) -> List[Input]:
        return [i for i in self.inputs if i.required]

    @property
    def optional_inputs(self) -> List[Input]:
        return [i for i in self.inputs if not i.required]


class ModuleCatalog:
    def __init__(self, idx: TerraPilotIndex):
        self.idx = idx
        self.modules: Dict[str, ModuleEntry] = {}
        self._bm25 = None
        self._order: List[str] = []
        self.build()

    def build(self) -> "ModuleCatalog":
        import root_schema
        schema = root_schema.get_schema(self.idx.root)
        
        # Determine the static prefix of the module source template
        # e.g., "modules/{provider}/infrastructure/..." -> "modules"
        tmpl = schema.module_source_template
        idx_brace = tmpl.find("{")
        base_prefix = tmpl[:idx_brace].rstrip("/") if idx_brace > 0 else tmpl
        
        for mod_dir in self.idx.list_module_dirs():
            # Only index modules that are within the repo's designated module source area
            if not mod_dir.startswith(base_prefix):
                continue
                
            entry = ModuleEntry(key=mod_dir, name=os.path.basename(mod_dir))
            for rel in self.idx.files:
                if os.path.dirname(rel) != mod_dir or not rel.endswith(".tf"):
                    continue
                blocks, clean = self.idx.iter_file_blocks(rel)
                for b in blocks:
                    if b.parent is not None:
                        continue
                    if b.type == "resource" and b.labels:
                        if b.labels[0] not in entry.resource_types:
                            entry.resource_types.append(b.labels[0])
                    elif b.type == "variable" and b.labels:
                        body = clean[b.body_start:b.body_end]
                        tym = _RE_TYPE.search(body)
                        dem = _RE_DESC.search(body)
                        entry.inputs.append(Input(
                            name=b.labels[0],
                            required=not bool(_RE_DEFAULT.search(body)),
                            type=(tym.group(2).strip() if tym else ""),
                            description=(dem.group(1) if dem else ""),
                        ))
                    elif b.type == "output" and b.labels:
                        if b.labels[0] not in entry.outputs:
                            entry.outputs.append(b.labels[0])
            # reuse evidence
            importers = set()
            for nm in self.idx._path_to_module_names(mod_dir):
                importers.update(self.idx.get_importers(nm))
            entry.used_by = sorted(importers)
            entry.reuse_count = len(importers)
            self.modules[mod_dir] = entry

        # build BM25 over module "documents"
        self._order = list(self.modules.keys())
        docs = []
        for k in self._order:
            m = self.modules[k]
            doc = " ".join([m.name, m.key.replace("/", " "),
                            " ".join(m.resource_types),
                            " ".join(i.name for i in m.inputs),
                            " ".join(m.outputs)])
            docs.append(tokenize_code(doc))
        self._bm25 = _make_bm25(docs) if docs else None
        return self

    def get(self, key: str) -> Optional[ModuleEntry]:
        return self.modules.get(key)

    def match(self, intent: str, top: int = 3) -> List[ModuleEntry]:
        """Rank modules for a request. Combines BM25 over the module doc with a
        boost for resource-type / name token hits and existing reuse."""
        if not self._bm25:
            return []
        q = tokenize_code(intent)
        scores = list(self._bm25.get_scores(q))
        qset = set(q)
        ranked = []
        for i, key in enumerate(self._order):
            m = self.modules[key]
            score = scores[i]
            # boost: intent token appears in module name or a resource type
            name_toks = set(tokenize_code(m.name + " " + " ".join(m.resource_types)))
            if qset & name_toks:
                score += 3.0
            score += 0.25 * m.reuse_count   # mild preference for proven modules
            ranked.append((score, m))
        ranked.sort(key=lambda t: t[0], reverse=True)
        return [m for s, m in ranked if s > 0][:top]
