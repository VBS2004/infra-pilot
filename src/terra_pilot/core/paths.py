"""paths.py - deterministic repo path resolution for the compose loop.

Pure + offline: encodes the path math that root.hcl performs at runtime so
the composer can resolve (project, env, component) -> component dir + module
source dir WITHOUT running terragrunt.

Actual repo layout:
    <project>/aws/<project>_<env>/<component>/{terragrunt.hcl, inputs.hcl}
    <project>/aws/<project>_<env>/config.yml      # terraform_module, account_id, region
    <project>/aws/common.hcl                      # shared project-level inputs
    modules/aws/infrastructure/<tf_module>/<component>/  # reusable modules
      tf_module examples: env, bastion, centraltools, common
      component examples (under env/): aerospike, airflow, athena, auroramysql, ec2, ...
    root.hcl                                      # provider/backend/source generation
"""
from __future__ import annotations

import dataclasses
import os
from typing import Dict, List, Optional, Tuple
from terra_pilot.core.convention import detect_convention, RepoConvention

DEFAULT_PROVIDER = "aws"


@dataclasses.dataclass
class ResolvedComponent:
    project: str
    env: str
    component: str
    provider: str
    terraform_module: Optional[str]
    component_dir: str
    module_source_dir: str
    variables_tf: str
    terragrunt_hcl: str
    inputs_hcl: str
    root_hcl: str
    common_hcl: str
    env_config: str

    def as_dict(self) -> Dict[str, object]:
        return dataclasses.asdict(self)


def infra_root(repo_root: str) -> str:
    # No 'infra/' prefix — projects and modules live directly under repo_root.
    return repo_root


def root_hcl_path(repo_root: str) -> str:
    from terra_pilot.models import root_schema
    schema = root_schema.get_schema(repo_root)
    # Return the exact discovered root file path, or fallback to infra_root/root.hcl
    if schema.root_file_path:
        return schema.root_file_path
    return os.path.join(infra_root(repo_root), "root.hcl")


def project_dir(repo_root: str, project: str, provider: str = DEFAULT_PROVIDER) -> str:
    return os.path.join(infra_root(repo_root), project, provider)


def common_hcl_path(repo_root: str, project: str, provider: str = DEFAULT_PROVIDER) -> str:
    return os.path.join(project_dir(repo_root, project, provider), "common.hcl")


def env_dir(repo_root: str, project: str, env: str, provider: str = DEFAULT_PROVIDER) -> str:
    """<repo_root>/<project>/<provider>/<project>_<env>/

    `env` is normally the short word ('nonprod'), but callers also hold the
    already-resolved env-tier directory name ('auth_nonprod', from
    match_env_dir). A name that is an existing directory, or that already carries
    the project prefix, is used as-is instead of being prefixed a second time
    (which produced 'auth/aws/auth_auth_nonprod/')."""
    base = project_dir(repo_root, project, provider)
    if os.path.isdir(os.path.join(base, env)) or env.startswith(f"{project}_"):
        return os.path.join(base, env)
    return os.path.join(base, f"{project}_{env}")


def env_config_path(repo_root: str, project: str, env: str, provider: str = DEFAULT_PROVIDER) -> str:
    return os.path.join(env_dir(repo_root, project, env, provider), "config.yml")


def component_dir(repo_root: str, project: str, env: str, component: str,
                  provider: str = DEFAULT_PROVIDER) -> str:
    return os.path.join(env_dir(repo_root, project, env, provider), component)


def module_source_dir(repo_root: str, terraform_module: str, component: str,
                      provider: str = DEFAULT_PROVIDER) -> str:
    """Resolved via the extracted repo schema (fallback: <repo_root>/modules/<provider>/infrastructure/<terraform_module>/<component>/)"""
    from terra_pilot.models import root_schema
    schema = root_schema.get_schema(repo_root)
    return schema.module_source_dir(repo_root, provider, terraform_module, component)


def resource_module_dir(repo_root: str, leaf: str, provider: str = DEFAULT_PROVIDER) -> str:
    return os.path.join(repo_root, "modules", provider, "resource", leaf)


