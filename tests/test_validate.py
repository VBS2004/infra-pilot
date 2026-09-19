"""utils/validate.py: the fmt -> sanity -> (module lint) -> validate/plan/checkov gate.

No real terragrunt/terraform/checkov is needed: tiny shell scripts on PATH stand
in for them, log their argv, and exit with a code the test controls. That
exercises the real subprocess plumbing, stage ordering and short-circuiting.
"""
import os
import shutil
import stat
import tempfile

import harness
from harness import check, copy_fixture, captured, finish, point_at
from fake_gateway import FakeGateway

BIN = tempfile.mkdtemp(prefix="tp_bin_")
LOG = os.path.join(BIN, "calls.log")
SCRIPT = """#!/bin/sh
echo "$(basename "$0") $*" >> "%s"
case "$(basename "$0")" in
  terragrunt|terraform)
    case "$*" in
      *--check*|*-check*) [ -f "%s.formatted" ] && exit 0; exit ${FAKE_FMT_RC:-0} ;;
      *fmt*) : > "%s.formatted"; exit ${FAKE_FMT_FIX_RC:-0} ;;
      *validate*) echo "validate output"; exit ${FAKE_VALIDATE_RC:-0} ;;
      *show*) echo "INFO log line" >&2; echo '{"planned_values": {}}'; exit ${FAKE_SHOW_RC:-0} ;;
      *plan*) exit ${FAKE_PLAN_RC:-0} ;;
    esac ;;
  checkov) echo "checkov output"; exit ${FAKE_CHECKOV_RC:-0} ;;
  tflint) exit ${FAKE_TFLINT_RC:-0} ;;
  tfsec) exit ${FAKE_TFSEC_RC:-0} ;;
  docker) exit ${FAKE_DOCKER_RC:-0} ;;
esac
exit 0
""" % (LOG, LOG, LOG)


def install(*names):
    for n in names:
        p = os.path.join(BIN, n)
        with open(p, "w") as f:
            f.write(SCRIPT)
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)


def calls():
    return open(LOG).read().splitlines() if os.path.exists(LOG) else []


def reset():
    for f in (LOG, LOG + ".formatted"):
        if os.path.exists(f):
            os.remove(f)
    for k in list(os.environ):
        if k.startswith("FAKE_"):
            del os.environ[k]


REAL_PATH = os.environ["PATH"]


def with_tools(*names):
    """PATH containing only the named fake tools (plus system utils like sh)."""
    for n in os.listdir(BIN):
        if n != "calls.log":
            os.remove(os.path.join(BIN, n))
    install(*names)
    # Only the fakes plus the two utilities they need: a real terraform/docker on
    # the host (CI runners ship them) must not leak into "tool is missing" cases.
    for util in ("sh", "basename"):
        real = shutil.which(util, path=REAL_PATH)
        if real and not os.path.exists(os.path.join(BIN, util)):
            os.symlink(real, os.path.join(BIN, util))
    os.environ["PATH"] = BIN


def component(broken=False):
    d = tempfile.mkdtemp(prefix="tp_comp_")
    with open(os.path.join(d, "terragrunt.hcl"), "w") as f:
        f.write('include { path = find_in_parent_folders("root.hcl") }\n')
    with open(os.path.join(d, "inputs.hcl"), "w") as f:
        f.write('inputs = {\n  a = "x"\n' if broken else 'inputs = {\n  a = "x"\n}\n')
    return d


from terra_pilot.utils import validate as v


def test_hcl_text_problems():
    print("\n# hcl_text_problems (stdlib checker)")
    ok = ('inputs = {\n  a = "x ${lookup(m, "k")} {"\n  # comment { unbalanced\n  /* also ( */\n'
          '  l = [1, (2)]\n  h = <<-EOF\n    { not code\n  EOF\n}\n')
    check("balanced, with braces inside strings/comments/heredocs", v.hcl_text_problems(ok) == [])
    check("unclosed brace", "unclosed '{'" in v.hcl_text_problems('inputs = {\n a = 1\n')[0])
    check("stray closer", "unexpected ']'" in v.hcl_text_problems('a = ]\n')[0])
    check("mismatched closer", "unexpected ')'" in v.hcl_text_problems('a = [1)\n')[0])
    check("unterminated string", "unterminated string" in v.hcl_text_problems('a = "abc\n')[0])
    check("unterminated block comment", "unterminated /*" in v.hcl_text_problems('/* x\n a = 1\n')[0])
    check("unterminated heredoc", "unterminated heredoc" in v.hcl_text_problems('a = <<EOF\n x\n')[0])
    check("escaped quote inside string", v.hcl_text_problems('a = "say \\"hi\\" {"\n') == [])
    check("empty file is fine", v.hcl_text_problems("") == [])


