#!/usr/bin/env python3
"""Plan-quality sweep over random TerraDS repos (offline, no LLM).

`stress_test.py` shows the pipeline does not crash on real repos; this measures
whether its reuse-vs-write decisions are RIGHT, with ground truth derived from the
repo itself:

  positive : for a module whose main resource is `aws_x_y`, ask for "x y". The plan
             must reuse a module that declares that resource type (strict), or one
             whose name/resources share a word with the request (lenient: asking for
             "iam policy" and getting the repo's `iam` module is a fair reuse).
  negative : ask for a resource type none of whose words appear in ANY module name or
             resource type of this repo (taken from another sampled repo). The plan must
             NOT reuse anything; reusing is the wrong-module bug that let an ec2 schema
             leak into an rds request.

Both the `plan` command's planner and compose's retrieval.find_module are scored.

    python quality_sweep.py [--n 100] [--seed 42] [--min-precision 0.85] [--max-false-reuse 0.05]

Exit code 1 when a threshold is missed, so it can run as a scheduled CI job.
"""
import argparse
import atexit
import os
import random
import re
import shutil
import sys
import tarfile
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "src"))
os.environ["FORCE_PY_PARSER"] = "1"
os.environ.setdefault("LLM_EMBED_ENABLED", "0")
os.environ.setdefault("LLM_EMBED_BACKEND", "none")
os.environ.pop("LLM_RERANK_GATEWAY_BASE", None)

from terra_pilot.search.index import TerraPilotIndex
from terra_pilot.search.catalog import ModuleCatalog
from terra_pilot.search import retrieval
from terra_pilot.llm.planner import Planner

TERRADS = os.path.join(HERE, "data", "TerraDS_CodeRepos")
_TYPE = re.compile(r"^aws_[a-z0-9_]+$")


def words(rtype):
    return rtype[len("aws_"):].replace("_", " ")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--min-precision", type=float, default=0.85)
    ap.add_argument("--max-false-reuse", type=float, default=0.05)
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args(argv)

    archives = sorted(f for f in os.listdir(TERRADS) if f.endswith(".tar.gz"))
    random.Random(a.seed).shuffle(archives)
    repos = []          # (name, path, {module_key: [aws types]}, catalog)
    tmp = tempfile.mkdtemp(prefix="tp_sweep_")
    atexit.register(shutil.rmtree, tmp, ignore_errors=True)   # ~300 MB per 200 repos
    for fn in archives:
        if len(repos) >= a.n:
            break
        dest = os.path.join(tmp, fn[:-7])
        try:
            with tarfile.open(os.path.join(TERRADS, fn)) as tar:
                tar.extractall(dest, filter="data") if sys.version_info >= (3, 12) else tar.extractall(dest)
            idx = TerraPilotIndex(dest).build()
            cat = ModuleCatalog(idx)
        except Exception:
            continue
        mods = {k: sorted({t for t in m.resource_types if _TYPE.match(t)})
                for k, m in cat.modules.items()}
        mods = {k: v for k, v in mods.items() if v}
        if len(mods) >= 2:
            repos.append((fn, dest, mods, idx, cat))
    if len(repos) < 2:
        print("not enough usable repos"); return 2

    all_types = sorted({t for _, _, mods, _, _ in repos for ts in mods.values() for t in ts})
    rnd = random.Random(a.seed)
    pos = {"planner": [0, 0], "compose": [0, 0]}
    lenient = {"planner": 0, "compose": 0}
    neg = {"planner": [0, 0], "compose": [0, 0]}      # [false reuse, total]
    misses = []
    for name, path, mods, idx, cat in repos:
        planner = Planner(idx, cat)
        repo_types = {t for ts in mods.values() for t in ts}
        repo_words = set()          # every word of every module name / resource type, any provider
        for k, m in cat.modules.items():
            repo_words |= set(re.split(r"[^a-z0-9]+", str(k).lower()))
            for t in m.resource_types:
                repo_words |= set(re.split(r"[^a-z0-9]+", str(t).lower()))
        # positive: a module with a single distinctive main type
        key, types = rnd.choice(sorted(mods.items()))
        want = types[0]
        p = planner.plan(words(want))
        pos["planner"][1] += 1
        if p.decision == "reuse" and p.module and want in p.module.resource_types:
            pos["planner"][0] += 1
            lenient["planner"] += 1
        elif p.decision == "reuse" and p.module and retrieval.module_relates(want[4:], p.module):
            lenient["planner"] += 1
            misses.append((name, "planner-lenient", words(want), p.module.key))
        else:
            misses.append((name, "planner", words(want), p.module.key if p.module else None))
        c = retrieval.find_module(path, words(want).replace(" ", "_"))
        pos["compose"][1] += 1
        if c is not None and want in c.resource_types:
            pos["compose"][0] += 1
            lenient["compose"] += 1
        elif c is not None and retrieval.module_relates(want[4:], c):
            lenient["compose"] += 1
        # negative: a type this repo has no module for
        absent = [t for t in all_types if t not in repo_types
                  and not (set(t[len("aws_"):].split("_")) & repo_words)]
        if absent:
            t = rnd.choice(absent)
            p = planner.plan(words(t))
            neg["planner"][1] += 1
            neg["planner"][0] += p.decision == "reuse"
            if p.decision == "reuse":
                misses.append((name, "planner-neg", words(t), p.module.key if p.module else None))
            c = retrieval.find_module(path, t[len("aws_"):])
            neg["compose"][1] += 1
            neg["compose"][0] += c is not None
            if c is not None:
                misses.append((name, "compose-neg", t, c.key))

    print(f"repos scored: {len(repos)} (seed {a.seed})")
    ok = True
    for who in ("planner", "compose"):
        p_hit, p_tot = pos[who]
        n_hit, n_tot = neg[who]
        prec = p_hit / max(p_tot, 1)
        lenp = lenient[who] / max(p_tot, 1)
        fr = n_hit / max(n_tot, 1)
        print(f"{who:>8}: strict {p_hit}/{p_tot} = {prec:.1%}   lenient {lenp:.1%}   "
              f"false-reuse {n_hit}/{n_tot} = {fr:.1%}")
        ok &= lenp >= a.min_precision and fr <= a.max_false_reuse
    if a.verbose:
        for m in misses[:40]:
            print("  miss:", m)
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
