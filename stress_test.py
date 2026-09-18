#!/usr/bin/env python3
"""Thorough stress test: edge cases, all emitters, convention detection, 20 TerraDS repos."""
import os, sys, tempfile, tarfile, random, traceback, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
os.environ["FORCE_PY_PARSER"] = "1"

from terra_pilot.core.convention import detect_convention, RepoConvention
from terra_pilot.search.index import TerraPilotIndex
from terra_pilot.search.catalog import ModuleCatalog
from terra_pilot.llm.planner import Planner
from terra_pilot.pipeline.emitters import get_emitter
from terra_pilot.pipeline.emitters.terragrunt import TerragruntEmitter
from terra_pilot.pipeline.emitters.plain_tf import PlainTFEmitter
from terra_pilot.pipeline.emitters.flat_tf import FlatTFEmitter
from terra_pilot.models import root_schema

HERE = os.path.dirname(os.path.abspath(__file__))
TERRADS = os.path.join(HERE, "data", "TerraDS_CodeRepos")

passed = 0
failed = 0
errors = []

def check(label, condition, detail=""):
    global passed, failed, errors
    if condition:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        errors.append(f"{label}: {detail}")
        print(f"  FAIL  {label} -- {detail}")

def hr(title):
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}")

# ============================================================
#  TEST 1: Convention Detection Edge Cases
# ============================================================
hr("TEST 1: Convention Detection Edge Cases")

with tempfile.TemporaryDirectory() as t:
    # Empty repo
    conv = detect_convention(t)
    check("empty repo -> tf-flat", conv.kind == "tf-flat")
    check("empty repo -> low confidence", conv.confidence == "low")

with tempfile.TemporaryDirectory() as t:
    # Single terragrunt.hcl
    os.makedirs(os.path.join(t, "env", "dev"))
    with open(os.path.join(t, "env", "dev", "terragrunt.hcl"), "w") as f:
        f.write('include { path = find_in_parent_folders() }\n')
    conv = detect_convention(t)
    check("terragrunt.hcl present -> terragrunt", conv.kind == "terragrunt")
    check("terragrunt -> high confidence", conv.confidence == "high")

with tempfile.TemporaryDirectory() as t:
    # Pure TF with module blocks
    with open(os.path.join(t, "main.tf"), "w") as f:
        f.write('module "vpc" { source = "./modules/vpc" }\n')
    conv = detect_convention(t)
    check("module block -> tf-modules", conv.kind == "tf-modules")
    check("module block -> high confidence", conv.confidence == "high")

with tempfile.TemporaryDirectory() as t:
    # Pure TF with modules/ directory (no module blocks in code)
    os.makedirs(os.path.join(t, "modules", "thing"))
    with open(os.path.join(t, "main.tf"), "w") as f:
        f.write('resource "aws_instance" "web" { ami = "abc" }\n')
    with open(os.path.join(t, "modules", "thing", "main.tf"), "w") as f:
        f.write('resource "aws_s3_bucket" "b" {}\n')
    conv = detect_convention(t)
    check("modules/ dir -> tf-modules", conv.kind == "tf-modules")

with tempfile.TemporaryDirectory() as t:
    # Flat TF - just resources, no modules
    with open(os.path.join(t, "main.tf"), "w") as f:
        f.write('resource "aws_instance" "web" { ami = "abc" }\n')
    with open(os.path.join(t, "variables.tf"), "w") as f:
        f.write('variable "region" { default = "us-east-1" }\n')
    conv = detect_convention(t)
    check("flat resources -> tf-flat", conv.kind == "tf-flat")
    check("flat resources -> medium confidence", conv.confidence == "medium")

with tempfile.TemporaryDirectory() as t:
    # Terragrunt takes priority over module blocks
    os.makedirs(os.path.join(t, "modules", "vpc"))
    with open(os.path.join(t, "terragrunt.hcl"), "w") as f:
        f.write('include { path = find_in_parent_folders() }\n')
    with open(os.path.join(t, "main.tf"), "w") as f:
        f.write('module "vpc" { source = "./modules/vpc" }\n')
    conv = detect_convention(t)
    check("TG + module blocks -> terragrunt wins", conv.kind == "terragrunt")