def test_hcl_sanity_stage():
    print("\n# hcl_sanity stage")
    good, bad = component(), component(broken=True)
    r = v.hcl_sanity(os.path.join(good, "inputs.hcl"), os.path.join(good, "terragrunt.hcl"))
    check("valid files pass", r.ok and not r.output)
    r = v.hcl_sanity(os.path.join(bad, "inputs.hcl"))
    check("broken file fails with the file name and line", (not r.ok) and "inputs.hcl" in r.output and "line" in r.output)
    r = v.hcl_sanity(os.path.join(good, "missing.hcl"))
    check("missing file reported", (not r.ok) and "missing" in r.output)


def test_fmt_stage():
    print("\n# fmt stage picks the right formatter")
    reset(); with_tools("terragrunt", "terraform")
    tg = component()
    r = v.fmt_check(tg)
    check("terragrunt component -> terragrunt hcl fmt --check", "terragrunt hcl fmt --check" in " ".join(calls()) and r.ok, str(calls()))
    reset()
    plain = tempfile.mkdtemp(prefix="tp_plain_")
    r = v.fmt_check(plain)
    check("plain terraform dir -> terraform fmt -check", calls() == ["terraform fmt -check"] and r.ok, str(calls()))
    os.environ["FAKE_FMT_RC"] = "3"
    r = v.fmt_check(tg)
    check("non-zero exit -> failed stage", (not r.ok) and not r.skipped)
    reset()
    r = v.fmt_check(tg, fix=True)
    check("fix=True runs without --check", calls() == ["terragrunt hcl fmt"], str(calls()))
    with_tools()
    r = v.fmt_check(tg)
    check("no binary -> SKIPPED, not silently passed", r.skipped and "not on PATH" in r.note)


def test_gate_offline():
    print("\n# gate: offline tier")
    reset(); with_tools("terragrunt")
    comp = component()
    g = v.gate(comp, repo_root=comp)
    stages = {s.name: s for s in g.stages}
    check("offline gate passes", g.ok, g.feedback)
    check("stages: fmt, hcl_sanity, online skipped",
          list(stages) == ["fmt", "hcl_sanity", "online gate"] and stages["online gate"].skipped)
    check("nothing online was invoked", not any("validate" in c or "plan" in c for c in calls()))
    os.environ["FAKE_FMT_RC"] = "1"
    g = v.gate(comp, repo_root=comp)
    check("fmt failure fails the gate and shows in feedback", (not g.ok) and "[FAIL] fmt" in g.feedback)
    reset()
    g = v.gate(component(broken=True), repo_root=comp)
    check("HCL sanity failure fails the gate", (not g.ok) and "[FAIL] hcl_sanity" in g.feedback)
    with_tools()
    g = v.gate(comp, repo_root=comp)
    check("missing terragrunt is a skip, gate still decided by the rest", g.ok and "[SKIP] fmt" in g.feedback)


def test_gate_online_chain():
    print("\n# gate: online tier ordering + short-circuit")
    reset(); with_tools("terragrunt", "checkov")
    comp = component()
    g = v.gate(comp, repo_root=comp, online=True, use_docker=False)
    names = [s.name for s in g.stages]
    check("full chain ran in order",
          names == ["fmt", "hcl_sanity", "terragrunt validate", "terragrunt plan", "plan->json", "checkov plan"], str(names))
    check("gate passes when every stage passes", g.ok, g.feedback)
    check("plan.json written from `terragrunt show -json`", os.path.exists(os.path.join(comp, "plan.json")))
    import json as _json
    try:
        _json.loads(open(os.path.join(comp, "plan.json")).read())
        check("plan.json is valid JSON (stderr log lines are not mixed in)", True)
    except ValueError:
        check("plan.json is valid JSON (stderr log lines are not mixed in)", False)
    check("terragrunt commands issued", any(c == "terragrunt plan -input=false -out=plan" for c in calls())
          and any(c == "terragrunt show -json plan" for c in calls()))

    reset()
    os.environ["FAKE_VALIDATE_RC"] = "1"
    g = v.gate(comp, repo_root=comp, online=True, use_docker=False)
    check("validate failure short-circuits (no plan/show/checkov)",
          (not g.ok) and not any("plan" in c or "show" in c or c.startswith("checkov") for c in calls()), str(calls()))
    check("failing stage output is in the repair feedback", "validate output" in g.feedback)

    reset()
    os.environ["FAKE_CHECKOV_RC"] = "1"
    g = v.gate(comp, repo_root=comp, online=True, use_docker=False)
    check("checkov failure fails the gate", (not g.ok) and "[FAIL] checkov plan" in g.feedback)

    reset(); with_tools("terragrunt")
    g = v.gate(comp, repo_root=comp, online=True, use_docker=False)
    stage = next(s for s in g.stages if s.name == "checkov plan")
    check("no checkov/docker -> skipped, not failed", stage.skipped and g.ok)


