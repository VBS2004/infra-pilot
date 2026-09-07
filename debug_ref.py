"""Diagnostic for the 'reference ... inputs.hcl not found' bug.

Run on the laptop, against the REAL repo:
    python debug_ref.py "$REPO"
    # optionally override: python debug_ref.py "$REPO" billing ecs pre-prod aws

It prints exactly which paths the resolver builds and whether they exist, so we
can see why preprodbilling/ecs/inputs.hcl was reported missing.
"""
import os
import sys

import paths

repo = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("REPO", ".")
proj = sys.argv[2] if len(sys.argv) > 2 else "billing"
comp = sys.argv[3] if len(sys.argv) > 3 else "ecs"
word = sys.argv[4] if len(sys.argv) > 4 else "pre-prod"
prov = sys.argv[5] if len(sys.argv) > 5 else "aws"

print("=" * 72)
print("repo (raw)     :", repr(repo))
print("repo (abspath) :", os.path.abspath(repo))
print("project/comp   : %s / %s   env-word=%r  provider=%r" % (proj, comp, word, prov))
pdir = paths.project_dir(repo, proj, prov)
print("project_dir    :", pdir)
print("project_dir is a dir:", os.path.isdir(pdir))
print("=" * 72)

envs = paths.list_envs(repo, proj, provider=prov)
print("\nlist_envs(%s):" % proj)
print(" ", envs or "(none)")

chosen, cands = paths.match_env_dir(repo, proj, word, provider=prov)
print("\nmatch_env_dir(%r) -> chosen=%r  candidates=%r" % (word, chosen, cands))

print("\nPer-env config.yml 'environment' and %s/inputs.hcl presence:" % comp)
for e in envs:
    cfg = paths.load_env_config(repo, proj, e, provider=prov)
    cdir = paths.component_dir(repo, proj, e, comp, prov)
    ip = os.path.join(cdir, "inputs.hcl")
    listing = ""
    if os.path.isdir(cdir):
        try:
            listing = " contents=" + repr(sorted(os.listdir(cdir)))
        except OSError as ex:
            listing = " (listdir error: %s)" % ex
    print("  %-24s env=%-12r tm=%-8r | %s/ dir=%-5s inputs.hcl=%-5s%s" % (
        e, cfg.get("environment"), cfg.get("terraform_module"),
        comp, os.path.isdir(cdir), os.path.exists(ip), listing))

print("\nDirect resolve(%s, preprodbilling, %s):" % (proj, comp))
rc = paths.resolve(repo, proj, "preprodbilling", comp, provider=prov)
print("  component_dir:", rc.component_dir)
print("  inputs_hcl   :", rc.inputs_hcl)
print("  component_dir is a dir:", os.path.isdir(rc.component_dir))
print("  inputs_hcl exists     :", os.path.exists(rc.inputs_hcl))

if chosen:
    print("\nDirect resolve via matched env (%s):" % chosen)
    rc2 = paths.resolve(repo, proj, chosen, comp, provider=prov)
    print("  inputs_hcl   :", rc2.inputs_hcl)
    print("  inputs_hcl exists:", os.path.exists(rc2.inputs_hcl))
print("=" * 72)
"""
What to look for:
 - Is 'preprodbilling' present in list_envs? (if not -> list_envs/provider bug)
 - Does its row show inputs.hcl=True with the .hcl files in contents?
 - What is preprodbilling's config 'environment' value? (drives match_env_dir)
 - Does match_env_dir map 'pre-prod' to preprodbilling, or somewhere else?
 - Does the direct resolve() inputs_hcl path exist? If the path is right but
   exists=False, paste the exact path so we can compare to `ls` on disk.
"""
