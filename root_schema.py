"""root_schema.py — discover a Terragrunt repo's structural conventions by
reading real component terragrunt.hcl files, not by hardcoding "root.hcl".

HOW DISCOVERY WORKS
-------------------
Terragrunt repos use `find_in_parent_folders()` in component-level
terragrunt.hcl files to locate a "root" config file. That file can be named
ANYTHING:
  - root.hcl        (modern recommended name)
  - terragrunt.hcl  (legacy, no-arg find_in_parent_folders())
  - account.hcl, region.hcl, env.hcl, common.hcl ...

So we discover the root by:
  1. Scanning all component-level terragrunt.hcl files in the repo
  2. Finding the first `include` block that calls find_in_parent_folders("X")
     (or no-arg version → defaults to "terragrunt.hcl")
  3. Walking UP from that component directory until we find a file named X
  4. Parsing that root file for:
       - terraform.source pattern → module path formula
       - locals.find_in_parent_folders("config.yml") → env config filename
       - remote_state.backend → backend type
       - generate "provider" → provider name
       - local.env_cfg.* references → required env config fields

WHAT WE EXTRACT AND HOW WE USE IT
----------------------------------
  RepoSchema.root_file_path      → absolute path to the root HCL file
  RepoSchema.root_file_name      → just the filename ("root.hcl", etc.)
  RepoSchema.include_target      → what component terragrunt.hcl includes
  RepoSchema.module_source_template  → e.g. "modules/{provider}/infrastructure/{terraform_module}/{component}"
  RepoSchema.env_config_filename → e.g. "config.yml"
  RepoSchema.env_config_fields   → ["terraform_module", "region", "account_id"]
  RepoSchema.backend_type        → "s3", "gcs", "azurerm", etc.
  RepoSchema.provider_name       → "aws", "google", "azurerm"
  RepoSchema.confidence          → "full" | "partial" | "fallback"

FALLBACK BEHAVIOUR
------------------
If discovery fails at ANY step, we return a schema with confidence="fallback"
whose values match the current hardcoded defaults in paths.py. This ensures
100% backward compatibility — every existing test passes unchanged.
"""
from __future__ import annotations

import dataclasses
import os
import re
from functools import lru_cache
from typing import List, Optional


# ---------------------------------------------------------------------------
# Regex helpers
# ---------------------------------------------------------------------------

# find_in_parent_folders("some-file.hcl") — capture the filename arg
_RE_FIND_PARENT = re.compile(
    r'\bfind_in_parent_folders\s*\(\s*(?:"([^"]+)"|\'([^\']+)\')\s*\)'
)
# find_in_parent_folders() — no-arg form (defaults to terragrunt.hcl)
_RE_FIND_PARENT_NOARG = re.compile(r'\bfind_in_parent_folders\s*\(\s*\)')

# terraform { source = "..." } — capture the source value
_RE_SOURCE = re.compile(r'\bsource\s*=\s*"([^"]+)"')

# local.env_cfg.field_name references — captures the field name
_RE_ENV_CFG_FIELD = re.compile(r'\blocal\.env_cfg\.([A-Za-z_]\w*)\b')

# remote_state { backend = "..." }
_RE_BACKEND = re.compile(r'\bbackend\s*=\s*"([^"]+)"')

# generate "provider" { contents = <<EOF ... provider "X" { EOF }
_RE_PROVIDER_NAME = re.compile(r'\bprovider\s+"([^"]+)"\s*\{')

# Terragrunt interpolation functions in source strings — we strip these to
# recover the literal path segments between them
_RE_TG_INTERP = re.compile(r'\$\{[^}]+\}')

