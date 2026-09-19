"""
Reuse-vs-write planner + emitter — the deterministic core of the compose loop.

Given an intent ("I want an EC2 for the billing project"), it:
  1. asks the ModuleCatalog for matching reusable templates,
  2. DECIDES reuse-vs-write,
  3a. reuse  -> emits a terragrunt.hcl that `source`s the existing module and
               wires its required inputs (learning the remote_state upstream
               pattern from sibling configs that already use the module), or
  3b. write  -> scaffolds a net-new leaf module (main/variables/outputs.tf)
               with the nearest existing module as a style reference.

No LLM here: this is the deterministic retrieval + planning shell. A generator
(any OpenAI-compatible model) fills the resource bodies and
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
    def plan(self, intent: str, project: str = "myproject",
             env_tier: str = "nonprod") -> Plan:
        convention = detect_convention(self.idx.root)
        emitter = get_emitter(convention.kind)
        
        candidates = self._rank(intent)
        if candidates:
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
        nearest_all = self.catalog.match(intent, top=3)
        plan = Plan(intent=intent, decision="write_new", candidates=nearest_all)
        nearest = nearest_all[0] if nearest_all else None
        plan.style_reference = nearest.key if nearest else None
        plan.rendered = emitter.scaffold_new_module(intent, project, nearest)
        plan.notes.append(
            "No sufficiently-matching module exists — scaffold a net-new leaf "
            "module. Nearest existing module is offered as a style reference."
            if nearest else
            "No existing module matched — scaffold a net-new leaf module from scratch.")
        return plan

    # Words that carry no resource-type information in a request or a module name.
    _GENERIC = {
        "aws", "azurerm", "google", "resource", "resources", "module", "modules", "main",
        "this", "the", "a", "an", "for", "in", "of", "to", "with", "and", "or", "on", "at",
        "create", "deploy", "provision", "new", "my", "i", "we", "want", "need", "please",
        "project", "like", "similar", "same", "as", "env", "environment", "nonprod", "prod",
        "dev", "staging", "just", "some", "add", "make", "set", "up",
    }

    @staticmethod
    def _toks(text: str) -> set:
        return {t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if t}

    def _rank(self, intent: str) -> List[ModuleEntry]:
        """Modules relevant to the request, best first. A module scores 2 per
        request word found in its provider resource types (aws_db_instance ->
        {db, instance}) and 1 per word found in its name/path; words that carry no
        type information (aws, resource, create, ...) never count. Zero score =
        not a reuse candidate. Replaces first-match token overlap, which reused an
        unrelated module on a single shared generic word."""
        from terra_pilot.search.retrieval import _TYPE_ALIASES
        words = self._toks(intent) - self._GENERIC
        want = set(words)
        for w in words:
            want |= _TYPE_ALIASES.get(w, set())
        want -= self._GENERIC
        scored = []
        for order, m in enumerate(self.catalog.modules.values()):
            types = set()
            for rt in m.resource_types:
                types |= self._toks(rt.replace("_", " "))
            types -= self._GENERIC
            name = self._toks(m.key.replace("_", " ").replace("-", " ")) - self._GENERIC
            score = 2 * len(want & types) + len(want & name)
            # A short request is a resource-type phrase ("iam role policy"): at least
            # half of its words must be explained by the module, so one shared
            # generic word ("network", "attachment") is not enough. A long sentence
            # keeps the any-match rule (its extra words are prose).
            if score and len(words) <= 5:
                hay = types | name
                covered = sum(1 for w in words if (({w} | _TYPE_ALIASES.get(w, set())) & hay))
                if covered * 2 < len(words):
                    continue
            if score:
                scored.append((-score, -m.reuse_count, order, m))
        scored.sort(key=lambda t: t[:3])
        return [t[3] for t in scored[:3]]

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

