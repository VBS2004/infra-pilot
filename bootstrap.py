"""bootstrap.py - new-project / new-env scaffolding for the compose loop (P2).

The composer assumes the destination already has its Terragrunt scaffolding:
    infra/root.hcl                          # repo-level dynamic source/backend (shared)
    infra/<project>/<provider>/common.hcl   # project-level shared inputs
    infra/<project>/<provider>/<env>/config.yml   # env-tier: terraform_module, account_id, region, environment

When the user targets a brand-new project or env, those files don't exist yet,
so a composed inputs.hcl can't actually `terragrunt apply`. This module detects
the gap and renders SCAFFOLDS for the missing pieces, modeled on a real sibling
in the same repo when one exists.

Guardrails (consistent with the rest of the loop):
  - root.hcl is shared repo infra; we NEVER auto-write it, only warn if absent.
  - account-specific values are NEVER borrowed from another project/env -- the
    account_id is always TODO'd so a human sets the correct account.
  - everything is review-before-apply; write_to_tree never clobbers an existing
    scaffold file.
"""
from __future__ import annotations

import dataclasses
import os
import re
from typing import List, Optional, Tuple

import paths

DEFAULT_REGION = "ap-south-1"

_ACCOUNT_LINE = re.compile(r'(account_id\s*=\s*)"[^"]*"')


@dataclasses.dataclass
class BootstrapFile:
    path: str
    text: str
    modeled_on: Optional[str] = None


@dataclasses.dataclass
class BootstrapPlan:
    files: List[BootstrapFile] = dataclasses.field(default_factory=list)
    notes: List[str] = dataclasses.field(default_factory=list)
    new_project: bool = False
    new_env: bool = False


def project_exists(repo: str, project: str, provider: str = paths.DEFAULT_PROVIDER) -> bool:
    return os.path.isdir(paths.project_dir(repo, project, provider))


def env_exists(repo: str, project: str, env: str, provider: str = paths.DEFAULT_PROVIDER) -> bool:
    return os.path.isdir(paths.env_dir(repo, project, env, provider))


def _find_template_env_config(repo: str, project: str, provider: str) -> Optional[str]:
    """A real config.yml to model on: prefer another env in the SAME project,
    else any other project's env config."""
    ordered = [project] + [p for p in paths.list_projects(repo, provider) if p != project]
    for p in ordered:
        for e in paths.list_envs(repo, p, provider=provider):
            cp = paths.env_config_path(repo, p, e, provider)
            if os.path.exists(cp):
                return cp
    return None


def _find_template_common(repo: str, project: str, provider: str) -> Optional[str]:
    """Any other project's common.hcl to model on."""
    for p in paths.list_projects(repo, provider):
        if p == project:
            continue
        cp = paths.common_hcl_path(repo, p, provider)
        if os.path.exists(cp):
            return cp
    return None


def scaffold_env_config(repo: str, project: str, env: str,
                        provider: str = paths.DEFAULT_PROVIDER, *,
                        environment: Optional[str] = None,
                        notes: Optional[List[str]] = None) -> Tuple[str, Optional[str]]:
    import root_schema
    schema = root_schema.get_schema(repo)
    notes = notes if notes is not None else []
    tmpl = _find_template_env_config(repo, project, provider)
    cfg = {}
    modeled = None
    if tmpl:
        modeled = os.path.relpath(tmpl, repo)
        cfg = paths._parse_flat_yaml(open(tmpl, encoding="utf-8", errors="replace").read())
    
    # Base fields we always try to provide sensible defaults for
    tm = cfg.get("terraform_module") or "env"
    region = cfg.get("region") or DEFAULT_REGION
    environment = environment or env
    
    # We use schema to know which fields are REQUIRED in this repo
    required_fields = schema.env_config_fields
    if not required_fields:
        required_fields = ["environment", "terraform_module", "region", "account_id"]
        
    lines = [f"# {schema.env_config_filename} - env-tier settings (SCAFFOLD: review TODOs before apply)"]
    if modeled:
        lines.append(f"# modeled on {modeled} (account_id intentionally TODO -- set this env's account)")
        
    # Emit required fields first
    emitted = set()
    for field in required_fields:
        emitted.add(field)
        if field == "environment":
            lines.append(f"{field}: {environment}")
        elif field == "terraform_module":
            lines.append(f"{field}: {tm}")
        elif field == "region":
            lines.append(f"{field}: {region}")
        elif field in ("account_id", "aws_account_id", "account"):
            lines.append(f'{field}: "TODO"   # account-specific -- set the AWS account ID for this env-tier')
        else:
            val = cfg.get(field)
            if val is not None:
                lines.append(f"{field}: {val}   # copied from template")
            else:
                lines.append(f"{field}: TODO   # required field")
                
    # Surface any other template keys so they aren't silently lost, as TODO.
    skip = {"environment", "terraform_module", "region", "account_id",
            "aws_account_id", "account"}
    for k in (cfg or {}):
        if k in skip or k in emitted:
            continue
        lines.append(f"{k}: TODO   # present in {modeled}; review/fill")
        
    notes.append(f"bootstrap: scaffolded env {schema.env_config_filename} "
                 + (f"(modeled on {modeled})" if modeled
                    else "(no template found; minimal scaffold)"))
    return "\n".join(lines) + "\n", modeled