# Path segments from the source template:
# modules//aws/infrastructure/${local.env_cfg.terraform_module}/${basename(...)}
# → we extract the literal "modules", "aws", "infrastructure" prefix
_RE_MODULES_PREFIX = re.compile(
    r'(?:modules)//([^/]+)/infrastructure/?\$\{[^}]+terraform_module[^}]*\}/\$\{[^}]+\}'
)


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class RepoSchema:
    """Structural conventions extracted from a Terragrunt repo's root HCL file.

    All path-formula fields use Python format-string placeholders so callers
    can use them with str.format(provider=..., terraform_module=..., component=...).
    """

    # Discovered root file info
    root_file_path: Optional[str]       # absolute path to the root HCL file
    root_file_name: str                 # just the filename ("root.hcl", "terragrunt.hcl", …)

    # What component terragrunt.hcl files include
    # (e.g. 'find_in_parent_folders("root.hcl")' → "root.hcl")
    # None means no-arg find_in_parent_folders() → defaults to "terragrunt.hcl"
    include_target: str

    # Module source path formula. Segments are named placeholders:
    #   {provider}         — cloud provider, e.g. "aws"
    #   {terraform_module} — tier from config.yml, e.g. "env"
    #   {component}        — component name, e.g. "ec2"
    module_source_template: str   # e.g. "modules/{provider}/infrastructure/{terraform_module}/{component}"

    # The config file that each env-tier must provide (found via find_in_parent_folders)
    env_config_filename: str      # e.g. "config.yml"

    # Fields referenced in the root as local.env_cfg.X — these are REQUIRED in config.yml
    env_config_fields: List[str]  # e.g. ["terraform_module", "region", "account_id"]

    # Backend / provider info (informational — useful for LLM context)
    backend_type: str             # e.g. "s3", "gcs", "azurerm", ""
    provider_name: str            # e.g. "aws", "google", "azurerm", ""

    # Confidence in the extraction
    confidence: str               # "full" | "partial" | "fallback"

    # Raw text of the root file (for LLM context, kept in memory once loaded)
    root_file_text: Optional[str] = None

    def as_prompt_context(self) -> str:
        """Return a compact text summary suitable for injecting into the LLM prompt."""
        lines = [
            f"# Repo conventions (extracted from {self.root_file_name})",
            f"# confidence: {self.confidence}",
            f"# Module source formula: {self.module_source_template}",
            f"# Env-config file:       {self.env_config_filename}",
            f"# Required config fields: {', '.join(self.env_config_fields) or 'unknown'}",
        ]
        if self.backend_type:
            lines.append(f"# Backend:  {self.backend_type}")
        if self.provider_name:
            lines.append(f"# Provider: {self.provider_name}")
        return "\n".join(lines)

    def module_source_dir(self, repo_root: str, provider: str,
                          terraform_module: str, component: str) -> str:
        """Resolve the module source directory for a component using this schema."""
        rel = self.module_source_template.format(
            provider=provider,
            terraform_module=terraform_module,
            component=component,
        )
        return os.path.join(repo_root, rel)

    def standard_terragrunt_hcl(self) -> str:
        """Return the boilerplate include block for a new component's terragrunt.hcl."""
        if self.include_target == "terragrunt.hcl":
            # Legacy no-arg style
            return 'include {\n  path = find_in_parent_folders()\n}\n'
        return (
            f'include {{\n'
            f'  path = find_in_parent_folders("{self.include_target}")\n'
            f'}}\n'
        )


# ---------------------------------------------------------------------------
# Default / fallback schema (mirrors current hardcoded paths.py behaviour)
# ---------------------------------------------------------------------------

_FALLBACK_SCHEMA = RepoSchema(
    root_file_path=None,
    root_file_name="root.hcl",
    include_target="root.hcl",
    module_source_template="modules/{provider}/infrastructure/{terraform_module}/{component}",
    env_config_filename="config.yml",
    env_config_fields=["terraform_module", "region", "account_id"],
    backend_type="s3",
    provider_name="aws",
    confidence="fallback",
)


# ---------------------------------------------------------------------------
# Discovery: find the root HCL filename from a leaf component's terragrunt.hcl
# ---------------------------------------------------------------------------

def _find_leaf_terragrunt_hcls(repo_root: str, max_scan: int = 50) -> List[str]:
    """Return paths to component-level terragrunt.hcl files (not the root one).

    A component-level terragrunt.hcl will have an `include` block with
    find_in_parent_folders. The repo-root one typically doesn't.
    We scan up to max_scan files for speed.
    """
    found: List[str] = []
    skip_dirs = {".terragrunt-cache", ".terraform", ".git", ".venv", "node_modules"}
    for dirpath, dirnames, filenames in os.walk(repo_root):
        # Prune directories we should never descend into
        dirnames[:] = [d for d in dirnames if d not in skip_dirs and not d.startswith(".")]
        if "terragrunt.hcl" in filenames:
            p = os.path.join(dirpath, "terragrunt.hcl")
            found.append(p)
            if len(found) >= max_scan:
                break
    return found