def test_module_lint_tier_and_docker_shape():
    print("\n# T1 module lint + docker command shape (no org-specific registry)")
    reset(); with_tools("terragrunt", "tflint", "tfsec")
    repo, mod = tempfile.mkdtemp(prefix="tp_r_"), tempfile.mkdtemp(prefix="tp_m_")
    comp = component()
    g = v.gate(comp, repo_root=repo, module_dir=mod, use_docker=False)
    check("tflint + tfsec ran on the module dir", g.ok and "tflint" in [c.split()[0] for c in calls()]
          and any(c.startswith("tfsec") and mod in c for c in calls()), str(calls()))
    check("no config flag when the repo has no tflint.hcl/tfsec.yaml",
          not any("--config" in c for c in calls()), str(calls()))
    with open(os.path.join(repo, "tflint.hcl"), "w") as f:
        f.write("# cfg\n")
    reset()
    v.tflint_module(repo, mod, use_docker=False)
    check("tflint.hcl is passed when present", any("--config" in c and "tflint.hcl" in c for c in calls()), str(calls()))
    reset()
    os.environ["FAKE_TFSEC_RC"] = "1"
    g = v.gate(comp, repo_root=repo, module_dir=mod, use_docker=False)
    check("tfsec failure fails the gate", (not g.ok) and "[FAIL] tfsec" in g.feedback)

    reset(); with_tools("docker")
    r = v.checkov_plan(repo, comp, use_docker=True)
    cmd = calls()[0] if calls() else ""
    check("docker checkov uses the public image and the plan file",
          cmd.startswith("docker run --rm") and "bridgecrew/checkov" in cmd and "-f /work/plan.json" in cmd, cmd)
    check("no organisation-specific registry anywhere",
          "acmecorp" not in cmd and "artifactory" not in cmd and "acmecorp" not in v.TFLINT_IMAGE + v.TFSEC_IMAGE)


def test_cli_and_apply_time_fmt_report():
    print("\n# validate CLI + `compose --apply` fmt report")
    reset(); with_tools("terragrunt")
    comp = component()
    with captured() as out:
        rc = v._main([comp, "--repo-root", comp])
    check("validate CLI: exit 0 + GATE: PASS", rc == 0 and "GATE: PASS" in out.getvalue())
    os.environ["FAKE_FMT_RC"] = "1"
    with captured() as out:
        rc = v._main([comp, "--repo-root", comp])
    check("validate CLI: exit 1 + GATE: FAIL", rc == 1 and "GATE: FAIL" in out.getvalue())

    from terra_pilot.pipeline import compose
    intent = {"resource_type": "ec2", "project": "auth", "env": "prod", "specifics": {},
              "operation": "create", "reference": None}
    gen = 'inputs = {\n  ami_id = "ami-1"\n}\n'
    gw = FakeGateway(lambda k, m: intent if k == "intent" else gen).start()
    point_at(gw)
    try:
        for rc_fmt, fix_rc, expect in (("0", "0", "# fmt check: ok"),
                                       ("1", "0", "# fmt: reformatted inputs.hcl"),
                                       ("1", "1", "# fmt check: FAILED")):
            reset(); with_tools("terragrunt")
            os.environ["FAKE_FMT_RC"] = rc_fmt; os.environ["FAKE_FMT_FIX_RC"] = fix_rc
            repo = copy_fixture("myrepo")
            with captured() as out:
                rc = compose.run_cli(repo, ["ec2", "for", "auth", "prod", "--apply"])
            check(f"--apply reports fmt result ({expect})", rc == 0 and expect in out.getvalue(), out.getvalue()[-200:])
            if fix_rc == "0" and rc_fmt == "1":
                check("only the written file was formatted (--file, not the whole dir)",
                      any(c.startswith("terragrunt hcl fmt --file inputs.hcl") for c in calls()), str(calls()))
        reset(); with_tools()
        repo = copy_fixture("myrepo")
        with captured() as out:
            compose.run_cli(repo, ["ec2", "for", "auth", "prod", "--apply"])
        check("--apply without terragrunt reports the skip", "fmt check skipped" in out.getvalue())
    finally:
        gw.stop()