def _parse_flat_yaml(text: str) -> Dict[str, str]:
    """Minimal parser for the flat `key: value` env config.yml. Handles quotes,
    inline `#` comments, and booleans/ints as strings. Prefers PyYAML if present."""
    try:
        import yaml  # type: ignore
        loaded = yaml.safe_load(text) or {}
        if isinstance(loaded, dict):
            return {str(k): v for k, v in loaded.items()}
    except Exception:
        pass
    out: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        # strip an inline comment that is not inside quotes
        if val[:1] not in ("'", '"') and "#" in val:
            val = val.split("#", 1)[0].strip()
        if len(val) >= 2 and val[0] in ("'", '"') and val[-1] == val[0]:
            val = val[1:-1]
        out[key] = val
    return out


def load_env_config(repo_root: str, project: str, env: str,
                    provider: str = DEFAULT_PROVIDER) -> Dict[str, str]:
    path = env_config_path(repo_root, project, env, provider)
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        return _parse_flat_yaml(fh.read())


# Directory names plain-Terraform repos use to hold per-environment roots.
_ENV_ROOTS = ("environments", "envs", "env", "live", "stacks", "deployments")


def _has_tf(d: str) -> bool:
    try:
        return any(f.endswith(".tf") for f in os.listdir(d))
    except OSError:
        return False


def _find_plain_env_dir(repo_root: str, project: str, env: str) -> Optional[str]:
    bases = list(_ENV_ROOTS) + [""]
    if project:
        bases += [project] + [os.path.join(project, r) for r in _ENV_ROOTS]
    for base in bases:
        cand = os.path.join(repo_root, base, env)
        if os.path.isdir(cand):
            return cand
    return None


def plain_tf_target(repo_root: str, kind: str, project: str, env: str, component: str):
    """Where a plain-Terraform component belongs -> (component_dir, file_path).

    Follows the repo's own evidence instead of a fixed layout:
      * `<env>/<component>/` already exists            -> its main.tf
      * env dir holds .tf files and no component dirs  -> `<env>/<component>.tf`
        (an `envs/dev/main.tf`-style root; a new file never clobbers main.tf)
      * env dir holds component sub-dirs               -> `<env>/<component>/main.tf`
      * no such env dir, flat repo                     -> `<repo>/<component>.tf`
      * no such env dir, module repo                   -> `<env>/<component>/main.tf`
    """
    env_dir = _find_plain_env_dir(repo_root, project, env)
    if env_dir is None:
        if kind == "tf-flat":
            return repo_root, os.path.join(repo_root, f"{component}.tf")
        cdir = os.path.join(repo_root, env, component)
        return cdir, os.path.join(cdir, "main.tf")
    cdir = os.path.join(env_dir, component)
    if os.path.isdir(cdir):
        return cdir, os.path.join(cdir, "main.tf")
    subdirs_with_tf = [d for d in os.listdir(env_dir)
                       if os.path.isdir(os.path.join(env_dir, d))
                       and _has_tf(os.path.join(env_dir, d))]
    if _has_tf(env_dir) and not subdirs_with_tf:
        return env_dir, os.path.join(env_dir, f"{component}.tf")
    return cdir, os.path.join(cdir, "main.tf")


def resolve_plain_tf(repo_root: str, convention: RepoConvention, project: str, env: str, component: str, *,
                     provider: str = DEFAULT_PROVIDER,
                     terraform_module: Optional[str] = None) -> ResolvedComponent:
    repo_root = os.path.abspath(repo_root)
    cdir, target = plain_tf_target(repo_root, convention.kind, project, env, component)

    msrc = ""
    if terraform_module:
        if os.path.isdir(os.path.join(repo_root, "modules", terraform_module)):
             msrc = os.path.join(repo_root, "modules", terraform_module)
        elif os.path.isdir(os.path.join(repo_root, "modules", provider, "infrastructure", terraform_module, component)):
             msrc = os.path.join(repo_root, "modules", provider, "infrastructure", terraform_module, component)
             
    return ResolvedComponent(
        project=project, env=env, component=component, provider=provider,
        terraform_module=terraform_module,
        component_dir=cdir,
        module_source_dir=msrc,
        variables_tf=os.path.join(msrc, "variables.tf") if msrc else "",
        terragrunt_hcl="",
        inputs_hcl=target,
        root_hcl="",
        common_hcl="",
        env_config="",
    )

