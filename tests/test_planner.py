"""
Proves the reuse-vs-write workflow end to end against fixtures/payments:
  * intent that matches an existing module  -> decision "reuse" + inputs.hcl
  * intent with no matching module          -> decision "write_new" + scaffold
Forces the stdlib parser so it runs anywhere.
"""
import os
import sys

os.environ["FORCE_PY_PARSER"] = "1"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from index import TerraPilotIndex          # noqa: E402
from catalog import ModuleCatalog        # noqa: E402
from planner import Planner              # noqa: E402

REPO = os.path.join(os.path.dirname(HERE), "fixtures", "payments")
_passed = _failed = 0


def check(name, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS  {name}")
    else:
        _failed += 1
        print(f"  FAIL  {name}  {detail}")


def main():
    idx = TerraPilotIndex(REPO).build()
    cat = ModuleCatalog(idx)
    pl = Planner(idx, cat)

    print("Catalog:")
    for m in cat.modules.values():
        print(f"  {m.key}  resources={m.resource_types} "
              f"required={[i.name for i in m.required_inputs]} "
              f"optional={[i.name for i in m.optional_inputs]} "
              f"outputs={m.outputs} reuse={m.reuse_count}")
    check("catalog found ec2 + security_group modules",
          {os.path.basename(k) for k in cat.modules} >= {"ec2", "security_group"},
          detail=str(list(cat.modules)))
    ec2 = next(m for m in cat.modules.values() if m.name == "ec2")
    check("required vs optional split (description has a default => optional)",
          "environment" in [i.name for i in ec2.required_inputs])
    sg = next(m for m in cat.modules.values() if m.name == "security_group")
    check("sg.description is optional (has default)",
          "description" in [i.name for i in sg.optional_inputs],
          detail=str([(i.name, i.required) for i in sg.inputs]))
    check("ec2 reuse_count >=1 (env-tier sources it)", ec2.reuse_count >= 1,
          detail=str(ec2.used_by))

    print("\n--- REUSE path: 'create an ec2 instance for payments' ---")
    p1 = pl.plan("create an ec2 instance for payments nonprod")
    print(p1.rendered)
    check("decision == reuse", p1.decision == "reuse", detail=p1.decision)
    check("reuses the ec2 module", p1.module and p1.module.name == "ec2")
    check("emitted terraform source line", "modules/aws/resource/ec2" in p1.rendered)
    check("wires required input 'environment'", "environment =" in p1.rendered)
    check("learned remote_state upstream wiring",
          "module.remote_state.components" in p1.rendered, detail=p1.rendered)

    print("\n--- WRITE path: 'provision an rds postgres database' ---")
    p2 = pl.plan("provision an rds postgres database for payments")
    print(p2.rendered)
    check("decision == write_new", p2.decision == "write_new", detail=p2.decision)
    check("scaffolds aws_db_instance", "aws_db_instance" in p2.rendered,
          detail=p2.rendered)
    check("scaffold lists main/variables/outputs",
          all(f in p2.rendered for f in ("main.tf", "variables.tf", "outputs.tf")))

    print(f"\n==== {_passed} passed, {_failed} failed ====")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
