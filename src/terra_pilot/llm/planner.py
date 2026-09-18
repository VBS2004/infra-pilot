"""
Reuse-vs-write planner + emitter — the deterministic core of the compose loop.

Given an intent ("I want an EC2 for the payments project"), it:
  1. asks the ModuleCatalog for matching reusable templates,
  2. DECIDES reuse-vs-write,
  3a. reuse  -> emits a terragrunt.hcl that `source`s the existing module and
               wires its required inputs (learning the remote_state upstream
               pattern from sibling configs that already use the module), or
  3b. write  -> scaffolds a net-new leaf module (main/variables/outputs.tf)
               with the nearest existing module as a style reference.

No LLM here: this is the deterministic retrieval + planning shell. A generator
(local Qwen2.5-Coder / hosted Qwen3-Coder-30B) fills the resource bodies and
free-form values; everything structural is grounded in the index.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from terra_pilot.search.index import TerraPilotIndex
from terra_pilot.search.catalog import ModuleCatalog, ModuleEntry
from terra_pilot.core.convention import detect_convention
from terra_pilot.pipeline.emitters import get_emitter

_RE_REMOTE_STATE = re.compile(
    r'module\.remote_state\.components\[\s*var\.upstream_modules\.'
    r'([A-Za-z_][\w-]*)\s*\]\s*\[\s*"([^"]+)"\s*\]'
)


@dataclass
class Plan:
    intent: str
    decision: str                       # "reuse" | "write_new"
    module: Optional[ModuleEntry] = None
    candidates: List[ModuleEntry] = field(default_factory=list)
    required_inputs: List[str] = field(default_factory=list)
    optional_inputs: List[str] = field(default_factory=list)
    style_reference: Optional[str] = None
    rendered: str = ""
    notes: List[str] = field(default_factory=list)


class Planner:
    def __init__(self, idx: TerraPilotIndex, catalog: Optional[ModuleCatalog] = None):
        self.idx = idx
        self.catalog = catalog or ModuleCatalog(idx)

    # --- decision ---------------------------------------------------------- #
    def plan(self, intent: str, project: str = "payments",
             env_tier: str = "nonprod") -> Plan:
        convention = detect_convention(self.idx.root)
        emitter = get_emitter(convention.kind)
        
        candidates = self.catalog.match(intent, top=3)
        if candidates and self._is_relevant(intent, candidates[0]):
            m = candidates[0]
            plan = Plan(intent=intent, decision="reuse", module=m,
                        candidates=candidates,
                        required_inputs=[i.name for i in m.required_inputs],
                        optional_inputs=[i.name for i in m.optional_inputs])
            upstreams = self._learn_upstream_pattern(m)
            plan.rendered = emitter.render_module_call(m, project, env_tier, upstreams)
            plan.notes.append(
                f"Reusing '{m.key}' (used by {m.reuse_count} existing config(s)). "
                f"Write only inputs.hcl wiring — no net-new .tf.")
            return plan
        # write-new path
        plan = Plan(intent=intent, decision="write_new", candidates=candidates)
        nearest = candidates[0] if candidates else None
        plan.style_reference = nearest.key if nearest else None
        plan.rendered = emitter.scaffold_new_module(intent, project, nearest)
        plan.notes.append(
            "No sufficiently-matching module exists — scaffold a net-new leaf "
            "module. Nearest existing module is offered as a style reference."
            if nearest else
            "No existing module matched — scaffold a net-new leaf module from scratch.")
        return plan

    def _is_relevant(self, intent: str, m: ModuleEntry) -> bool:
        """A match is a reuse candidate if the intent names the module or one of
        its resource types (e.g. 'ec2' -> ec2 / aws_instance)."""
        toks = set(re.findall(r"[a-z0-9]+", intent.lower()))
        hay = set(re.findall(r"[a-z0-9]+", (m.name + " " + " ".join(m.resource_types)).lower()))
        # common aliases
        alias = {"ec2": "instance", "sg": "security", "lb": "alb"}
        for a, b in alias.items():
            if a in toks:
                hay.add(a)
        return bool(toks & hay)

    # --- emitters ---------------------------------------------------------- #
    # --- helpers ----------------------------------------------------------- #

    def _learn_upstream_pattern(self, m: ModuleEntry) -> List[str]:
        ups = []
        for consumer in m.used_by:
            for imp in self.idx.get_imports(consumer):
                if imp.edge_type in ("remote_state", "output_ref") and imp.name:
                    if imp.name not in ups and imp.name != m.name:
                        ups.append(imp.name)
        return ups

