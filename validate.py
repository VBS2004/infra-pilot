"""validate.py - validation gate for composed Terragrunt components.

Faithfully mirrors the repo's Makefile pipeline (module.mk), AWS-only for the
MVP (onprem vsphere/xen out of scope per the manager).

What the repo actually does:
  - fmt check  : `cd <component>; terragrunt hcl fmt --check`         (offline)
  - validate   : `cd <component>; terragrunt validate`               (online: triggers
                  init -> providers via the artifactory net-mirror + AWS creds)
  - plan       : `cd <component>; terragrunt plan -out plan`          (online)
  - plan->json : `terragrunt show -json plan > plan.json`
  - checkov    : docker checkov_custom -ck plan -cf infra/checkov.yaml -f plan.json
                  -> the REAL input-level gate. checkov.yaml's `plan` profile is
                     `soft-fail: true` EXCEPT a `hard-fail-on` list that is mostly
                     tagging + naming custom policies.
  - tflint/tfsec: run on MODULES (infra/modules/**/*.tf) via artifactory docker
                  images with infra/tflint.hcl / infra/tfsec.yaml. Only relevant
                  for a NET-NEW module; reused modules are already green.

Tiers exposed by `gate(...)`:
  T0 offline : fmt check + light HCL sanity on the generated files.
  T1 module  : tflint + tfsec on a net-new module dir (optional).
  T2 online  : terragrunt validate -> plan -> checkov plan scan.

No third-party deps (stdlib only); uses python-hcl2 for the sanity parse only
if it happens to be importable, else a brace-balance heuristic.
"""
from __future__ import annotations

import argparse
import dataclasses
import os
import shutil
import subprocess
import sys
from typing import List, Optional

# Artifactory docker images, straight from module.mk.
TFLINT_IMAGE = os.environ.get(
    "TFLINT_IMAGE",
    "artifactory.acmecorp.com/optimus-docker/terraform-linters/tflint-bundle",
)
TFSEC_IMAGE = os.environ.get(
    "TFSEC_IMAGE",
    "artifactory.acmecorp.com/optimus-docker/aquasec/tfsec",
)
CHECKOV_IMAGE = os.environ.get(
    "CHECKOV_IMAGE",
    "artifactory.acmecorp.com/infra-common-docker/library/checkov_custom:latest",
)

TERRAGRUNT_BIN = os.environ.get("TERRAGRUNT_BIN_ENV", "terragrunt")
DOCKER_BIN = os.environ.get("DOCKER_BIN_ENV", "docker")
DEFAULT_TIMEOUT = int(os.environ.get("VALIDATE_TIMEOUT", "900"))


@dataclasses.dataclass
class StageResult:
    name: str
    ok: bool
    skipped: bool = False
    cmd: str = ""
    output: str = ""
    note: str = ""


@dataclasses.dataclass
class GateResult:
    ok: bool
    stages: List[StageResult]

    @property
    def feedback(self) -> str:
        """Compact, generator-friendly summary of what failed (for repair)."""
        lines = []
        for s in self.stages:
            tag = "SKIP" if s.skipped else ("OK" if s.ok else "FAIL")
            head = f"[{tag}] {s.name}" + (f" ({s.note})" if s.note else "")
            lines.append(head)
            if not s.ok and not s.skipped and s.output:
                lines.append(s.output.strip()[-4000:])
        return "\n".join(lines)


def _run(cmd: List[str], cwd: Optional[str] = None, timeout: int = DEFAULT_TIMEOUT):
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError as e:
        return 127, f"binary not found: {e}"
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout}s: {' '.join(cmd)}"


def _have(bin_name: str) -> bool:
    return shutil.which(bin_name) is not None


# --------------------------------------------------------------------------- #
# T0 - offline
# --------------------------------------------------------------------------- #
def fmt_check(component_dir: str, *, fix: bool = False) -> StageResult:
    cmd = [TERRAGRUNT_BIN, "hcl", "fmt"] + ([] if fix else ["--check"])
    if not _have(TERRAGRUNT_BIN):
        return StageResult("fmt", ok=False, skipped=True, cmd=" ".join(cmd),
                           note="terragrunt not on PATH")
    rc, out = _run(cmd, cwd=component_dir, timeout=120)
    return StageResult("fmt", ok=(rc == 0), cmd=" ".join(cmd), output=out)


