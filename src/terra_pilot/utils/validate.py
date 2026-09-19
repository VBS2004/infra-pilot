"""validate.py - validation gate for composed Terraform/Terragrunt components.

Tiers exposed by `gate(...)`:
  T0 offline : fmt check (terragrunt hcl fmt, or terraform fmt for plain
               Terraform) + HCL sanity on the generated files. No credentials.
  T1 module  : tflint + tfsec on a net-new module dir (optional).
  T2 online  : validate -> plan -> plan.json -> checkov plan scan. Needs cloud
               credentials and a provider mirror.

`hcl_text_problems` is the stdlib bracket/string checker `compose.write_to_tree`
runs before writing anything, so `--apply` always passes at least the offline
sanity tier without any external binary.

Every external tool is optional: a missing binary yields a SKIPPED stage (never
a silent pass or a crash). Docker images and binaries are overridable through
env vars; nothing here is specific to one organisation's registry or config.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import shutil
import subprocess
import sys
from typing import List, Optional

TFLINT_IMAGE = os.environ.get("TFLINT_IMAGE", "ghcr.io/terraform-linters/tflint:latest")
TFSEC_IMAGE = os.environ.get("TFSEC_IMAGE", "aquasec/tfsec:latest")
CHECKOV_IMAGE = os.environ.get("CHECKOV_IMAGE", "bridgecrew/checkov:latest")

TERRAGRUNT_BIN = os.environ.get("TERRAGRUNT_BIN_ENV", "terragrunt")
TERRAFORM_BIN = os.environ.get("TERRAFORM_BIN_ENV", "terraform")
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


def _run(cmd: List[str], cwd: Optional[str] = None, timeout: int = DEFAULT_TIMEOUT,
         stdout_only: bool = False):
    try:
        # stdin is closed so a tool can never block on an interactive prompt
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
        if stdout_only:
            return p.returncode, (p.stdout or "")
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
    """`terragrunt hcl fmt` for a Terragrunt component, `terraform fmt` otherwise."""
    if os.path.exists(os.path.join(component_dir, "terragrunt.hcl")):
        binary, cmd = TERRAGRUNT_BIN, [TERRAGRUNT_BIN, "hcl", "fmt"]
        cmd += [] if fix else ["--check"]
    else:
        binary, cmd = TERRAFORM_BIN, [TERRAFORM_BIN, "fmt"]
        cmd += [] if fix else ["-check"]
    if not _have(binary):
        return StageResult("fmt", ok=False, skipped=True, cmd=" ".join(cmd),
                           note=f"{binary} not on PATH")
    rc, out = _run(cmd, cwd=component_dir, timeout=120)
    return StageResult("fmt", ok=(rc == 0), cmd=" ".join(cmd), output=out)


def format_file(path: str) -> StageResult:
    """Canonically format ONE file (never the whole directory, so files we did not
    write are left alone): `terragrunt hcl fmt --file` next to a terragrunt.hcl,
    else `terraform fmt <file>`."""
    d, name = os.path.split(os.path.abspath(path))
    if os.path.exists(os.path.join(d, "terragrunt.hcl")):
        binary, cmd = TERRAGRUNT_BIN, [TERRAGRUNT_BIN, "hcl", "fmt", "--file", name]
    else:
        binary, cmd = TERRAFORM_BIN, [TERRAFORM_BIN, "fmt", name]
    if not _have(binary):
        return StageResult("format", ok=False, skipped=True, cmd=" ".join(cmd),
                           note=f"{binary} not on PATH")
    rc, out = _run(cmd, cwd=d, timeout=120)
    return StageResult("format", ok=(rc == 0), cmd=" ".join(cmd), output=out)


def hcl_text_problems(text: str) -> List[str]:
    """Bracket/quote check for HCL text: skips comments, heredocs and string
    contents (but follows `${...}` interpolation). Returns a list of problems,
    empty when the structure is sound."""
    import re
    problems: List[str] = []
    stack: list = []          # ("str", line) | ("br", char, line)
    pairs = {"{": "}", "[": "]", "(": ")"}
    i, n, line = 0, len(text), 1
    while i < n:
        c = text[i]
        top = stack[-1] if stack else None
        if top and top[0] == "str":
            if c == "\\":
                i += 2
                continue
            if c == "\n":
                line += 1
            if c == '"':
                stack.pop()
            elif text.startswith(("${", "%{"), i):
                stack.append(("br", "{", line))
                i += 1
            i += 1
            continue
        if c == "\n":
            line += 1
        elif c == "#" or text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            if j < 0:
                problems.append(f"line {line}: unterminated /* comment")
                return problems
            line += text.count("\n", i, j)
            i = j + 2
            continue
        elif text.startswith("<<", i):
            m = re.match(r"<<-?([A-Za-z_]\w*)[ \t]*\r?\n", text[i:])
            if m:
                marker = m.group(1)
                end = re.search(r"^[ \t]*" + re.escape(marker) + r"[ \t]*\r?$",
                                text[i + m.end():], re.M)
                if not end:
                    problems.append(f"line {line}: unterminated heredoc <<{marker}")
                    return problems
                stop = i + m.end() + end.end()
                line += text.count("\n", i, stop)
                i = stop
                continue
        elif c == '"':
            stack.append(("str", line))
        elif c in pairs:
            stack.append(("br", c, line))
        elif c in "}])":
            if not stack or stack[-1][0] != "br" or pairs[stack[-1][1]] != c:
                problems.append(f"line {line}: unexpected '{c}'")
                return problems
            stack.pop()
        i += 1
    for item in stack:
        if item[0] == "str":
            problems.append(f"line {item[1]}: unterminated string")
        else:
            problems.append(f"line {item[2]}: unclosed '{item[1]}'")
    return problems


def hcl_sanity(*paths: str) -> StageResult:
    """Offline HCL sanity: real parse via python-hcl2 if importable, else the
    stdlib bracket/string checker."""
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
            problems += [f"{name}: {p}" for p in hcl_text_problems(text)]
    return StageResult("hcl_sanity", ok=(not problems), output="\n".join(problems),
                       note="hcl2" if have_hcl2 else "stdlib bracket/string check")


# --------------------------------------------------------------------------- #
# T2 - online (needs AWS creds + provider mirror)
# --------------------------------------------------------------------------- #
def terragrunt_validate(component_dir: str) -> StageResult:
    cmd = [TERRAGRUNT_BIN, "validate"]
    rc, out = _run(cmd, cwd=component_dir)
    return StageResult("terragrunt validate", ok=(rc == 0), cmd=" ".join(cmd), output=out)


def terragrunt_plan(component_dir: str, out_file: str = "plan") -> StageResult:
    cmd = [TERRAGRUNT_BIN, "plan", "-input=false", f"-out={out_file}"]
    rc, out = _run(cmd, cwd=component_dir)
    return StageResult("terragrunt plan", ok=(rc == 0), cmd=" ".join(cmd), output=out)


def plan_to_json(component_dir: str, plan_file: str = "plan",
                 json_file: str = "plan.json") -> StageResult:
    cmd = [TERRAGRUNT_BIN, "show", "-json", plan_file]
    # stdout only: terragrunt's own log lines go to stderr and would corrupt the JSON
    rc, out = _run(cmd, cwd=component_dir, stdout_only=True)
    if rc != 0:
        return StageResult("plan->json", ok=False, cmd=" ".join(cmd), output=out)
    try:
        json.loads(out)
    except ValueError as e:
        return StageResult("plan->json", ok=False, cmd=" ".join(cmd),
                           output=f"`show -json` did not produce valid JSON: {e}\n{out[:300]}")
    try:
        with open(os.path.join(component_dir, json_file), "w", encoding="utf-8") as fh:
            fh.write(out)
    except OSError as e:
        return StageResult("plan->json", ok=False, output=str(e))
    return StageResult("plan->json", ok=True, cmd=" ".join(cmd))


def checkov_plan(repo_root: str, component_dir: str, *, plan_json: str = "plan.json",
                 project_checkov: Optional[str] = None, use_docker: bool = True) -> StageResult:
    """checkov scan of the plan JSON. Uses `<repo>/checkov.yaml` (or
    `<repo>/infra/checkov.yaml`) when present; the exit code is the verdict."""
    cfg = next((p for p in (os.path.join(repo_root, "checkov.yaml"),
                            os.path.join(repo_root, "infra", "checkov.yaml"))
                if os.path.exists(p)), None)
    plan_path = os.path.join(component_dir, plan_json)
    if use_docker and _have(DOCKER_BIN):
        cmd = [DOCKER_BIN, "run", "--rm", "-v", f"{component_dir}:/work"]
        if cfg:
            cmd += ["-v", f"{cfg}:/etc/checkov.yaml"]
        cmd += [CHECKOV_IMAGE, "-f", f"/work/{plan_json}", "--compact", "--quiet"]
        if cfg:
            cmd += ["--config-file", "/etc/checkov.yaml"]
    elif _have("checkov"):
        cmd = ["checkov", "-f", plan_path, "--compact", "--quiet"]
        if cfg:
            cmd += ["--config-file", cfg]
    else:
        return StageResult("checkov plan", ok=False, skipped=True,
                           note="neither docker nor local checkov available")
    if project_checkov:
        cmd += ["--config-file", project_checkov]
    rc, out = _run(cmd)
    return StageResult("checkov plan", ok=(rc == 0), cmd=" ".join(cmd), output=out)


# --------------------------------------------------------------------------- #
# T1 - module lint (only for net-new modules)
# --------------------------------------------------------------------------- #
def _config_file(repo_root: str, name: str) -> Optional[str]:
    return next((p for p in (os.path.join(repo_root, name),
                             os.path.join(repo_root, "infra", name))
                 if os.path.exists(p)), None)


def tflint_module(repo_root: str, module_dir: str, *, use_docker: bool = True) -> StageResult:
    cfg = _config_file(repo_root, "tflint.hcl")
    if use_docker and _have(DOCKER_BIN):
        cmd = [DOCKER_BIN, "run", "--rm", "-v", f"{module_dir}:/data"]
        if cfg:
            cmd += ["-v", f"{cfg}:/etc/tflint/tflint.hcl"]
        cmd += [TFLINT_IMAGE] + (["--config", "/etc/tflint/tflint.hcl"] if cfg else [])
        rc, out = _run(cmd)
    elif _have("tflint"):
        cmd = ["tflint"] + (["--config", cfg] if cfg else [])
        rc, out = _run(cmd, cwd=module_dir)
    else:
        return StageResult("tflint", ok=False, skipped=True, note="no docker/tflint")
    return StageResult("tflint", ok=(rc == 0), cmd=" ".join(cmd), output=out)


def tfsec_module(repo_root: str, module_dir: str, *, use_docker: bool = True) -> StageResult:
    cfg = _config_file(repo_root, "tfsec.yaml")
    if use_docker and _have(DOCKER_BIN):
        cmd = [DOCKER_BIN, "run", "--rm", "-v", f"{module_dir}:/src"]
        if cfg:
            cmd += ["-v", f"{cfg}:/etc/tfsec/tfsec.yaml"]
        cmd += [TFSEC_IMAGE] + (["--config-file", "/etc/tfsec/tfsec.yaml"] if cfg else []) + ["/src"]
    elif _have("tfsec"):
        cmd = ["tfsec"] + (["--config-file", cfg] if cfg else []) + [module_dir]
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
    ap = argparse.ArgumentParser(description="Validate a composed Terraform/Terragrunt component.")
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
