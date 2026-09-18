#!/usr/bin/env python3
"""
Showcase test: run the adaptive infra pilot across different repo conventions.

Usage:
    PYTHONPATH=src .venv/bin/python showcase_test.py [--terrads N]

Without --terrads: tests only the two fixture repos (fast, no extraction needed).
With --terrads N:  also extracts and tests N random TerraDS repos.
"""
import os
import sys
import json
import tarfile
import tempfile
import random
import argparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from terra_pilot.core.convention import detect_convention
from terra_pilot.search.index import TerraPilotIndex

# Force pure-Python parser so it works everywhere
os.environ["FORCE_PY_PARSER"] = "1"

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")
TERRADS = os.path.join(HERE, "data", "TerraDS_CodeRepos")

# --- Helpers ---

def hr(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")

def test_convention(repo_path, label):
    conv = detect_convention(repo_path)
    print(f"  Convention kind : {conv.kind}")
    print(f"  Confidence      : {conv.confidence}")
    print(f"  Module dirs     : {conv.module_dirs or '(none)'}")
    print(f"  Var files       : {len(conv.var_files)} found")
    return conv

def test_index(repo_path, label):
    try:
        idx = TerraPilotIndex(repo_path).build()
        syms = idx.all_symbols()
        kinds = {}
        for _, s in syms:
            kinds[s.kind] = kinds.get(s.kind, 0) + 1
        print(f"  Files indexed   : {len(idx.files)}")
        print(f"  Symbols         : {len(syms)}")
        print(f"  Symbol kinds    : {json.dumps(kinds, indent=None)}")
        return idx
    except Exception as e:
        print(f"  Index FAILED: {e}")
        return None

def test_catalog(idx, label):
    if idx is None:
        print("  (skipped - no index)")
        return
    try:
        from terra_pilot.search.catalog import ModuleCatalog
        cat = ModuleCatalog(idx)
        mods = list(cat.modules.values())
        print(f"  Modules found   : {len(mods)}")
        for m in mods[:5]:
            req = [i.name for i in m.required_inputs]
            print(f"    {m.key}  resources={m.resource_types}  required={req}  reuse={m.reuse_count}")
        if len(mods) > 5:
            print(f"    ... and {len(mods)-5} more")
        return cat
    except Exception as e:
        print(f"  Catalog FAILED: {e}")
        return None

def test_plan(idx, cat, label):
    if idx is None or cat is None:
        print("  (skipped - no index/catalog)")
        return
    try:
        from terra_pilot.llm.planner import Planner
        pl = Planner(idx, cat)

        # Build meaningful intents from actual resource types found in the repo
        intents = []
        if cat.modules:
            for mod in list(cat.modules.values())[:3]:
                for rtype in mod.resource_types[:2]:
                    # Turn "aws_instance" -> "create an ec2 instance"
                    # Turn "azurerm_kubernetes_cluster" -> "provision a kubernetes cluster"
                    clean = rtype.replace("aws_", "").replace("azurerm_", "").replace("google_", "") \
                                 .replace("digitalocean_", "").replace("_", " ")
                    intents.append(f"provision a {clean}")
                if not mod.resource_types:
                    # Use required inputs as hints
                    if mod.required_inputs:
                        hint = mod.required_inputs[0].name.replace("_", " ")
                        intents.append(f"create infrastructure with {hint}")

        # Fallback intents if nothing was found
        if not intents:
            intents = ["create an ec2 instance", "provision a security group"]

        # Deduplicate and cap
        seen = set()
        unique_intents = []
        for i in intents:
            if i not in seen:
                seen.add(i)
                unique_intents.append(i)
        intents = unique_intents[:3]

        for intent in intents:
            p = pl.plan(intent)
            print(f"  Intent          : \"{intent}\"")
            print(f"  Decision        : {p.decision}")
            if p.module:
                print(f"  Module          : {p.module.key} (reuse={p.module.reuse_count})")
            print(f"  Rendered:")
            for line in p.rendered.splitlines()[:5]:
                print(f"    {line}")
            if len(intents) > 1:
                print()
    except Exception as e:
        print(f"  Plan FAILED: {e}")

def test_root_schema(repo_path):
    try:
        from terra_pilot.models import root_schema
        schema = root_schema.parse(repo_path)
        print(f"  Confidence      : {schema.confidence}")
        print(f"  Include target  : {schema.include_target or '(none)'}")
        print(f"  Module template : {schema.module_source_template or '(none)'}")
        print(f"  Env config file : {schema.env_config_filename or '(none)'}")
        print(f"  Backend type    : {schema.backend_type or '(none)'}")
    except Exception as e:
        print(f"  Schema FAILED: {e}")


def run_full_test(repo_path, label):
    hr(label)
    print(f"  Path: {repo_path}\n")

    print("-- Convention Detection --")
    conv = test_convention(repo_path, label)

    print("\n-- Root Schema --")
    test_root_schema(repo_path)

    print("\n-- Index --")
    idx = test_index(repo_path, label)

    print("\n-- Catalog --")
    cat = test_catalog(idx, label)

    print("\n-- Planner --")
    test_plan(idx, cat, label)

    return conv


# --- Main ---

def main():
    parser = argparse.ArgumentParser(description="Showcase adaptive infra pilot")
    parser.add_argument("--terrads", type=int, default=0,
                        help="Number of random TerraDS repos to extract and test")
    args = parser.parse_args()

    results = {"terragrunt": 0, "tf-modules": 0, "tf-flat": 0}

    # Fixture repos
    for name in sorted(os.listdir(FIXTURES)):
        path = os.path.join(FIXTURES, name)
        if os.path.isdir(path):
            conv = run_full_test(path, f"FIXTURE: {name}")
            results[conv.kind] = results.get(conv.kind, 0) + 1

    # TerraDS repos
    if args.terrads > 0 and os.path.isdir(TERRADS):
        archives = [f for f in os.listdir(TERRADS) if f.endswith(".tar.gz")]
        sample = random.sample(archives, min(args.terrads, len(archives)))

        for archive_name in sample:
            archive_path = os.path.join(TERRADS, archive_name)
            repo_id = archive_name.replace(".tar.gz", "")

            with tempfile.TemporaryDirectory() as tmpdir:
                try:
                    with tarfile.open(archive_path, "r:gz") as tar:
                        tar.extractall(tmpdir)
                except Exception as e:
                    print(f"\n  SKIP {repo_id}: extraction failed ({e})")
                    continue

                extracted = os.listdir(tmpdir)
                if len(extracted) == 1 and os.path.isdir(os.path.join(tmpdir, extracted[0])):
                    repo_path = os.path.join(tmpdir, extracted[0])
                else:
                    repo_path = tmpdir

                conv = run_full_test(repo_path, f"TerraDS: {repo_id}")
                results[conv.kind] = results.get(conv.kind, 0) + 1

    # Summary
    hr("SUMMARY")
    total = sum(results.values())
    print(f"  Total repos tested: {total}")
    for kind, count in sorted(results.items()):
        pct = (count / total * 100) if total else 0
        print(f"    {kind:15s}: {count:3d}  ({pct:.0f}%)")
    print()


if __name__ == "__main__":
    main()