def test_real_terragrunt_if_installed():
    print("\n# real terragrunt / terraform (skipped when not installed)")
    os.environ["PATH"] = REAL_PATH
    if not shutil.which("terragrunt"):
        return print("  skip terragrunt not installed")
    comp = component()
    with open(os.path.join(comp, "inputs.hcl"), "w") as f:
        f.write('inputs = {\n  a = "x"\n  long_name = 1\n}\n')      # misaligned '=' -> not canonical
    r = v.fmt_check(comp)
    check("real `terragrunt hcl fmt --check` flags a non-canonical file", not r.ok and not r.skipped, r.output[-200:])
    g = v.gate(comp, repo_root=comp)
    check("gate FAILs on it (real exit code)", not g.ok and "[FAIL] fmt" in g.feedback)
    f = v.format_file(os.path.join(comp, "inputs.hcl"))
    check("format_file rewrites just that file", f.ok, f.output[-200:])
    check("file is canonical now", "a         = \"x\"" in open(os.path.join(comp, "inputs.hcl")).read())
    check("gate passes after formatting", v.gate(comp, repo_root=comp).ok)
    if shutil.which("terraform"):
        plain = tempfile.mkdtemp(prefix="tp_plain_")
        with open(os.path.join(plain, "main.tf"), "w") as fh:
            fh.write('resource "null_resource" "a" {\n triggers = {\n a = 1\n  bb = 2\n }\n}\n')
        check("real `terraform fmt -check` flags it", not v.fmt_check(plain).ok)
        check("real terraform fmt fixes it", v.format_file(os.path.join(plain, "main.tf")).ok and v.fmt_check(plain).ok)


def test_real_online_gate_if_terragrunt_installed():
    print("\n# real online gate: validate -> plan -> show (built-in terraform_data, no provider/creds)")
    os.environ["PATH"] = REAL_PATH
    if not (shutil.which("terragrunt") and shutil.which("terraform")):
        return print("  skip terragrunt/terraform not installed")
    import json as _json
    d = tempfile.mkdtemp(prefix="tp_online_")
    os.makedirs(os.path.join(d, "mod")); os.makedirs(os.path.join(d, "comp"))
    with open(os.path.join(d, "mod", "main.tf"), "w") as f:
        f.write('variable "name" { type = string }\nresource "terraform_data" "x" { input = var.name }\n')
    comp = os.path.join(d, "comp")
    with open(os.path.join(comp, "inputs.hcl"), "w") as f:
        f.write("inputs = {}\n")
    def tg(inputs):
        with open(os.path.join(comp, "terragrunt.hcl"), "w") as f:
            f.write('terraform {\n  source = "%s/mod"\n}\n%s' % (d, inputs))
    tg("")
    g = v.gate(comp, repo_root=d, online=True, use_docker=False)
    names = {s.name: s for s in g.stages}
    check("missing variable: plan fails fast (no hang on the interactive prompt)",
          (not g.ok) and names["terragrunt validate"].ok and not names["terragrunt plan"].ok, g.feedback[-300:])
    tg('inputs = {\n  name = "hello"\n}\n')
    g = v.gate(comp, repo_root=d, online=True, use_docker=False)
    names = {s.name: s for s in g.stages}
    check("complete inputs: validate, plan and plan->json all pass",
          all(names[n].ok for n in ("terragrunt validate", "terragrunt plan", "plan->json")), g.feedback[-300:])
    try:
        plan = _json.loads(open(os.path.join(comp, "plan.json")).read())
        check("plan.json is valid JSON describing the resource",
              any(rc["type"] == "terraform_data" for rc in plan.get("resource_changes", [])))
    except (ValueError, OSError) as e:
        check("plan.json is valid JSON describing the resource", False, str(e))


if __name__ == "__main__":
    try:
        for fn in [test_hcl_text_problems, test_hcl_sanity_stage, test_fmt_stage, test_gate_offline,
                   test_gate_online_chain, test_module_lint_tier_and_docker_shape,
                   test_cli_and_apply_time_fmt_report, test_real_terragrunt_if_installed, test_real_online_gate_if_terragrunt_installed]:
            fn()
    finally:
        os.environ["PATH"] = REAL_PATH
    finish()