def resolve_generic(repo_root: str, project: str, env: str, component: str, *,
                    provider: str = DEFAULT_PROVIDER,
                    terraform_module: Optional[str] = None) -> ResolvedComponent:
    convention = detect_convention(repo_root)
    if convention.kind == "terragrunt":
        return resolve(repo_root, project, env, component, provider=provider, terraform_module=terraform_module)
    else:
        return resolve_plain_tf(repo_root, convention, project, env, component, provider=provider, terraform_module=terraform_module)

def resolve(repo_root: str, project: str, env: str, component: str, *,
            provider: str = DEFAULT_PROVIDER,
            terraform_module: Optional[str] = None) -> ResolvedComponent:
    """Resolve all paths for a component. `terraform_module` is read from the env
    config.yml unless explicitly overridden; defaults to 'env' if neither is set."""
    repo_root = os.path.abspath(repo_root)
    if terraform_module is None:
        cfg = load_env_config(repo_root, project, env, provider)
        terraform_module = cfg.get("terraform_module") or None
    effective_tm = terraform_module or "env"
    cdir = component_dir(repo_root, project, env, component, provider)
    msrc = module_source_dir(repo_root, effective_tm, component, provider)
    return ResolvedComponent(
        project=project, env=env, component=component, provider=provider,
        terraform_module=terraform_module,
        component_dir=cdir,
        module_source_dir=msrc,
        variables_tf=os.path.join(msrc, "variables.tf"),
        terragrunt_hcl=os.path.join(cdir, "terragrunt.hcl"),
        inputs_hcl=os.path.join(cdir, "inputs.hcl"),
        root_hcl=root_hcl_path(repo_root),
        common_hcl=common_hcl_path(repo_root, project, provider),
        env_config=env_config_path(repo_root, project, env, provider),
    )


def module_exists(repo_root: str, terraform_module: str, component: str,
                  provider: str = DEFAULT_PROVIDER) -> bool:
    msrc = module_source_dir(repo_root, terraform_module, component, provider)
    return os.path.isdir(msrc) and (
        os.path.exists(os.path.join(msrc, "main.tf"))
        or os.path.exists(os.path.join(msrc, "variables.tf"))
    )


def _list_dirs(path: str) -> List[str]:
    if not os.path.isdir(path):
        return []
    return sorted(d for d in os.listdir(path)
                  if os.path.isdir(os.path.join(path, d)) and not d.startswith("."))


def list_infrastructure_modules(repo_root: str, terraform_module: str,
                                provider: str = DEFAULT_PROVIDER) -> List[str]:
    from terra_pilot.models import root_schema
    schema = root_schema.get_schema(repo_root)
    # the parent dir of the component inside the template
    # e.g. from "modules/{provider}/infrastructure/{terraform_module}/{component}"
    # we want "modules/{provider}/infrastructure/{terraform_module}"
    # we can just use the schema to get the path for a dummy component, then dirname
    dummy = schema.module_source_dir(repo_root, provider, terraform_module, "DUMMY")
    parent = os.path.dirname(dummy)
    return _list_dirs(parent)


def list_resource_modules(repo_root: str, provider: str = DEFAULT_PROVIDER) -> List[str]:
    return _list_dirs(os.path.join(infra_root(repo_root), "modules", provider, "resource"))


def list_all_components(repo_root: str, provider: str = DEFAULT_PROVIDER) -> List[str]:
    """Union of component dir names across every infrastructure/<tf_module>/ dir --
    the repo's ground-truth resource_type vocabulary (used for fan-out detection)."""
    from terra_pilot.models import root_schema
    schema = root_schema.get_schema(repo_root)
    # Hack: infer the base 'infrastructure' dir from the template
    dummy = schema.module_source_dir(repo_root, provider, "DUMMY_TM", "DUMMY_COMP")
    # dirname = DUMMY_TM dir; dirname(dirname) = infrastructure dir
    base = os.path.dirname(os.path.dirname(dummy))
    out = set()
    for tm in _list_dirs(base):
        for comp in _list_dirs(os.path.join(base, tm)):
            out.add(comp)
    return sorted(out)