def scaffold_common_hcl(repo: str, project: str,
                        provider: str = paths.DEFAULT_PROVIDER, *,
                        notes: Optional[List[str]] = None) -> Tuple[str, Optional[str]]:
    notes = notes if notes is not None else []
    tmpl = _find_template_common(repo, project, provider)
    if tmpl:
        modeled = os.path.relpath(tmpl, repo)
        text = open(tmpl, encoding="utf-8", errors="replace").read()
        tmpl_proj = modeled.replace("\\", "/").split("/")[1] if "/" in modeled else None
        if tmpl_proj and tmpl_proj != project:
            text = re.sub(r"\b" + re.escape(tmpl_proj) + r"\b", project, text)
        # NEVER carry another project's account id across -- TODO it out.
        text = _ACCOUNT_LINE.sub(r'\1"TODO"', text)
        header = (f"# common.hcl for {project} ({provider}) -- SCAFFOLD modeled on {modeled}.\n"
                  f"# Project token '{tmpl_proj}' -> '{project}'; account_id TODO'd out "
                  f"(account-specific).\n"
                  f"# Review every value (default_tags, backend, account) before apply.\n")
        notes.append(f"bootstrap: scaffolded common.hcl (modeled on {modeled}; "
                     f"account_id TODO'd, '{tmpl_proj}'->'{project}')")
        return header + text, modeled
    # No template anywhere -> minimal documented scaffold.
    region = DEFAULT_REGION
    tc = _find_template_env_config(repo, project, provider)
    if tc:
        region = paths._parse_flat_yaml(
            open(tc, encoding="utf-8", errors="replace").read()).get("region") or region
    text = (
        f"# common.hcl - project-level shared inputs for {project} ({provider})\n"
        f"# SCAFFOLD: no existing common.hcl found to model on; fill TODOs before apply.\n"
        "locals {\n"
        '  account_id = "TODO"   # AWS account ID (account-specific)\n'
        f'  region     = "{region}"\n'
        "  default_tags = {\n"
        f'    Project = "{project}"\n'
        "    # add org-required business tags (e.g. UsedFor, CostCenter, Environment)\n"
        "  }\n"
        "}\n"
    )
    notes.append("bootstrap: scaffolded minimal common.hcl (no template found)")
    return text, None


def plan(repo: str, project: str, env: str,
         provider: str = paths.DEFAULT_PROVIDER, *,
         environment: Optional[str] = None) -> BootstrapPlan:
    """Detect missing project/env scaffolding and render scaffolds for the gaps.
    Returns an empty plan (no files) when the destination is already set up, so
    existing flows are completely unaffected."""
    notes: List[str] = []
    files: List[BootstrapFile] = []
    new_project = not project_exists(repo, project, provider)
    new_env = not env_exists(repo, project, env, provider)

    import root_schema
    schema = root_schema.get_schema(repo)
    if not os.path.exists(paths.root_hcl_path(repo)):
        notes.append(f"bootstrap WARNING: infra/{schema.root_file_name} not found -- this shared "
                     f"repo-level file is required and is NOT auto-scaffolded; "
                     f"create it before apply")

    common_p = paths.common_hcl_path(repo, project, provider)
    if not os.path.exists(common_p):
        if new_project:
            notes.append(f"bootstrap: project '{project}' has no {provider} tree -- "
                         f"scaffolding project + env files")
        text, modeled = scaffold_common_hcl(repo, project, provider, notes=notes)
        files.append(BootstrapFile(common_p, text, modeled))

    cfg_p = paths.env_config_path(repo, project, env, provider)
    if not os.path.exists(cfg_p):
        if new_env and not new_project:
            notes.append(f"bootstrap: env '{env}' has no dir under {project}/{provider} "
                         f"-- scaffolding config.yml")
        text, modeled = scaffold_env_config(repo, project, env, provider,
                                            environment=environment, notes=notes)
        files.append(BootstrapFile(cfg_p, text, modeled))

    return BootstrapPlan(files=files, notes=notes,
                         new_project=new_project, new_env=new_env)