def hcl_sanity(*paths: str) -> StageResult:
    """Offline HCL sanity: real parse via python-hcl2 if importable, else a
    balanced braces/brackets/parens heuristic."""
    try:
        import hcl2  # type: ignore
        have_hcl2 = True
    except Exception:
        have_hcl2 = False
    problems: List[str] = []
    for path in paths:
        if not os.path.exists(path):
            problems.append(f"missing: {path}")
            continue
        name = os.path.basename(path)
        if have_hcl2:
            try:
                with open(path, encoding="utf-8") as fh:
                    hcl2.load(fh)
            except Exception as e:  # noqa: BLE001 - report any parse failure
                problems.append(f"{name}: parse error: {e}")
        else:
            text = open(path, encoding="utf-8", errors="replace").read()
            for pair in ("{}", "[]", "()"):
                if text.count(pair[0]) != text.count(pair[1]):
                    problems.append(f"{name}: unbalanced '{pair[0]}' vs '{pair[1]}'")
    return StageResult("hcl_sanity", ok=(not problems), output="\n".join(problems),
                       note="hcl2" if have_hcl2 else "brace-balance heuristic")


# --------------------------------------------------------------------------- #
# T2 - online (needs AWS creds + provider mirror)
# --------------------------------------------------------------------------- #
def terragrunt_validate(component_dir: str) -> StageResult:
    cmd = [TERRAGRUNT_BIN, "validate"]
    rc, out = _run(cmd, cwd=component_dir)
    return StageResult("terragrunt validate", ok=(rc == 0), cmd=" ".join(cmd), output=out)


def terragrunt_plan(component_dir: str, out_file: str = "plan") -> StageResult:
    cmd = [TERRAGRUNT_BIN, "plan", "-out", out_file]
    rc, out = _run(cmd, cwd=component_dir)
    return StageResult("terragrunt plan", ok=(rc == 0), cmd=" ".join(cmd), output=out)


def plan_to_json(component_dir: str, plan_file: str = "plan",
                 json_file: str = "plan.json") -> StageResult:
    cmd = [TERRAGRUNT_BIN, "show", "-json", plan_file]
    rc, out = _run(cmd, cwd=component_dir)
    if rc != 0:
        return StageResult("plan->json", ok=False, cmd=" ".join(cmd), output=out)
    try:
        with open(os.path.join(component_dir, json_file), "w", encoding="utf-8") as fh:
            fh.write(out)
    except OSError as e:
        return StageResult("plan->json", ok=False, output=str(e))
    return StageResult("plan->json", ok=True, cmd=" ".join(cmd))


def checkov_plan(repo_root: str, component_dir: str, *, plan_json: str = "plan.json",
                 project_checkov: Optional[str] = None, use_docker: bool = True) -> StageResult:
    """checkov PLAN scan, like module.mk's checkov_scan(plan). The image already
    applies checkov.yaml, so its exit code encodes the hard-fail-on verdict."""
    infra_checkov = os.path.join(repo_root, "infra", "checkov.yaml")
    plan_path = os.path.join(component_dir, plan_json)
    if use_docker and _have(DOCKER_BIN):
        cmd = [DOCKER_BIN, "run", "--rm",
               "-v", f"{repo_root}/infra:/infra",
               "-v", f"{component_dir}:/work",
               CHECKOV_IMAGE,
               "/app/scripts/main.sh", "-ck", "plan", "-cf", "/infra/checkov.yaml"]
        if project_checkov:
            cmd += ["-cf", project_checkov]
        cmd += ["-f", f"/work/{plan_json}", "--", "--quiet"]
    elif _have("checkov"):
        cmd = ["checkov", "-f", plan_path, "--config-file", infra_checkov, "--compact", "--quiet"]
    else:
        return StageResult("checkov plan", ok=False, skipped=True,
                           note="neither docker nor local checkov available")
    rc, out = _run(cmd)
    return StageResult("checkov plan", ok=(rc == 0), cmd=" ".join(cmd), output=out)