def list_projects(repo_root: str, provider: str = DEFAULT_PROVIDER) -> List[str]:
    """Project slugs that have a <provider> dir under infra/ (excludes 'modules')."""
    base = infra_root(repo_root)
    out = []
    for p in _list_dirs(base):
        if p == "modules":
            continue
        if os.path.isdir(project_dir(repo_root, p, provider)):
            out.append(p)
    return out


def list_envs(repo_root: str, project: str, provider: str = DEFAULT_PROVIDER) -> List[str]:
    """Return env dir names under <project>/<provider>/. These are named
    <project>_<env> (e.g. 'auth_nonprod'), so the returned names include the
    project prefix. Use match_env_dir() to resolve a short env name like
    'nonprod' to the full dir name."""
    return _list_dirs(project_dir(repo_root, project, provider))


def _norm_env(s: object) -> str:
    """Normalize an env word for fuzzy matching: casefold + strip '-' and '_'.
    So 'pre-prod', 'pre_prod', 'PreProd' all collapse to 'preprod'."""
    return str(s or "").strip().lower().replace("-", "").replace("_", "")


def match_env_dir(repo_root: str, project: str, stated_env: str, *,
                  provider: str = DEFAULT_PROVIDER) -> Tuple[Optional[str], List[str]]:
    """Resolve a stated env word (e.g. 'nonprod') to a real env-tier directory name
    (e.g. 'auth_nonprod').

    Returns (chosen_dir_or_None, candidate_dirs). `chosen` is set only when the
    match is unambiguous. All comparisons are normalized (casefold + strip '-'/'_').

    Match order:
      1. exact directory name (raw)
      2. normalized exact match on full dir name OR on the part AFTER the project prefix
         so 'nonprod' matches 'auth_nonprod'
      3. the dir's config.yml `environment` field (normalized)
      4. containment fallback, e.g. 'preprod' contained in 'auth_preprod'
    """
    from terra_pilot.models import root_schema
    schema = root_schema.get_schema(repo_root)
    envs = list_envs(repo_root, project, provider=provider)
    if stated_env in envs:
        return stated_env, [stated_env]
    target = _norm_env(stated_env)
    if not target or not envs:
        return None, []

    # Normalize: also try matching the suffix after the project_ prefix.
    def _env_suffix(dir_name: str) -> str:
        prefix = _norm_env(project) + "_"
        n = _norm_env(dir_name)
        return n[len(prefix):] if n.startswith(prefix) else n

    exact = [e for e in envs if _norm_env(e) == target or _env_suffix(e) == target]
    if exact:
        return (exact[0] if len(exact) == 1 else None), exact

    by_cfg = []
    for e in envs:
        # load_env_config needs the raw dir name, but env_dir will append project_env
        # so we pass the full dir name as the 'env' and let env_dir handle it.
        # However env_dir does project_env internally, so we must read directly.
        cfg_path = os.path.join(project_dir(repo_root, project, provider), e, schema.env_config_filename)
        if os.path.exists(cfg_path):
            with open(cfg_path, encoding="utf-8", errors="replace") as fh:
                cfg = _parse_flat_yaml(fh.read())
            if _norm_env(cfg.get("environment", "")) == target:
                by_cfg.append(e)
    if by_cfg:
        return (by_cfg[0] if len(by_cfg) == 1 else None), by_cfg

    contained = [e for e in envs if target and target in _norm_env(e)]
    if contained:
        return (contained[0] if len(contained) == 1 else None), contained
    return None, []


# The standard component terragrunt.hcl
def standard_terragrunt_hcl(repo_root: str) -> str:
    from terra_pilot.models import root_schema
    return root_schema.get_schema(repo_root).standard_terragrunt_hcl()


if __name__ == "__main__":
    import json
    import sys
    if len(sys.argv) < 5:
        print("usage: python3 paths.py <repo_root> <project> <env> <component> [terraform_module]")
        sys.exit(2)
    repo, proj, env, comp = sys.argv[1:5]
    tm = sys.argv[5] if len(sys.argv) > 5 else None
    rc = resolve(repo, proj, env, comp, terraform_module=tm)
    print(json.dumps(rc.as_dict(), indent=2))