def _extract_include_target(text: str) -> Optional[str]:
    """Extract what filename find_in_parent_folders() is looking for in this file.

    Returns:
      - "terragrunt.hcl"  if no-arg find_in_parent_folders() found
      - "root.hcl"        if find_in_parent_folders("root.hcl") found
      - None              if no find_in_parent_folders found at all
    """
    m = _RE_FIND_PARENT.search(text)
    if m:
        return m.group(1) or m.group(2)  # group 1 = double-quoted, 2 = single-quoted
    if _RE_FIND_PARENT_NOARG.search(text):
        return "terragrunt.hcl"
    return None


def _discover_root_file(repo_root: str) -> Optional[tuple]:
    """Discover (root_file_path, include_target) for this repo.

    Strategy:
    1. Scan component-level terragrunt.hcl files
    2. Find one with find_in_parent_folders("X")
    3. Walk up the directory tree from that component to find the file named X
    4. Return (absolute_path_to_root_file, include_target_name)
    """
    candidates = _find_leaf_terragrunt_hcls(repo_root)
    repo_abs = os.path.abspath(repo_root)

    for leaf_path in candidates:
        try:
            with open(leaf_path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue

        target = _extract_include_target(text)
        if target is None:
            continue

        # Walk up from this component's directory toward the repo root
        search_dir = os.path.dirname(leaf_path)
        
        # If we are looking for a file with the same name as the leaf (e.g. terragrunt.hcl 
        # looking for terragrunt.hcl), start from the parent directory so we don't just find ourselves.
        if target == os.path.basename(leaf_path):
            search_dir = os.path.dirname(search_dir)
            
        while True:
            candidate = os.path.join(search_dir, target)
            if os.path.isfile(candidate):
                return (os.path.abspath(candidate), target)
            parent = os.path.dirname(search_dir)
            if parent == search_dir:
                # Reached filesystem root without finding the file
                break
            # Don't walk above the repo root
            if not os.path.abspath(search_dir).startswith(repo_abs):
                break
            search_dir = parent

    # Fallback: look for common root file names directly at repo root
    for name in ("root.hcl", "terragrunt.hcl", "account.hcl", "common.hcl"):
        p = os.path.join(repo_abs, name)
        if os.path.isfile(p):
            return (p, name)

    return None


# ---------------------------------------------------------------------------
# Parsing the root HCL file
# ---------------------------------------------------------------------------

def _parse_module_source_template(source: str) -> Optional[str]:
    """Convert a Terragrunt terraform.source string into a portable template.

    Input examples:
      "${get_parent_terragrunt_dir()}/modules//aws/infrastructure/${local.env_cfg.terraform_module}/${basename(get_original_terragrunt_dir())}/"
      "../../../../modules/aws/resource/ec2"

    Output:
      "modules/{provider}/infrastructure/{terraform_module}/{component}"
      or None if we can't parse it.
    """
    # Pattern 1: the standard Acme-style dynamic source
    #   .../modules//aws/infrastructure/${tm}/${component}/
    m = _RE_MODULES_PREFIX.search(source)
    if m:
        provider = m.group(1)  # e.g. "aws"
        return f"modules/{provider}/infrastructure/{{terraform_module}}/{{component}}"

    # Pattern 2: relative static source like ../../../../modules/aws/resource/ec2
    # Strip TG interpolations, then try to find "modules/<...>" in what's left
    stripped = _RE_TG_INTERP.sub("", source).strip("/")
    # Find "modules/" in the stripped path and take everything after it
    idx = stripped.find("modules/")
    if idx >= 0:
        after = stripped[idx:]  # e.g. "modules/aws/resource/ec2"
        parts = after.strip("/").split("/")
        # If it's 4 parts and part[2] == "infrastructure", it's the dynamic pattern
        # If it's 4 parts like modules/aws/resource/ec2, it's a static resource module
        # We normalize it to the template format
        if len(parts) >= 2:
            # Reconstruct as template: keep literal segments, replace unknowns
            return "/".join(parts)  # Return as-is; caller can use it as-is

    return None


def _parse_root_file(text: str, include_target: str) -> dict:
    """Extract structured info from a root HCL file text."""
    result: dict = {
        "module_source_template": None,
        "env_config_filename": "config.yml",  # most common default
        "env_config_fields": [],
        "backend_type": "",
        "provider_name": "",
    }

    # Module source template from terraform { source = "..." }
    m = _RE_SOURCE.search(text)
    if m:
        tmpl = _parse_module_source_template(m.group(1))
        if tmpl:
            result["module_source_template"] = tmpl

    # Env config filename from find_in_parent_folders("config.yml")
    m = _RE_FIND_PARENT.search(text)
    if m:
        fname = m.group(1) or m.group(2)
        if fname and fname != include_target:
            result["env_config_filename"] = fname

    # Required env config fields from local.env_cfg.X references
    fields = list(dict.fromkeys(_RE_ENV_CFG_FIELD.findall(text)))  # ordered dedup
    if fields:
        result["env_config_fields"] = fields

    # Backend type from remote_state { backend = "..." }
    m = _RE_BACKEND.search(text)
    if m:
        result["backend_type"] = m.group(1)

    # Provider name from generate "provider" { ... provider "aws" { ... } }
    # We look for the provider block inside the generate block's contents
    m = _RE_PROVIDER_NAME.search(text)
    if m:
        result["provider_name"] = m.group(1)

    return result


# ---------------------------------------------------------------------------
# Main public API
# ---------------------------------------------------------------------------

def parse(repo_root: str) -> RepoSchema:
    """Parse the repo's root HCL file and return a RepoSchema.

    Always returns a valid RepoSchema — uses fallback defaults if discovery
    fails at any point. Never raises.
    """
    try:
        return _parse_uncached(repo_root)
    except Exception:
        return dataclasses.replace(_FALLBACK_SCHEMA)


def _parse_uncached(repo_root: str) -> RepoSchema:
    """Internal: may raise. Called by parse() which catches everything."""
    discovered = _discover_root_file(repo_root)

    if discovered is None:
        return dataclasses.replace(_FALLBACK_SCHEMA)

    root_path, include_target = discovered
    try:
        with open(root_path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return dataclasses.replace(_FALLBACK_SCHEMA)

    info = _parse_root_file(text, include_target)

    # Determine confidence level
    has_source = info["module_source_template"] is not None
    has_fields = bool(info["env_config_fields"])
    has_backend = bool(info["backend_type"])
    has_provider = bool(info["provider_name"])
    
    if has_source and has_fields:
        confidence = "full"
    elif has_source or has_fields or has_backend or has_provider:
        confidence = "partial"
    else:
        confidence = "fallback"

    # Fill in defaults where parsing produced nothing
    module_tmpl = (info["module_source_template"]
                   or _FALLBACK_SCHEMA.module_source_template)
    fields = info["env_config_fields"] or _FALLBACK_SCHEMA.env_config_fields
    backend = info["backend_type"] or _FALLBACK_SCHEMA.backend_type
    provider = info["provider_name"] or _FALLBACK_SCHEMA.provider_name

    return RepoSchema(
        root_file_path=root_path,
        root_file_name=os.path.basename(root_path),
        include_target=include_target,
        module_source_template=module_tmpl,
        env_config_filename=info["env_config_filename"],
        env_config_fields=fields,
        backend_type=backend,
        provider_name=provider,
        confidence=confidence,
        root_file_text=text,
    )


@lru_cache(maxsize=16)
def get_schema(repo_root: str) -> RepoSchema:
    """Return a cached RepoSchema for this repo_root. Safe to call repeatedly."""
    return parse(os.path.abspath(repo_root))


def invalidate_cache(repo_root: Optional[str] = None) -> None:
    """Invalidate the schema cache (call if root HCL changes on disk)."""
    get_schema.cache_clear()