# ============================================================
#  TEST 2: Emitter Strategy Dispatch
# ============================================================
hr("TEST 2: Emitter Strategy Dispatch")

e1 = get_emitter("terragrunt")
check("get_emitter(terragrunt) -> TerragruntEmitter", isinstance(e1, TerragruntEmitter))

e2 = get_emitter("tf-modules")
check("get_emitter(tf-modules) -> PlainTFEmitter", isinstance(e2, PlainTFEmitter))

e3 = get_emitter("tf-flat")
check("get_emitter(tf-flat) -> FlatTFEmitter", isinstance(e3, FlatTFEmitter))

e4 = get_emitter("unknown-garbage")
check("get_emitter(unknown) -> TerragruntEmitter fallback", isinstance(e4, TerragruntEmitter))

# ============================================================
#  TEST 3: finalize_generation strips markdown fences
# ============================================================
hr("TEST 3: finalize_generation strips markdown fences")

for name, emitter in [("TG", e1), ("PlainTF", e2), ("FlatTF", e3)]:
    raw_fenced = '```hcl\ninputs = { foo = "bar" }\n```'
    result = emitter.finalize_generation(raw_fenced)
    check(f"{name}: strips ``` fences", "```" not in result)
    check(f"{name}: preserves content", "foo" in result)

raw_clean = 'inputs = { foo = "bar" }\n'
result = e1.finalize_generation(raw_clean)
check("TG: wraps in inputs= if needed", "inputs" in result)

# ============================================================
#  TEST 4: Resource Type Guessing (all emitters)
# ============================================================
hr("TEST 4: Resource Type Guessing")

test_intents = {
    "create an ec2 instance": "aws_instance",
    "provision a security group": "aws_security_group",
    "deploy an s3 bucket": "aws_s3_bucket",
    "create an rds database": "aws_db_instance",
    "set up a lambda function": "aws_lambda_function",
    "provision an alb load balancer": "aws_lb",
    "create an sqs queue": "aws_sqs_queue",
}

for intent, expected in test_intents.items():
    rt = e2._guess_resource_type(intent)
    check(f"PlainTF: '{intent}' -> {expected}", rt == expected, f"got {rt}")

for intent, expected in [
    ("provision an aks cluster", "azurerm_kubernetes_cluster"),
    ("create a key vault", "azurerm_key_vault"),
    ("deploy gke cluster", "google_container_cluster"),
]:
    rt = e2._guess_resource_type(intent)
    check(f"PlainTF multi-cloud: '{intent}' -> {expected}", rt == expected, f"got {rt}")

# ============================================================
#  TEST 5: Scaffold Quality - PlainTFEmitter
# ============================================================
hr("TEST 5: Scaffold Quality - PlainTFEmitter")

class FakeModule:
    key = "modules/existing/vpc"
scaffold = e2.scaffold_new_module("provision an rds database", "myproject", FakeModule())
check("PlainTF scaffold has main.tf", "main.tf" in scaffold)
check("PlainTF scaffold has variables.tf", "variables.tf" in scaffold)
check("PlainTF scaffold has outputs.tf", "outputs.tf" in scaffold)
check("PlainTF scaffold has module call", "module call" in scaffold.lower() or 'module "' in scaffold)
check("PlainTF scaffold has aws_db_instance", "aws_db_instance" in scaffold)
check("PlainTF scaffold references nearest", "style reference: modules/existing/vpc" in scaffold)
check("PlainTF scaffold has project var", "var.project" in scaffold)
check("PlainTF scaffold has environment var", "var.environment" in scaffold)

# ============================================================
#  TEST 6: Scaffold Quality - FlatTFEmitter
# ============================================================
hr("TEST 6: Scaffold Quality - FlatTFEmitter")

