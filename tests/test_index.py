"""
Verification suite for the payments indexer prototype.

Runs against fixtures/payments (a synthetic Terragrunt repo mirroring the Acme
layout). Asserts the symbol table is correct and that each of the 5 edge types
is extracted and resolved. Pure-Python fallback parser is forced so this runs
anywhere (no tree-sitter / no network).
"""
import os
import sys

os.environ["FORCE_PY_PARSER"] = "1"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from index import TerraPilotIndex  # noqa: E402

REPO = os.path.join(os.path.dirname(HERE), "fixtures", "payments")

_passed = 0
_failed = 0


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
    print(f"Indexed {len(idx.files)} files, {len(idx.all_symbols())} symbols\n")

    # ---- Symbol table -----------------------------------------------------
    print("Symbol table:")
    ec2_main = "modules/aws/resource/ec2/main.tf"
    outline = idx.get_file_outline(ec2_main) or []
    names = {(s.kind, s.name) for s in outline}
    check("resource aws_instance.this", ("resource", "aws_instance.this") in names)
    check("resource random_string.name_suffix",
          ("resource", "random_string.name_suffix") in names)
    check("locals captured", any(s.kind == "locals" for s in outline))
    # nested dynamic block recorded as a child with parent
    nested = [s for s in outline if s.parent is not None]
    check("nested dynamic/ebs block has parent",
          any("ebs_block_device" in s.name or s.kind in ("dynamic", "block")
              for s in nested),
          detail=str([(s.kind, s.name, s.parent) for s in nested]))

    sg_vars = "modules/aws/resource/security_group/variables.tf"
    var_syms = {s.name for s in (idx.get_file_outline(sg_vars) or []) if s.kind == "variable"}
    check("sg variables present", {"environment", "vpc_id", "ingress_rules"} <= var_syms,
          detail=str(var_syms))

    # find_symbol across the repo
    hits = idx.find_symbol("aws_instance", kind="resource")
    check("find_symbol(resource)", any(s.name == "aws_instance.this" for _, s in hits))

    # ---- Edge type 1: module_source --------------------------------------
    print("\nEdge 1 — module_source:")
    ec2_tg = "live/nonprod/payments/ec2/terragrunt.hcl"
    imps = idx.get_imports(ec2_tg)
    src_edges = [e for e in imps if e.edge_type == "module_source"]
    check("ec2 terragrunt -> ec2 module",
          any(e.module.endswith("modules/aws/resource/ec2") for e in src_edges),
          detail=str([e.module for e in src_edges]))
    importers = idx.get_importers("ec2")
    check("get_importers(ec2) finds the env-tier config",
          any(f == ec2_tg for f in importers), detail=str(importers))

    # ---- Edge type 2: remote_state ---------------------------------------
    print("\nEdge 2 — remote_state cross-stack:")
    rs = [e for e in imps if e.edge_type == "remote_state"]
    check("remote_state upstream=security_group output=security_group_id",
          any(e.name == "security_group" and e.output == "security_group_id" for e in rs),
          detail=str([(e.name, e.output) for e in rs]))

    # ---- Edge type 3: var_ref --------------------------------------------
    print("\nEdge 3 — var_ref:")
    ec2_main_imps = idx.get_imports(ec2_main)
    vrefs = {e.name for e in ec2_main_imps if e.edge_type == "var_ref"}
    check("ec2 main.tf references var.instance_type / var.subnet_id",
          {"instance_type", "subnet_id", "ami_id"} <= vrefs, detail=str(vrefs))

    # ---- Edge type 4: output_ref -----------------------------------------
    print("\nEdge 4 — output_ref:")
    orefs = [e for e in imps if e.edge_type == "output_ref"]
    check("dependency.security_group.outputs.security_group_id captured",
          any(e.name == "security_group" and e.output == "security_group_id" for e in orefs),
          detail=str([(e.name, e.output) for e in orefs]))

    # ---- Edge type 5: terragrunt -----------------------------------------
    print("\nEdge 5 — terragrunt wiring:")
    tg = [e for e in imps if e.edge_type == "terragrunt"]
    tg_modules = {e.module for e in tg}
    check("include block captured", "include" in tg_modules, detail=str(tg_modules))
    check("find_in_parent_folders captured",
          "find_in_parent_folders" in tg_modules, detail=str(tg_modules))
    check("dependency block resolved to ../security_group",
          any(e.name == "security_group" and "security_group" in (e.module or "")
              for e in tg), detail=str([(e.name, e.module) for e in tg]))
    inputs_edge = [e for e in idx.get_imports(
        "live/nonprod/payments/ec2/inputs.hcl") if e.module == "inputs.hcl"] \
        if "live/nonprod/payments/ec2/inputs.hcl" in idx.files else ["n/a"]

    # ---- path resolution helper ------------------------------------------
    print("\nPath resolution:")
    resolved = idx._resolve_relative(ec2_tg, "../../../../modules/aws/resource/ec2")
    check("_resolve_relative normalizes the source path",
          resolved == "modules/aws/resource/ec2", detail=resolved)

    print(f"\n==== {_passed} passed, {_failed} failed ====")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