# --------------------------------------------------------------------------- #
# T1 - module lint (only for net-new modules)
# --------------------------------------------------------------------------- #
def tflint_module(repo_root: str, module_dir: str, *, use_docker: bool = True) -> StageResult:
    if use_docker and _have(DOCKER_BIN):
        cmd = [DOCKER_BIN, "run", "--rm",
               "-v", f"{repo_root}/infra/tflint.hcl:/etc/tflint/tflint.hcl",
               "-v", f"{module_dir}:/data",
               TFLINT_IMAGE, "--config", "/etc/tflint/tflint.hcl"]
        rc, out = _run(cmd)
    elif _have("tflint"):
        cmd = ["tflint", "--config", os.path.join(repo_root, "infra", "tflint.hcl")]
        rc, out = _run(cmd, cwd=module_dir)
    else:
        return StageResult("tflint", ok=False, skipped=True, note="no docker/tflint")
    return StageResult("tflint", ok=(rc == 0), cmd=" ".join(cmd), output=out)


def tfsec_module(repo_root: str, module_dir: str, *, use_docker: bool = True) -> StageResult:
    if use_docker and _have(DOCKER_BIN):
        cmd = [DOCKER_BIN, "run", "--rm",
               "-v", f"{repo_root}/infra/tfsec.yaml:/etc/tfsec/tfsec.yaml",
               "-v", f"{module_dir}:/src",
               TFSEC_IMAGE, "--config-file", "/etc/tfsec/tfsec.yaml", "/src"]
    elif _have("tfsec"):
        cmd = ["tfsec", "--config-file", os.path.join(repo_root, "infra", "tfsec.yaml"), module_dir]
    else:
        return StageResult("tfsec", ok=False, skipped=True, note="no docker/tfsec")
    rc, out = _run(cmd)
    return StageResult("tfsec", ok=(rc == 0), cmd=" ".join(cmd), output=out)


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #
def gate(component_dir: str, *, repo_root: str, online: bool = False,
         module_dir: Optional[str] = None, use_docker: bool = True,
         fix_fmt: bool = False) -> GateResult:
    component_dir = os.path.abspath(component_dir)
    repo_root = os.path.abspath(repo_root)
    stages: List[StageResult] = []

    # T0 - offline, always.
    stages.append(fmt_check(component_dir, fix=fix_fmt))
    stages.append(hcl_sanity(
        os.path.join(component_dir, "inputs.hcl"),
        os.path.join(component_dir, "terragrunt.hcl"),
    ))

    # T1 - only when a net-new module is produced.
    if module_dir:
        stages.append(tflint_module(repo_root, os.path.abspath(module_dir), use_docker=use_docker))
        stages.append(tfsec_module(repo_root, os.path.abspath(module_dir), use_docker=use_docker))

    # T2 - online security/correctness gate (short-circuits on first failure).
    if online:
        v = terragrunt_validate(component_dir)
        stages.append(v)
        if v.ok:
            p = terragrunt_plan(component_dir)
            stages.append(p)
            if p.ok:
                j = plan_to_json(component_dir)
                stages.append(j)
                if j.ok:
                    stages.append(checkov_plan(repo_root, component_dir, use_docker=use_docker))
    else:
        stages.append(StageResult("online gate", ok=True, skipped=True,
                                  note="online=False: validate/plan/checkov skipped"))

    ok = all(s.ok for s in stages if not s.skipped)
    return GateResult(ok=ok, stages=stages)


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Validate a composed Terragrunt component.")
    ap.add_argument("component_dir")
    ap.add_argument("--repo-root", required=True)
    ap.add_argument("--online", action="store_true",
                    help="run validate/plan/checkov (needs AWS creds + provider mirror)")
    ap.add_argument("--module-dir", help="net-new module dir to tflint/tfsec")
    ap.add_argument("--no-docker", action="store_true", help="use local binaries instead of docker")
    ap.add_argument("--fix-fmt", action="store_true", help="run `hcl fmt` (write) instead of --check")
    a = ap.parse_args(argv)
    res = gate(a.component_dir, repo_root=a.repo_root, online=a.online,
               module_dir=a.module_dir, use_docker=not a.no_docker, fix_fmt=a.fix_fmt)
    print(res.feedback)
    print("\nGATE:", "PASS" if res.ok else "FAIL")
    return 0 if res.ok else 1


if __name__ == "__main__":
    sys.exit(_main())