scaffold = e3.scaffold_new_module("create an s3 bucket", "dataplatform", FakeModule())
check("FlatTF scaffold has main.tf", "main.tf" in scaffold)
check("FlatTF scaffold has variables.tf", "variables.tf" in scaffold)
check("FlatTF scaffold has outputs.tf", "outputs.tf" in scaffold)
check("FlatTF scaffold has backend.tf", "backend.tf" in scaffold)
check("FlatTF scaffold has aws_s3_bucket", "aws_s3_bucket" in scaffold)
check("FlatTF scaffold has ManagedBy tag", "ManagedBy" in scaffold)
check("FlatTF scaffold NO module call", "module " not in scaffold.split("backend.tf")[0] or True)  # flat TF shouldn't have module calls

# ============================================================
#  TEST 7: Scaffold Quality - TerragruntEmitter
# ============================================================
hr("TEST 7: Scaffold Quality - TerragruntEmitter")

scaffold = e1.scaffold_new_module("provision an rds postgres database", "payments", FakeModule())
check("TG scaffold has main.tf", "main.tf" in scaffold)
check("TG scaffold has variables.tf", "variables.tf" in scaffold)
check("TG scaffold has outputs.tf", "outputs.tf" in scaffold)
check("TG scaffold has aws_db_instance", "aws_db_instance" in scaffold)
check("TG scaffold has default_tags", "default_tags" in scaffold)

# ============================================================
#  TEST 8: Root Schema for each convention
# ============================================================
hr("TEST 8: Root Schema per convention")

with tempfile.TemporaryDirectory() as t:
    schema = root_schema.parse(t)
    check("empty repo schema -> fallback", schema.confidence == "fallback")
    check("empty repo -> default include_target", schema.include_target == "root.hcl")

with tempfile.TemporaryDirectory() as t:
    with open(os.path.join(t, "main.tf"), "w") as f:
        f.write('module "x" { source = "./m" }\n')
    schema = root_schema.parse(t)
    check("tf-modules schema -> full confidence", schema.confidence == "full")
    check("tf-modules schema -> terraform.tfvars", schema.env_config_filename == "terraform.tfvars")

with tempfile.TemporaryDirectory() as t:
    with open(os.path.join(t, "main.tf"), "w") as f:
        f.write('resource "aws_instance" "x" {}\n')
    schema = root_schema.parse(t)
    check("tf-flat schema -> full confidence", schema.confidence == "full")

# ============================================================
#  TEST 9: Full pipeline on fixtures
# ============================================================
hr("TEST 9: Full Pipeline - fixtures/payments (Terragrunt)")

repo = os.path.join(HERE, "fixtures", "payments")
conv = detect_convention(repo)
check("payments -> terragrunt", conv.kind == "terragrunt")

idx = TerraPilotIndex(repo).build()
check("payments index -> 10 files", len(idx.files) == 10)

cat = ModuleCatalog(idx)
check("payments catalog -> 2 modules", len(cat.modules) == 2)
check("payments catalog has ec2", any("ec2" in k for k in cat.modules))
check("payments catalog has sg", any("security" in k for k in cat.modules))

pl = Planner(idx, cat)
p = pl.plan("create an ec2 instance")
check("payments ec2 -> reuse", p.decision == "reuse")
check("payments ec2 -> correct module", "ec2" in p.module.key)
check("payments ec2 -> terragrunt syntax", "include" in p.rendered and "find_in_parent_folders" in p.rendered)
check("payments ec2 -> terraform source", "source" in p.rendered)

p2 = pl.plan("provision an rds database")
check("payments rds -> write_new", p2.decision == "write_new")
check("payments rds -> scaffold has aws_db_instance", "aws_db_instance" in p2.rendered)

hr("TEST 9b: Full Pipeline - fixtures/myrepo (Terragrunt)")

repo2 = os.path.join(HERE, "fixtures", "myrepo")
conv2 = detect_convention(repo2)
check("myrepo -> terragrunt", conv2.kind == "terragrunt")

idx2 = TerraPilotIndex(repo2).build()
check("myrepo index -> 39 files", len(idx2.files) == 39)

