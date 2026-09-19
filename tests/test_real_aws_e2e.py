"""compose -> apply -> the REAL online gate, against the real AWS provider.

Proves that what compose writes is not just well-formed HCL but actually plans
against the module it targets, and that the gate catches a generation that omits
required inputs. Opt-in (downloads the ~700 MB AWS provider, needs network):

    TP_REAL_AWS=1 python tests/test_real_aws_e2e.py

Uses dummy credentials with the provider's skip_* flags, a local backend and an
`inputs = read_terragrunt_config(inputs.hcl).inputs` root (tests/data/root_offline.hcl),
so no AWS account is touched. The plugin cache lives in ~/.terra-pilot/tf_plugin_cache.
"""
import os
import shutil

import harness
from harness import check, copy_fixture, finish, point_at, read
from fake_gateway import FakeGateway

if os.environ.get("TP_REAL_AWS") != "1":
    print("skip: set TP_REAL_AWS=1 to run against the real AWS provider (network, ~700 MB)")
    raise SystemExit(0)
brew = "/home/linuxbrew/.linuxbrew/bin"
os.environ["PATH"] += os.pathsep + brew if os.path.isdir(brew) else ""
if not (shutil.which("terragrunt") and shutil.which("terraform")):
    print("skip: terragrunt/terraform not installed")
    raise SystemExit(0)
cache = os.path.expanduser("~/.terra-pilot/tf_plugin_cache")
os.makedirs(cache, exist_ok=True)
os.environ["TF_PLUGIN_CACHE_DIR"] = cache

from terra_pilot.pipeline import compose
from terra_pilot.utils import validate as v

VALID = ('inputs = {\n  ami_id = "ami-0c55b159cbfafe1f0"\n  instance_type = "t3.small"\n'
         '  subnet_id = "subnet-0auth002"\n  vpc_id = "vpc-0auth002"\n'
         '  security_group_ids = ["sg-0auth002"]\n  enable_monitoring = false\n'
         '  workload_subnets = []\n}\n')
MISSING = 'inputs = {\n  instance_type = "t3.small"\n}\n'      # required inputs omitted


def offline_repo():
    repo = copy_fixture("myrepo")
    shutil.copy(os.path.join(harness.HERE, "data", "root_offline.hcl"), os.path.join(repo, "root.hcl"))
    return repo


def generate_and_gate(text):
    repo = offline_repo()
    gw = FakeGateway(lambda k, m: text).start()
    point_at(gw)
    try:
        a = compose.compose(repo, resource_type="ec2", project="auth", env="prod",
                            reference={"project": "auth", "env": "nonprod", "component": "ec2"})
    finally:
        gw.stop()
    compose.write_to_tree(a)
    v.format_file(a.inputs_file_path)
    return repo, a, v.gate(a.component_dir, repo_root=repo, online=True, use_docker=False)


print("\n# valid generation plans against the real module + provider")
repo, a, g = generate_and_gate(VALID)
st = {s.name: s for s in g.stages}
check("fmt ok after the file-scoped format", st["fmt"].ok, st["fmt"].output[-200:])
check("terragrunt validate passes", st["terragrunt validate"].ok, st["terragrunt validate"].output[-300:])
check("terragrunt plan passes with the real aws provider", st["terragrunt plan"].ok, st["terragrunt plan"].output[-300:])
check("plan.json produced", st["plan->json"].ok)
import json
plan = json.loads(read(os.path.join(a.component_dir, "plan.json")))
types = sorted(rc["type"] for rc in plan["resource_changes"])
check("plan creates the instance the inputs describe", types == ["aws_instance"], str(types))

print("\n# a generation that omits required inputs is caught by the gate")
repo, a, g = generate_and_gate(MISSING)
st = {s.name: s for s in g.stages}
check("gate fails", not g.ok)
check("failure is at plan (missing required variable), not silently passed",
      "terragrunt plan" in st and not st["terragrunt plan"].ok
      and ("ami_id" in st["terragrunt plan"].output or "No value for required variable" in st["terragrunt plan"].output),
      st.get("terragrunt plan").output[-300:] if "terragrunt plan" in st else str(list(st)))
finish()
