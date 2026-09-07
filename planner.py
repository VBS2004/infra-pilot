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

from index import TerraPilotIndex
from catalog import ModuleCatalog, ModuleEntry

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
        candidates = self.catalog.match(intent, top=3)
        if candidates and self._is_relevant(intent, candidates[0]):
            m = candidates[0]
            plan = Plan(intent=intent, decision="reuse", module=m,
                        candidates=candidates,
                        required_inputs=[i.name for i in m.required_inputs],
                        optional_inputs=[i.name for i in m.optional_inputs])
            plan.rendered = self.render_inputs_hcl(m, project, env_tier)
            plan.notes.append(
                f"Reusing '{m.key}' (used by {m.reuse_count} existing config(s)). "
                f"Write only inputs.hcl wiring — no net-new .tf.")
            return plan
        # write-new path
        plan = Plan(intent=intent, decision="write_new", candidates=candidates)
        nearest = candidates[0] if candidates else None
        plan.style_reference = nearest.key if nearest else None
        plan.rendered = self.scaffold_new_module(intent, project, nearest)
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
    def render_inputs_hcl(self, m: ModuleEntry, project: str, env_tier: str) -> str:
        """Emit a terragrunt.hcl that reuses module `m`. Wires required inputs;
        replicates the remote_state upstream pattern if siblings use it."""
        # learn upstream wiring from an existing consumer of this module
        upstreams = self._learn_upstream_pattern(m)
        # relative source from the conventional env-tier location
        # live/<env>/<project>/<name>/terragrunt.hcl -> modules/.../<name>
        depth_prefix = "../../../../"
        source = depth_prefix + m.key

        lines = []
        lines.append('include "root" {')
        lines.append('  path = find_in_parent_folders()')
        lines.append('}')
        lines.append('')
        lines.append('terraform {')
        lines.append(f'  source = "{source}"')
        lines.append('}')
        lines.append('')
        for up in upstreams:
            lines.append(f'dependency "{up}" {{')
            lines.append(f'  config_path = "../{up}"')
            lines.append('}')
            lines.append('')
        if upstreams:
            lines.append('locals {')
            lines.append('  upstream_modules = {')
            for up in upstreams:
                lines.append(f'    {up} = "{up}"')
            lines.append('  }')
            lines.append('}')
            lines.append('')
        lines.append('inputs = {')
        for i in m.required_inputs:
            placeholder = self._placeholder(i.name, i.type, project, env_tier, upstreams)
            lines.append(f'  {i.name} = {placeholder}')
        # optional inputs that map to a learned upstream get actively wired;
        # the rest stay commented so module defaults apply.
        wired_optional, commented_optional = [], []
        for i in m.optional_inputs:
            ph = self._placeholder(i.name, i.type, project, env_tier, upstreams)
            if 'module.remote_state' in ph or 'dependency.' in ph:
                wired_optional.append((i, ph))
            else:
                commented_optional.append(i)
        for i, ph in wired_optional:
            lines.append(f'  {i.name} = {ph}')
        if commented_optional:
            lines.append('')
            lines.append('  # optional (module defaults apply if omitted):')
            for i in commented_optional:
                lines.append(f'  # {i.name} = ...   # {i.type}')
        lines.append('}')
        return "\n".join(lines) + "\n"

    def _learn_upstream_pattern(self, m: ModuleEntry) -> List[str]:
        ups = []
        for consumer in m.used_by:
            for imp in self.idx.get_imports(consumer):
                if imp.edge_type in ("remote_state", "output_ref") and imp.name:
                    if imp.name not in ups and imp.name != m.name:
                        ups.append(imp.name)
        return ups

    def _placeholder(self, name: str, vtype: str, project: str,
                     env_tier: str, upstreams: List[str]) -> str:
        n = name.lower()
        if name == "environment":
            return "local.env.locals.environment"
        if "security_group_ids" in n and upstreams:
            up = next((u for u in upstreams if "security" in u), upstreams[0])
            return (f'[module.remote_state.components[var.upstream_modules.{up}]'
                    f'["security_group_id"]]')
        if vtype.startswith("list"):
            return "[]   # TODO"
        if vtype.startswith("number"):
            return "0    # TODO"
        if vtype.startswith("bool"):
            return "false # TODO"
        return '""   # TODO'

    def scaffold_new_module(self, intent: str, project: str,
                            nearest: Optional[ModuleEntry]) -> str:
        rtype = self._guess_resource_type(intent)
        name = self._guess_module_name(intent)
        ref = f"  # style reference: {nearest.key}" if nearest else ""
        env_var = chr(36) + chr(123) + "var.environment" + chr(125)
        files = {
            f"modules/aws/resource/{name}/main.tf":
                (f'resource "{rtype}" "this" {{{ref}\n'
                 f'  # TODO: generator fills the body, grounded in retrieved siblings\n'
                 f'  tags = merge(local.default_tags, {{ Name = "{project}-{env_var}-{name}" }})\n'
                 f'}}\n\nlocals {{\n  default_tags = {{ Project = "{project}", Environment = var.environment }}\n}}\n'),
            f"modules/aws/resource/{name}/variables.tf":
                'variable "environment" {\n  type = string\n}\n# TODO: add the inputs this resource needs\n',
            f"modules/aws/resource/{name}/outputs.tf":
                'output "id" {\n  value = ' + rtype + '.this.id\n}\n',
        }
        out = [f"# NET-NEW module scaffold for: {intent}",
               f"# guessed resource type: {rtype}, module name: {name}", ""]
        for path, body in files.items():
            out.append(f"# ===== {path} =====")
            out.append(body)
        return "\n".join(out)

    def _guess_resource_type(self, intent: str) -> str:
        table = {
            "ec2": "aws_instance", "instance": "aws_instance",
            "security group": "aws_security_group", "sg": "aws_security_group",
            "s3": "aws_s3_bucket", "bucket": "aws_s3_bucket",
            "rds": "aws_db_instance", "database": "aws_db_instance",
            "alb": "aws_lb", "load balancer": "aws_lb",
            "lambda": "aws_lambda_function", "sqs": "aws_sqs_queue",
        }
        low = intent.lower()
        for k, v in table.items():
            if k in low:
                return v
        return "aws_RESOURCE"

    def _guess_module_name(self, intent: str) -> str:
        rt = self._guess_resource_type(intent)
        return rt.replace("aws_", "").replace("_instance", "").replace("db", "rds") \
            if rt != "aws_RESOURCE" else "new_resource"