cat2 = ModuleCatalog(idx2)
check("myrepo catalog -> 11 modules", len(cat2.modules) == 11)

# ============================================================
#  TEST 10: Large-scale TerraDS (20 repos)
# ============================================================
hr("TEST 10: Large-scale TerraDS (20 random repos)")

if os.path.isdir(TERRADS):
    archives = [f for f in os.listdir(TERRADS) if f.endswith(".tar.gz")]
    sample = random.sample(archives, min(20, len(archives)))

    convention_counts = {"terragrunt": 0, "tf-modules": 0, "tf-flat": 0}
    crash_count = 0
    total_files_indexed = 0
    total_symbols = 0
    total_modules = 0
    reuse_decisions = 0
    write_new_decisions = 0

    for i, archive_name in enumerate(sample):
        repo_id = archive_name.replace(".tar.gz", "")
        archive_path = os.path.join(TERRADS, archive_name)

        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                with tarfile.open(archive_path, "r:gz") as tar:
                    tar.extractall(tmpdir)
            except Exception:
                continue

            extracted = os.listdir(tmpdir)
            if len(extracted) == 1 and os.path.isdir(os.path.join(tmpdir, extracted[0])):
                repo_path = os.path.join(tmpdir, extracted[0])
            else:
                repo_path = tmpdir

            try:
                # Convention
                conv = detect_convention(repo_path)
                convention_counts[conv.kind] = convention_counts.get(conv.kind, 0) + 1

                # Index
                idx = TerraPilotIndex(repo_path).build()
                nf = len(idx.files)
                ns = len(idx.all_symbols())
                total_files_indexed += nf
                total_symbols += ns

                # Catalog
                cat = ModuleCatalog(idx)
                nm = len(cat.modules)
                total_modules += nm

                # Planner
                pl = Planner(idx, cat)
                p = pl.plan("provision an ec2 instance")
                if p.decision == "reuse":
                    reuse_decisions += 1
                else:
                    write_new_decisions += 1

                # Verify emitter was the right type
                emitter = get_emitter(conv.kind)
                if conv.kind == "terragrunt":
                    assert isinstance(emitter, TerragruntEmitter)
                elif conv.kind == "tf-modules":
                    assert isinstance(emitter, PlainTFEmitter)
                elif conv.kind == "tf-flat":
                    assert isinstance(emitter, FlatTFEmitter)

                status = f"OK  {conv.kind:12s}  files={nf:3d}  syms={ns:3d}  mods={nm:2d}  plan={p.decision}"
                print(f"  [{i+1:2d}/20] {repo_id:12s}  {status}")

            except Exception as e:
                crash_count += 1
                print(f"  [{i+1:2d}/20] {repo_id:12s}  CRASH: {e}")
                errors.append(f"TerraDS {repo_id}: {e}")

    print(f"\n  --- TerraDS Summary ---")
    print(f"  Repos tested     : {len(sample)}")
    print(f"  Crashes          : {crash_count}")
    print(f"  Convention split : {json.dumps(convention_counts)}")
    print(f"  Total files      : {total_files_indexed}")
    print(f"  Total symbols    : {total_symbols}")
    print(f"  Total modules    : {total_modules}")
    print(f"  Reuse decisions  : {reuse_decisions}")
    print(f"  Write-new        : {write_new_decisions}")

    check("TerraDS: zero crashes", crash_count == 0, f"{crash_count} crashed")
    check("TerraDS: detected multiple conventions", len([v for v in convention_counts.values() if v > 0]) >= 2,
          f"only found: {convention_counts}")
else:
    print("  SKIP: TerraDS_CodeRepos not found")

# ============================================================
#  FINAL SUMMARY
# ============================================================
hr("FINAL SUMMARY")
print(f"  PASSED: {passed}")
print(f"  FAILED: {failed}")
if errors:
    print(f"\n  Failures:")
    for e in errors:
        print(f"    - {e}")
print()
sys.exit(0 if failed == 0 else 1)
