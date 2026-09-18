"""compose.py - the MVP compose loop (Phase 4).

intent -> resolve paths -> reuse-vs-write decision + typed module schema ->
retrieval grounding -> deterministic edit engine (C) + LLM edit-planner (A) ->
MiniMax generates/splices inputs.hcl -> emit (terragrunt.hcl + inputs.hcl) for
the env-tier component, in the REAL cloud-native-open-foundation convention.

Reconciliation with the repo's existing layer:
  * We REUSE catalog.ModuleCatalog / ModuleEntry for the typed input schema
    (name/required/type/description), resource_types, and reuse_count.
  * We DO NOT use planner.Planner.render_inputs_hcl / scaffold_new_module: those
    emit the generic legacy_coder convention (live/<env>/<project>/<name>/ with an
    inline `terraform { source = "../../.." + key }` + dependency blocks). This
    repo's component terragrunt.hcl is just `include { find_in_parent_folders(
    "root.hcl") }`; root.hcl generates source/provider/backend. So we emit that
    + a generated inputs.hcl instead.

SOURCE vs DESTINATION (P0/P1): the intent separates the DESTINATION (where to
create: project/env/resource_type) from an optional REFERENCE (the source to
model on, from "similar to / like X"). The reference NEVER fills the
destination. When a reference resolves to a real component on disk, P1 reads its
inputs.hcl and feeds it to the generator as the reproduce-base so the output
mirrors real values instead of placeholders.

EDIT ENGINE (P2, A+C): mutations against an existing component are applied by
`_apply_edits`, NOT by dumping every intent scalar as a top-level key.
  * C (deterministic): a requested value is written via hcl_override.set_top_level
    ONLY when the module contract declares it as a top-level SCALAR input, and
    NEVER for operation=='add'. This kills the old bug where `cidr_block` /
    `availability_zone` for a list(object(...)) input were injected as bogus
    top-level scalars.
  * A (LLM edit-planner): everything else -- unknown keys, complex-typed inputs,
    and every 'add' -- is placed by an LLM that returns a typed hcl_edit
    edit-set (e.g. {path: workload_subnets, op: add, value: {...}}), which is
    then applied + verified deterministically via hcl_edit. The planner is fed
    the GROUNDING (raw variables.tf + siblings), NOT the catalog's shallow
    scraped type, because the real list(object({...})) shape lives only in
    variables.tf.

After a VERIFIED edit-set mutation, whole-file regeneration is SKIPPED by
default (splice-only) so real values/ARNs are preserved byte-for-byte. Pass
`regen=True` (CLI `--regen`) to also re-run the LLM for restyling.

Generation is injectable (`generate=` / `generate_json=` callables) so the
deterministic plumbing is testable offline without the gateway.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
from typing import Callable, Dict, List, Optional, Tuple

from terra_pilot.pipeline import bootstrap
from terra_pilot.llm import generator
from terra_pilot.hcl import hcl_edit
from terra_pilot.hcl import hcl_override
from terra_pilot.llm import intent_parser
from terra_pilot.core import paths
from terra_pilot.search import retrieval
from terra_pilot.core.convention import detect_convention
from terra_pilot.pipeline.emitters import get_emitter

GenerateFn = Callable[[List[Dict[str, str]]], str]
GenerateJsonFn = Callable[[List[Dict[str, str]]], Dict]


@dataclasses.dataclass
class ComposeArtifacts:
    component_dir: str
    terragrunt_hcl: str
    inputs_hcl: str
    inputs_file_path: str
    decision: str                 # "reuse" | "net-new"
    module_key: Optional[str]
    terraform_module: str
    notes: List[str]
    grounding: str
    bootstrap: Optional["bootstrap.BootstrapPlan"] = None
    refused: bool = False
    change_applied: Optional[bool] = None   # None = n/a (create); else did the change land?


# We use the emitter's edit prompt now


def _default_generate_json(messages: List[Dict[str, str]]) -> Dict:
    """Edit-planner JSON call."""
    return generator.complete_json(
        messages,
        temperature=0.0, max_tokens=1200,
    )


def _plan_edits_llm(base_hcl: str, values: Dict, grounding: str,
                    operation: str, generate_json: GenerateJsonFn,
                    edit_prompt: str) -> List[Dict]:
    """Option A: LLM places `values` into the right (nested/list) location as a
    typed edit-set, grounded in the real variables.tf schema (via grounding)."""
    user = (
        f"OPERATION: {operation}\n\n"
        f"MODULE GROUNDING (schema + examples):\n{grounding[:4000]}\n\n"
        f"REQUESTED VALUES (JSON): {json.dumps(values)}\n\n"
        f"CURRENT configuration:\n{base_hcl}\n\n"
        "Emit the edits JSON now."
    )
    raw = generate_json([
        {"role": "system", "content": edit_prompt},
        {"role": "user", "content": user},
    ])
    edits = raw.get("edits") if isinstance(raw, dict) else None
    return edits if isinstance(edits, list) else []


def _apply_hcl_edits(base_hcl: str, edits: List[Dict]) -> Tuple[str, List[Dict]]:
    """Call hcl_edit.apply_edits tolerantly (it returns (hcl, results))."""
    out = hcl_edit.apply_edits(base_hcl, edits)
    if isinstance(out, tuple) and len(out) == 2:
        new_hcl, results = out
        return new_hcl, (results if isinstance(results, list) else [])
    # Some builds may return just the string.
    return (out if isinstance(out, str) else base_hcl), []


def _verify_hcl_edits(base_hcl: str, edits: List[Dict]) -> Tuple[bool, List[str]]:
    """Best-effort wrapper over hcl_edit.verify_edits, tolerant of return shape
    (bool | (bool, failures) | dict | list-of-results). Missing/raising verifier
    trusts the apply step rather than blocking a real change."""
    fn = getattr(hcl_edit, "verify_edits", None)
    if not callable(fn):
        return True, []
    try:
        res = fn(base_hcl, edits)
    except Exception as e:  # noqa: BLE001
        return True, [f"verify_edits raised {type(e).__name__}; trusting apply"]
    if isinstance(res, tuple) and len(res) == 2:
        ok, failures = res
        return bool(ok), list(failures or [])
    if isinstance(res, bool):
        return res, []
    if isinstance(res, dict):
        ok = res.get("ok", res.get("verified", True))
        return bool(ok), list(res.get("failures", []) or [])
    if isinstance(res, list):
        fails = [str(r) for r in res if isinstance(r, dict) and r.get("status") == "error"]
        return (not fails), fails
    return True, []


def _apply_edits(base_hcl: str, specifics: Dict, contract: Dict[str, str],
                 grounding: str, resource_type: str, operation: str,
                 edit_prompt: str,
                 generate_json: Optional[GenerateJsonFn] = None,
                 ) -> Tuple[str, List[str], bool]:
    """C + A edit engine. Returns (new_hcl, applied_notes, ok).

    C: deterministic set_top_level ONLY for keys the contract declares as
       top-level scalars, and never for an 'add'.
    A: everything else (unknown keys, complex-typed inputs, any 'add') is placed
       by the LLM edit-planner and applied + verified deterministically.

    ok=False signals the caller to fall back to whole-file regeneration.
    """
    if not base_hcl or not specifics:
        return base_hcl, [], True

    applied: List[str] = []
    specifics = dict(specifics)  # never mutate caller's dict

    # No hardcoded name->field remap. If the request's logical 'name' is itself a
    # top-level scalar in the contract, the C path sets it directly; otherwise
    # (e.g. it belongs inside a nested list(object(...)) element) it falls to the
    # LLM edit-planner, which maps it onto the correct schema field using the
    # grounding (variables.tf) rather than a guessed field name.

    def _is_scalar(v) -> bool:
        return isinstance(v, (bool, int, float, str))

    top_edits: Dict[str, object] = {}
    residual: Dict[str, object] = {}
    for k, v in specifics.items():
        t = (contract.get(k) or "").lower()
        is_top_scalar = (k in contract) and not t.startswith(_COMPLEX_TYPES)
        if operation != "add" and _is_scalar(v) and "." not in k \
                and "[" not in k and is_top_scalar:
            top_edits[k] = v
        else:
            residual[k] = v   # unknown key, complex-typed input, or an 'add'

    ok = True

    # ---- C: deterministic top-level scalar sets ---------------------------
    if top_edits:
        base_hcl, result = hcl_override.set_top_level(base_hcl, top_edits)
        applied.extend(f"{k}: {v} ({(result or {}).get(k, 'set')})"
                       for k, v in top_edits.items())

    # ---- A: LLM edit-planner for the residual -----------------------------
    if residual:
        gen_json = generate_json or _default_generate_json
        try:
            edits = _plan_edits_llm(base_hcl, residual, grounding, operation, gen_json, edit_prompt)
        except Exception as e:  # noqa: BLE001 - surface as a clean fallback
            return base_hcl, applied + [f"edit-planner failed ({type(e).__name__})"], False
        if not edits:
            return base_hcl, applied + ["no applicable edits for: "
                                       + ", ".join(residual)], False
        # SCOPE GUARD (deterministic): an 'add' may ONLY append. Drop any
        # 'set'/'remove'/rename the planner returned so a bare logical 'name'
        # can never be misapplied to an existing top-level input (e.g. the
        # planner trying to set cluster_name). Prompt guidance is not trusted
        # to be reliable; this filter is.
        if operation == "add":
            scoped, dropped = [], []
            for e in edits:
                if isinstance(e, dict) and e.get("op") == "add":
                    scoped.append(e)
                else:
                    dropped.append(e)
            if dropped:
                applied.append(
                    "scope guard: dropped non-append edit(s) on an 'add' "
                    "(request only adds an element): "
                    + ", ".join(f"{e.get('op')} {e.get('path')}"
                                for e in dropped if isinstance(e, dict)))
            edits = scoped
            if not edits:
                return base_hcl, applied + [
                    "no append edit produced for 'add'"], False
        base_hcl, results = _apply_hcl_edits(base_hcl, edits)
        errs = [r for r in results if isinstance(r, dict) and r.get("status") == "error"]
        verified, failures = _verify_hcl_edits(base_hcl, edits)
        ok = (not errs) and verified
        applied.append("planned edits: " + ", ".join(
            f"{r.get('op')} {r.get('path')} [{r.get('status', 'ok')}]"
            for r in results) if results else "planned edits: (applied)")
        if failures:
            applied.append("verify failures: " + "; ".join(failures))

    return base_hcl, applied, ok


# Prompt building and finalization logic moved to EmitterStrategy


def _brace_balance(t: str) -> int:
    return t.count("{") - t.count("}")


def _pick_env_from_hits(candidates: List[str], hits: List[Dict]) -> Optional[str]:
    """Disambiguate multiple env-dir candidates using retrieval hit file paths:
    return the first candidate whose dir segment appears in a ranked hit's file."""
    for h in (hits or []):
        f = str(h.get("file", "")).replace("\\", "/")
        for c in candidates:
            if ("/" + c + "/") in f or f.endswith("/" + c):
                return c
    return None


def _resolve_env(repo: str, project: str, stated_env: Optional[str], provider: str,
                 hits: List[Dict], notes: List[str], role: str) -> Optional[str]:
    """Reconcile a stated env word to a real env-tier directory (shared by the
    DESTINATION and the REFERENCE). Normalizes 'pre-prod' -> 'preprod', matches
    on the config.yml `environment` field and dir basenames, and disambiguates
    multiple matches via the retrieval hits when possible. Returns the resolved
    dir, or the original word when nothing matches (so the destination still
    composes net-new at aws/<env>/)."""
    if not stated_env:
        return stated_env
    envs = paths.list_envs(repo, project, provider=provider)
    if stated_env in envs or not envs:
        return stated_env
    chosen, candidates = paths.match_env_dir(repo, project, stated_env, provider=provider)
    if chosen:
        notes.append(f"{role} env '{stated_env}' has no directory; matched env-tier "
                     f"'{chosen}'")
        return chosen
    if candidates:
        picked = _pick_env_from_hits(candidates, hits)
        how = "via retrieval top-hit" if picked else "first candidate"
        picked = picked or candidates[0]
        notes.append(f"{role} env '{stated_env}' ambiguous among {candidates}; "
                     f"using '{picked}' ({how})")
        return picked
    notes.append(f"{role} env '{stated_env}' not found under {project}/{provider} "
                 f"(available: {', '.join(envs)})")
    return stated_env


def _component_dir_from_hit(repo: str, file: object, ref_proj: str, ref_prov: str,
                            ref_comp: str) -> Optional[str]:
    """From a retrieval hit's file path, reconstruct the reference component dir
    infra/<ref_proj>/<ref_prov>/<env>/<ref_comp> when the hit lives under it."""
    f = str(file or "").replace("\\", "/")
    if not f:
        return None
    parts = f.split("/")
    for idx in range(3, len(parts)):
        if (parts[idx] == ref_comp and parts[idx - 2] == ref_prov
                and parts[idx - 3] == ref_proj):
            sub = "/".join(parts[:idx + 1])
            return sub if os.path.isabs(sub) else os.path.join(repo, sub)
    return None


def _resolve_reference(repo: str, reference: Dict[str, object], dest_project: str,
                       resource_type: str, provider: str, hits: List[Dict],
                       notes: List[str]):
    """Locate the REFERENCE component's real inputs.hcl for use as the reproduce-base.
    Component-existence aware (this is the key fix): the requested env word only
    wins when that env actually CONTAINS the component. Priority:
      1. requested env word, if that env has <component>/inputs.hcl,
      2. the closest retrieval precedent (top-ranked hit under <proj>/<comp>),
      3. any env under the project that has <component>/inputs.hcl.
    Returns (inputs_text, label) or (None, None). The reference NEVER affects the
    destination; it only grounds generation.
    """
    ref_proj = str(reference.get("project") or dest_project)
    ref_comp = str(reference.get("component") or resource_type)
    ref_prov = str(reference.get("provider") or provider)
    ref_env_word = reference.get("env")

    def _read(cdir: str, how: str):
        ip = os.path.join(cdir, "inputs.hcl")
        if not os.path.exists(ip):
            return None, None
        label = os.path.relpath(cdir, repo)
        notes.append(f"reference resolved ({how}): {label} -- using its inputs.hcl "
                     f"as the reproduce-base")
        return open(ip, encoding="utf-8", errors="replace").read(), label

    envs = paths.list_envs(repo, ref_proj, provider=ref_prov)
    have_comp = [e for e in envs if os.path.exists(os.path.join(
        paths.component_dir(repo, ref_proj, e, ref_comp, ref_prov), "inputs.hcl"))]

    # 1) Requested env word, ONLY when env(s) matching it actually have the
    #    component. If several matches have it, disambiguate via the retrieval
    #    hit (e.g. preprod vs preprod-DR), else take the first.
    if ref_env_word:
        chosen, cands = paths.match_env_dir(repo, ref_proj, ref_env_word, provider=ref_prov)
        ordered = ([chosen] if chosen else []) + [c for c in (cands or []) if c != chosen]
        word_envs = [e for e in ordered if e in have_comp]
        if word_envs:
            pick = _pick_env_from_hits(word_envs, hits) or word_envs[0]
            return _read(paths.component_dir(repo, ref_proj, pick, ref_comp, ref_prov),
                         f"env '{ref_env_word}'")

    # 2) Retrieval hit-driven: the closest precedent the search already found.
    for h in retrieval._primary_hits(hits):
        cdir = _component_dir_from_hit(repo, h.get("file"), ref_proj, ref_prov, ref_comp)
        if cdir and os.path.exists(os.path.join(cdir, "inputs.hcl")):
            how = "retrieval hit"
            if ref_env_word:
                how = (f"retrieval hit; requested env '{ref_env_word}' has no "
                       f"'{ref_comp}' component, using closest precedent")
            return _read(cdir, how)

    # 3) Any env under the project that has the component.
    if have_comp:
        return _read(
            paths.component_dir(repo, ref_proj, have_comp[0], ref_comp, ref_prov),
            f"only/first env with '{ref_comp}'")

    notes.append(f"reference {ref_proj}/{ref_comp}: no inputs.hcl found in any of "
                 f"{len(envs)} env dirs; falling back to retrieval grounding only")
    return None, None


def _find_entry(cat, component: str, terraform_module: str):
    """Best-effort: the env-tier module catalog.ModuleEntry for this component."""
    modules = getattr(cat, "modules", {}) or {}
    suffixes = (f"/infrastructure/{terraform_module}/{component}",
                f"infrastructure/{terraform_module}/{component}")
    for key, m in modules.items():
        if str(key).replace("\\", "/").rstrip("/").endswith(suffixes):
            return m
    for key, m in modules.items():
        if getattr(m, "name", "") == component and "infrastructure" in str(key):
            return m
    try:
        cand = cat.match(component, top=1)
        if cand:
            return cand[0]
    except Exception:
        pass
    return None


def _entry_schema_text(m) -> str:
    lines = [f"MODULE: {getattr(m, 'key', '?')} "
             f"(reuse_count={getattr(m, 'reuse_count', 0)}, "
             f"resource_types={getattr(m, 'resource_types', [])})",
             "INPUT CONTRACT:"]
    for i in getattr(m, "required_inputs", []):
        lines.append(f"  [required] {i.name}: {getattr(i, 'type', '') or '?'}"
                     + (f"  // {i.description}" if getattr(i, 'description', '') else ""))
    for i in getattr(m, "optional_inputs", []):
        lines.append(f"  [optional] {i.name}: {getattr(i, 'type', '') or '?'}"
                     + (f"  // {i.description}" if getattr(i, 'description', '') else ""))
    return "\n".join(lines)


def _entry_contract(m) -> Dict[str, str]:
    """name -> shallow scraped type for the edit engine. The REAL complex shape
    (list(object({...}))) is NOT here -- it rides in grounding (variables.tf),
    which is what the edit-planner actually reads."""
    out: Dict[str, str] = {}
    for i in list(getattr(m, "required_inputs", [])) + list(getattr(m, "optional_inputs", [])):
        out[i.name] = getattr(i, "type", "") or ""
    return out


def _residual_source_tokens(text: str, tokens) -> Dict[str, List[str]]:
    """Map each SOURCE token (>=4 chars) to the inputs.hcl lines that still
    contain it, so leaked source identifiers can be reviewed before apply."""
    hits: Dict[str, List[str]] = {}
    lines = (text or "").splitlines()
    for tok in tokens:
        t = str(tok or "").strip().lower()
        if len(t) < 4:
            continue
        for ln in lines:
            if t in ln.lower():
                hits.setdefault(t, []).append(ln.strip())
    return hits


def _verify_requested_change(new: str, old: str, specifics) -> bool:
    """Heuristic P0 check: did the requested mutation actually alter inputs.hcl?
    True when the file changed AND every concrete requested value appears in the
    new text."""
    if new == old:
        return False
    tokens = []
    if isinstance(specifics, dict):
        tokens = [str(v) for v in specifics.values() if v]
    elif isinstance(specifics, (list, tuple)):
        tokens = [str(v) for v in specifics if v]
    elif specifics:
        tokens = [str(specifics)]
    return all(tok in new for tok in tokens) if tokens else (new != old)


def compose(repo: str, *, resource_type: str, project: str, env: str,
            specifics: Optional[Dict[str, object]] = None,
            terraform_module: Optional[str] = None,
            provider: str = paths.DEFAULT_PROVIDER,
            generate: Optional[GenerateFn] = None,
            generate_json: Optional[GenerateJsonFn] = None,
            query: Optional[str] = None,
            reference: Optional[Dict[str, object]] = None,
            operation: str = "create",
            regen: bool = False,
            extra_notes: Optional[List[str]] = None,
            plan_only: bool = False) -> ComposeArtifacts:
    specifics = specifics or {}
    change_applied: Optional[bool] = None

    # Reconcile the stated DESTINATION env against real env-tier directories. The
    # path segment after aws/ is the env-tier INSTANCE name, which may differ
    # from the AWS environment word the user said (e.g. "preprod" vs the dir
    # "authpreprod"). Done before retrieval; destination disambiguation cannot
    # use hits (the hits target the source/reference), so ties pick deterministically.
    path_notes: List[str] = list(extra_notes or [])
    stated_env = env
    env = _resolve_env(repo, project, env, provider, [], path_notes, "destination")

    convention = detect_convention(repo)
    emitter = get_emitter(convention.kind)

    rc = paths.resolve_generic(repo, project, env, resource_type, provider=provider,
                               terraform_module=terraform_module)
    tm = rc.terraform_module or "env"

    # --- P0 GUARDRAIL 1: never create net-new under a mutation verb. An
    # update/add/modify/delete against a component that does not exist yet must
    # NOT silently scaffold a new one -- refuse with a clear reason instead.
    if operation != "create":
        _early_inputs = rc.inputs_hcl
        if not os.path.exists(_early_inputs):
            note = ("REFUSED: component %r does not exist in %s/%s; an '%s' "
                    "request will not create it" % (resource_type, project, env, operation))
            return ComposeArtifacts(
                component_dir=rc.component_dir,
                terragrunt_hcl=paths.standard_terragrunt_hcl(repo),
                inputs_hcl="",
                decision="refused",
                module_key=None,
                terraform_module=tm,
                notes=path_notes + [note],
                grounding="",
                bootstrap=None,
                refused=True,
            )

    # P2 bootstrap: scaffold the missing project/env Terragrunt files (common.hcl,
    # config.yml) when the destination doesn't exist yet. plan() returns an empty
    # plan (no files) for an already-set-up destination, so validated flows are
    # completely unaffected. `stated_env` is the word the user said (the AWS
    # `environment`), captured before env-tier-dir reconciliation.
    bstrap = bootstrap.plan(repo, project, env, provider, environment=stated_env)
    path_notes.extend(bstrap.notes)
    if not bstrap.files:
        bstrap = None

    # Query for hybrid semantic retrieval: prefer the caller's NL text, else
    # synthesize one from the resource type + requested specifics + the reference
    # (so even the explicit/flag path targets the named precedent).
    if query:
        q = query
    else:
        q_terms = [resource_type] + [str(v) for v in (specifics or {}).values() if v]
        if reference:
            q_terms += [str(reference.get(k)) for k in ("component", "env", "project")
                        if reference.get(k)]
        q = " ".join(q_terms)

    bundle = retrieval.gather(repo, resource_type=resource_type, project=project,
                              env=env, terraform_module=tm, provider=provider,
                              query=q)
    notes = path_notes + list(bundle.notes)

    # P1.1: resolve the REFERENCE (source to model on) to a real component's
    # inputs.hcl and use it as the reproduce-base. Component-existence aware:
    # prefers the requested env when it actually has the component, else the
    # closest retrieval precedent, else any env that has it. The reference NEVER
    # changes the destination; it only grounds generation.
    reference_inputs: Optional[str] = None
    reference_label: Optional[str] = None
    if reference:
        reference_inputs, reference_label = _resolve_reference(
            repo, reference, project, resource_type, provider,
            getattr(bundle, "retrieved", []), notes)
    else:
        # No explicit 'similar to' reference -- but if retrieval found real
        # sibling inputs.hcl hits for this resource_type, auto-ground on the
        # top-ranked one so the generator mirrors real conventions instead
        # of inventing 'newenv' / 'sample-*' placeholders.
        for h in retrieval._primary_hits(getattr(bundle, "retrieved", [])):
            f = str(h.get("file", "")).replace("\\", "/")
            if not f.endswith("/inputs.hcl"):
                continue
            parts = f.split("/")
            # infra/<proj>/<prov>/<env>/<comp>/inputs.hcl -> comp is parts[-2]
            if len(parts) >= 5 and parts[-2] == resource_type:
                cdir = os.path.join(repo, "/".join(parts[:-1]))
                if os.path.exists(os.path.join(cdir, "inputs.hcl")):
                    reference_inputs = open(
                        os.path.join(cdir, "inputs.hcl"),
                        encoding="utf-8", errors="replace").read()
                    reference_label = "/".join(parts[:-1])
                    notes.append(
                        f"auto-grounding (no explicit reference): top retrieval "
                        f"hit {reference_label} -- using its inputs.hcl as the "
                        f"reproduce-base")
                    break

    req_names = list(bundle.module.required_inputs) if bundle.module else []
    opt_names = list(bundle.module.optional_inputs) if bundle.module else []
    module_key = bundle.module.key if bundle.module else None

    # Input contract (name -> shallow type) for the edit engine. Seed from the
    # bundle's module names; upgrade to the typed catalog contract below.
    contract: Dict[str, str] = {n: "" for n in list(req_names) + list(opt_names)}

    # Prefer the typed catalog schema when the index is available.
    grounding = retrieval.to_prompt_context(bundle)
    try:
        from terra_pilot.search.catalog import ModuleCatalog
        from terra_pilot.search.index import get_index
        entry = _find_entry(ModuleCatalog(get_index(repo)), resource_type, tm)
        if entry is not None:
            req_names = [i.name for i in getattr(entry, "required_inputs", [])]
            opt_names = [i.name for i in getattr(entry, "optional_inputs", [])]
            module_key = getattr(entry, "key", module_key)
            contract = _entry_contract(entry)
            grounding = _entry_schema_text(entry) + "\n\n" + grounding
    except Exception as e:
        notes.append(f"typed catalog schema unavailable ({type(e).__name__}); "
                     f"used path/variables.tf schema")

    inputs_path = rc.inputs_hcl
    existing = os.path.exists(inputs_path)

    if plan_only:
        # Fast reuse-audit path: decision + module mapping are deterministic and
        # computed above; skip the (slow) LLM generation entirely.
        inputs_hcl = ""
        notes.append("plan-only: skipped LLM generation (decision + module mapping only)")
    else:
        # HYBRID: if the component already exists, feed its real inputs.hcl in as the
        # base to reproduce/extend; otherwise, if a reference resolved, reproduce
        # that; otherwise scaffold a fresh component.
        existing_inputs = None
        base_before = None   # the pre-edit original, for change verification
        if existing:
            existing_inputs = open(inputs_path, encoding="utf-8", errors="replace").read()
            base_before = existing_inputs
            notes.append("hybrid: existing component - reproducing current inputs.hcl, "
                         "applying only requested changes")

        # P2 (A+C): apply the edit engine BEFORE any LLM step. C sets declared
        # top-level scalars deterministically; A routes complex/unknown/add
        # values through the LLM edit-planner -> hcl_edit (verified).
        applied_edits: List[str] = []
        edits_ok = True
        if existing_inputs and specifics:
            edited_inputs, applied_edits, edits_ok = _apply_edits(
                existing_inputs, specifics, contract, grounding,
                resource_type, operation, emitter.edit_prompt, generate_json=generate_json)
            existing_inputs = edited_inputs
            notes.append("P2 edits: " + "; ".join(applied_edits or ["(none)"]))
            if not edits_ok:
                notes.append("P2 edit engine incomplete -> falling back to whole-file "
                             "regeneration")

        # P1.2: concrete DESTINATION identifiers so the model renames the source
        # env's identifiers (e.g. cluster_name/env_name/bucket_name) to the real
        # destination env-tier instead of inventing a placeholder like "newenv".
        dest_cfg = paths.load_env_config(repo, project, env, provider=provider)
        dest_ctx = {"project": project, "env_tier": env,
                    "environment": dest_cfg.get("environment"),
                    "region": dest_cfg.get("region"),
                    "account_id": (dest_cfg.get("account_id")
                                   or dest_cfg.get("aws_account_id")
                                   or dest_cfg.get("account"))}
        source_ctx = None
        if reference_label and not existing:
            rl = str(reference_label).replace("\\", "/").split("/")
            if len(rl) >= 5:   # infra/<proj>/<prov>/<env>/<comp>
                s_proj, s_prov, s_env_tier = rl[1], rl[2], rl[3]
                s_cfg = paths.load_env_config(repo, s_proj, s_env_tier, provider=s_prov)
                source_ctx = {"project": s_proj, "env_tier": s_env_tier,
                              "environment": s_cfg.get("environment")}

        # SPLICE-vs-REGEN decision. For an existing component whose requested
        # mutation was fully applied + verified by the edit engine, we WRITE THE
        # SPLICED FILE and SKIP whole-file regeneration by default -- this is the
        # fix for "the LLM rewrote the whole file / dropped real values". Pass
        # regen=True (CLI --regen) to also run the LLM for restyling.
        splice = (operation != "create" and existing and bool(specifics)
                  and edits_ok and not regen)

        if splice:
            inputs_hcl = emitter.finalize_generation(existing_inputs)
            change_applied = _verify_requested_change(
                inputs_hcl, base_before or "", specifics)
            notes.append("mutation applied via typed edit-set; skipped whole-file "
                         "regeneration (use --regen to also restyle via the LLM)")
            if not change_applied:
                notes.append("# CHANGE NOT APPLIED: requested %s did not alter inputs.hcl"
                             % operation)
        else:
            gen = generate or _default_generate
            messages = emitter.build_llm_prompt(resource_type, specifics, grounding, req_names, opt_names,
                                      decision=bundle.decision, existing=existing,
                                      existing_inputs=existing_inputs,
                                      reference_inputs=(None if existing else reference_inputs),
                                      reference_label=reference_label,
                                      dest_ctx=dest_ctx, source_ctx=source_ctx,
                                      repo=repo)
            inputs_hcl = emitter.finalize_generation(gen(messages))
            bal = _brace_balance(inputs_hcl)
            if bal != 0:
                notes.append(f"WARNING: inputs.hcl brace balance off by {bal}; review before apply")

            # P1.2: flag SOURCE env identifiers that leaked through the rename (e.g. a
            # container/port name still embedding the reference project/env). Some
            # hits are intentional (ECR image registry paths, cross-account ARNs); we
            # surface line numbers so they can be eyeballed before --apply.
            if source_ctx and not existing:
                resid = _residual_source_tokens(
                    inputs_hcl, [source_ctx.get("project"), source_ctx.get("env_tier"),
                                 source_ctx.get("environment")])
                if resid:
                    total = sum(len(v) for v in resid.values())
                    notes.append(
                        f"REVIEW: {total} line(s) still mention SOURCE identifier(s) "
                        f"{sorted(resid)} -- rename env-specific ones (e.g. container/port "
                        f"names); some may be intentional (ECR image paths, cross-account ARNs)")

            # --- P0 GUARDRAIL 2: verify the requested change actually applied. A
            # mutation whose generated file is unchanged (or is missing the
            # requested values) is flagged so write_to_tree refuses -- a silent
            # no-op "success" is worse than an explicit error. Compare against the
            # PRE-EDIT original so an edit that only the engine applied still counts.
            if operation != "create":
                change_applied = _verify_requested_change(
                    inputs_hcl, base_before if base_before is not None else (existing_inputs or ""),
                    specifics)
                if not change_applied:
                    notes.append("# CHANGE NOT APPLIED: requested %s did not alter inputs.hcl"
                                 % operation)

    if bundle.decision == "net-new":
        notes.append("DECISION net-new: no reusable module found; a net-new leaf "
                     "module likely needs to be written before this inputs.hcl is valid")

    return ComposeArtifacts(
        component_dir=rc.component_dir,
        terragrunt_hcl=paths.standard_terragrunt_hcl(repo) if convention.kind == "terragrunt" else "",
        inputs_hcl=inputs_hcl,
        inputs_file_path=rc.inputs_hcl,
        decision=bundle.decision,
        module_key=module_key,
        terraform_module=tm,
        notes=notes,
        grounding=grounding,
        bootstrap=bstrap,
        change_applied=change_applied,
    )


def from_text(repo: str, text: str, *, provider: str = paths.DEFAULT_PROVIDER,
              generate: Optional[GenerateFn] = None, regen: bool = False):
    """Parse NL -> intent -> compose. Returns (artifacts, intent)."""
    intent = intent_parser.parse_intent(text)
    missing = intent_parser.missing_fields(intent)
    reference = intent.get("reference")
    if missing:
        msg = ("intent missing required destination fields: " + ", ".join(missing)
               + " -- ask the user or infer from the repo")
        if isinstance(reference, dict) and any(reference.get(k) for k in
                                               ("project", "env", "component")):
            ref_str = "/".join(str(reference.get(k) or "?") for k in
                               ("project", "env", "component"))
            msg += (f" (note: '{ref_str}' is the REFERENCE to model on, not where to "
                    f"create it -- specify the destination " + ", ".join(missing) + ")")
        raise ValueError(msg)

    # Fold the parsed logical name into specifics so the generator actually uses it
    # (otherwise the requested resource name is silently dropped).
    specifics = dict(intent.get("specifics") or {})
    if intent.get("name") and "name" not in specifics:
        specifics["name"] = intent["name"]

    operation = intent.get("operation", "create")
    resource_type = str(intent["resource_type"])
    reconcile_notes: List[str] = []
    if operation != "create":
        resource_type = _reconcile_component_type(
            repo, text, resource_type, provider, reconcile_notes)

    arts = compose(repo, resource_type=resource_type,
                   project=str(intent["project"]), env=str(intent["env"]),
                   specifics=specifics, provider=provider,
                   generate=generate, query=text,
                   operation=operation, regen=regen,
                   extra_notes=reconcile_notes,
                   reference=reference if isinstance(reference, dict) else None)
    return arts, intent


def detect_resource_types(repo: str, text: str, primary: str,
                          provider: str = paths.DEFAULT_PROVIDER) -> List[str]:
    """Find every repo component type named in the request (fan-out), ordered by
    first appearance. The repo's real infrastructure component dirs are the
    vocabulary; `primary` (the parser's resource_type) is always included. A
    single-type request returns just [primary]."""
    vocab = set(paths.list_all_components(repo, provider))
    if primary:
        vocab.add(primary)
    low = " " + (text or "").lower() + " "
    found = []
    for comp in vocab:
        if not comp:
            continue
        # whole-token match; allow hyphen/space interchange (security-group).
        pat = (r"(?<![a-z0-9])" + re.escape(comp).replace(r"\-", r"[-\s]")
               + r"(?![a-z0-9])")
        m = re.search(pat, low)
        if m:
            found.append((m.start(), comp))
    found.sort()
    ordered = [c for _, c in found]
    if primary and primary not in ordered:
        ordered.insert(0, primary)
    seen = set()
    out = []
    for c in ordered:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _reconcile_component_type(repo: str, text: str, primary: str,
                             provider: str, notes: List[str]) -> str:
    """A mutation's resource_type must name a REAL repo component. The intent
    parser sometimes extracts a sub-element noun (e.g. 'subnet') that is not a
    component on its own -- subnets are a nested `workload_subnets` input inside
    the vpc/network component. When the parsed type is not a real component,
    remap it to the real component actually NAMED in the request. Data-driven:
    the repo's component dirs are the only vocabulary; no hardcoded synonym map.
    Returns the (possibly remapped) component; leaves it unchanged when it is
    already real or when nothing better is named (GUARDRAIL 1 then refuses).
    """
    try:
        real = set(paths.list_all_components(repo, provider))
    except Exception as e:  # noqa: BLE001
        notes.append(f"component reconcile skipped ({type(e).__name__})")
        return primary
    if primary in real:
        return primary
    named = [c for c in detect_resource_types(repo, text, primary, provider)
             if c in real and c != primary]
    if named:
        chosen = named[0]
        extra = f"; other candidates: {named[1:]}" if len(named) > 1 else ""
        notes.append(f"resource_type '{primary}' is not a component in this repo; "
                     f"remapped to '{chosen}' (named in the request){extra}")
        return chosen
    notes.append(f"resource_type '{primary}' is not a component in this repo and no "
                 f"real component was named in the request; not guessing "
                 f"(available: {', '.join(sorted(real))})")
    return primary


def from_text_multi(repo: str, text: str, *, provider: str = paths.DEFAULT_PROVIDER,
                    generate: Optional[GenerateFn] = None, regen: bool = False):
    """Parse NL -> intent, then compose EACH requested component (fan-out).
    Returns (results, intent) where results is a list of (resource_type,
    ComposeArtifacts). The shared destination (project/env) and reference apply
    to every component; for >1 component the reference's `component` is
    retargeted to each type so each is modeled on the same env's same-type
    precedent. A single-component request behaves exactly like from_text."""
    intent = intent_parser.parse_intent(text)
    missing = intent_parser.missing_fields(intent)
    reference = intent.get("reference")
    if missing:
        msg = ("intent missing required destination fields: " + ", ".join(missing)
               + " -- ask the user or infer from the repo")
        if isinstance(reference, dict) and any(reference.get(k) for k in
                                               ("project", "env", "component")):
            ref_str = "/".join(str(reference.get(k) or "?") for k in
                               ("project", "env", "component"))
            msg += (f" (note: '{ref_str}' is the REFERENCE to model on, not where to "
                    f"create it -- specify the destination " + ", ".join(missing) + ")")
        raise ValueError(msg)

    # P0 GUARDRAIL 3: an under-specified mutation must NOT be guessed at or
    # fanned out -- stop before generating anything (even under --force).
    if intent_parser.notes_flag_ambiguity(intent):
        raise ValueError("ambiguous mutation: refusing to guess a target "
                         "(component/field unresolved) -- specify what to change")

    operation = intent.get("operation", "create")
    primary = str(intent["resource_type"])
    reconcile_notes: List[str] = []
    # P0: mutations act on ONE existing component; never fan out to sibling types.
    if operation != "create":
        primary = _reconcile_component_type(repo, text, primary, provider, reconcile_notes)
        rtypes = [primary]
    else:
        rtypes = detect_resource_types(repo, text, primary, provider)

    base_specifics = dict(intent.get("specifics") or {})
    if intent.get("name") and "name" not in base_specifics:
        base_specifics["name"] = intent["name"]

    project = str(intent["project"])
    env = str(intent["env"])
    results = []
    for rt in rtypes:
        ref = reference if isinstance(reference, dict) else None
        if ref is not None and len(rtypes) > 1:
            ref = dict(ref)
            ref["component"] = rt
        # name/specifics are type-specific: apply them only to the primary type.
        specifics = base_specifics if rt == primary else {}
        arts = compose(repo, resource_type=rt, project=project, env=env,
                       specifics=specifics, provider=provider,
                       generate=generate, query=text, reference=ref,
                       operation=operation, regen=regen,
                       extra_notes=reconcile_notes)
        results.append((rt, arts))
    return results, intent


def diff_against_disk(a: ComposeArtifacts) -> str:
    import difflib
    out: List[str] = []
    for fname, new in (("terragrunt.hcl", a.terragrunt_hcl), (os.path.basename(a.inputs_file_path), a.inputs_hcl)):
        if fname == "terragrunt.hcl" and not new:
            continue
        path = os.path.join(a.component_dir, fname)
        old = ""
        if os.path.exists(path):
            old = open(path, encoding="utf-8", errors="replace").read()
        d = "".join(difflib.unified_diff(
            old.splitlines(True), new.splitlines(True),
            fromfile=f"a/{fname}", tofile=f"b/{fname}"))
        out.append(d if d else f"(no change) {fname}")
    return "\n".join(out)


def write_to_tree(a: ComposeArtifacts, *, overwrite: bool = False) -> List[str]:
    # P0 SAFETY: fail closed on a refused compose or a mutation whose requested
    # change did not land. --force only governs overwrite; it never bypasses
    # these correctness stops.
    if a.refused:
        raise ValueError("refusing to write: " + "; ".join(a.notes))
    if a.change_applied is False:
        raise ValueError("refusing to write: requested change did not apply -- "
                         + "; ".join(n for n in a.notes if "CHANGE NOT APPLIED" in n))

    # SAFETY: never write a structurally broken inputs.hcl. An unbalanced brace
    # count almost always means the generation was truncated (hit max_tokens);
    # writing it - even with --force - would clobber a real file with a partial
    # one. Refuse instead.
    bal = _brace_balance(a.inputs_hcl)
    if bal != 0:
        raise ValueError(
            f"refusing to write: inputs.hcl brace balance off by {bal} "
            f"(generation likely truncated). Increase max_tokens or review -- not writing.")

    written: List[str] = []
    # P2: write scaffolded project/env files first; NEVER clobber an existing one.
    if a.bootstrap:
        for bf in a.bootstrap.files:
            os.makedirs(os.path.dirname(bf.path), exist_ok=True)
            if os.path.exists(bf.path):
                continue
            open(bf.path, "w", encoding="utf-8").write(bf.text)
            written.append(bf.path)

    os.makedirs(a.component_dir, exist_ok=True)
    if a.terragrunt_hcl:
        tg = os.path.join(a.component_dir, "terragrunt.hcl")
        if not os.path.exists(tg):
            open(tg, "w", encoding="utf-8").write(a.terragrunt_hcl)
            written.append(tg)
            
    ip = a.inputs_file_path
    if os.path.exists(ip) and not overwrite:
        raise FileExistsError(f"{ip} exists; pass overwrite=True (or --apply --force) to replace")
    open(ip, "w", encoding="utf-8").write(a.inputs_hcl)
    written.append(ip)
    return written


def _print_bootstrap(a: ComposeArtifacts, seen: Optional[set] = None) -> None:
    """Print scaffolded project/env files (P2 bootstrap), deduped across a
    multi-component fan-out via the shared `seen` set."""
    if not a.bootstrap or not a.bootstrap.files:
        return
    seen = seen if seen is not None else set()
    fresh = [bf for bf in a.bootstrap.files if bf.path not in seen]
    if not fresh:
        return
    print("\n# ===== bootstrap: scaffolded project/env files (review before apply) =====")
    for bf in fresh:
        seen.add(bf.path)
        modeled = f"  (modeled on {bf.modeled_on})" if bf.modeled_on else ""
        print(f"# --- {bf.path}{modeled} ---")
        print(bf.text)


def _print_artifacts(a: ComposeArtifacts) -> None:
    print(f"# component_dir : {a.component_dir}")
    print(f"# decision      : {a.decision}")
    print(f"# module        : {a.module_key}  (terraform_module={a.terraform_module})")
    if a.notes:
        print("# notes:")
        for n in a.notes:
            print(f"#   - {n}")
    print("\n# ===== terragrunt.hcl =====")
    print(a.terragrunt_hcl)
    print("# ===== inputs.hcl =====")
    print(a.inputs_hcl)


def run_cli(repo: str, rest: List[str]) -> int:
    """`cli.py <repo> compose ...` entry. Two forms:
      - explicit: --resource-type R --project P --env E [--provider P] [--like "..."]
      - natural language: free-text intent tokens
    Flags: --apply (write; default dry-run), --force (overwrite inputs.hcl),
    --regen (after a verified edit-set mutation, ALSO re-run whole-file LLM
    generation to restyle; default: skip / splice-only), --like "<text>"
    (retrieval hint on the explicit path -- the "similar to X" precedent).
    Returns a process exit code (0 ok, 1 usage/failure).
    """
    val_flags = {"--resource-type": None, "--project": None,
                 "--env": None, "--provider": None, "--like": None}
    do_apply = do_force = do_plan_only = do_regen = False
    intent_tokens: List[str] = []
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok in val_flags:
            if i + 1 >= len(rest):
                print(f"# error: {tok} needs a value")
                return 1
            val_flags[tok] = rest[i + 1]
            i += 2
            continue
        if tok == "--apply":
            do_apply = True
        elif tok == "--force":
            do_force = True
        elif tok == "--plan-only":
            do_plan_only = True
        elif tok == "--regen":
            do_regen = True
        else:
            intent_tokens.append(tok)
        i += 1

    intent_text = " ".join(intent_tokens).strip()
    rtype = val_flags["--resource-type"]
    project = val_flags["--project"]
    env = val_flags["--env"]
    provider = val_flags["--provider"] or paths.DEFAULT_PROVIDER
    like = val_flags["--like"]

    intent = None
    try:
        if rtype and project and env:
            arts = compose(repo, resource_type=rtype, project=project,
                           env=env, provider=provider, query=like,
                           regen=do_regen, plan_only=do_plan_only)
            results = [(rtype, arts)]
        elif intent_text:
            results, intent = from_text_multi(repo, intent_text, provider=provider,
                                              regen=do_regen)
        else:
            print('usage: cli.py <repo> compose "<intent...>"   '
                  '(or --resource-type R --project P --env E [--like "..."]) '
                  '[--plan-only] [--apply] [--force] [--regen]')
            return 1
    except Exception as e:
        print(f"# compose failed: {type(e).__name__}: {e}")
        return 1

    if intent is not None:
        print("# intent: " + json.dumps(intent))
    if len(results) > 1:
        print("# fan-out: " + str(len(results)) + " components -> "
              + ", ".join(rt for rt, _ in results))

    if do_plan_only:
        for rt, arts in results:
            if len(results) > 1:
                print(f"\n# ===== component: {rt} =====")
            print(f"# component_dir : {arts.component_dir}")
            print(f"# decision      : {arts.decision}")
            print(f"# module        : {arts.module_key}  (terraform_module={arts.terraform_module})")
            for n in arts.notes:
                print(f"#   - {n}")
        return 0

    boot_seen: set = set()
    for rt, arts in results:
        if len(results) > 1:
            print(f"\n# ========== component: {rt} ==========")
        _print_bootstrap(arts, seen=boot_seen)
        _print_artifacts(arts)
        print("\n# ===== diff vs working tree =====")
        print(diff_against_disk(arts))
        print()

    if do_apply:
        wrote_any = False
        for rt, arts in results:
            try:
                written = write_to_tree(arts, overwrite=do_force)
            except FileExistsError as e:
                print(f"# [{rt}] refusing to overwrite (use --force): {e}")
                continue
            except ValueError as e:
                print(f"# [{rt}] {e}")
                continue
            if written:
                wrote_any = True
                print(f"# WROTE ({rt}):")
                for w in written:
                    print("#   " + w)
        if not wrote_any:
            print("# nothing written.")
    else:
        print("# dry-run (no files written). Re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="MVP Terragrunt compose loop")
    ap.add_argument("request", nargs="?", help="plain-English request")
    ap.add_argument("--repo-root", required=True)
    ap.add_argument("--resource-type")
    ap.add_argument("--project")
    ap.add_argument("--env")
    ap.add_argument("--like", help="retrieval hint (the 'similar to X' precedent) on the explicit path")
    ap.add_argument("--apply", action="store_true", help="write files (default: dry-run)")
    ap.add_argument("--force", action="store_true", help="overwrite an existing inputs.hcl")
    ap.add_argument("--regen", action="store_true", default=False,
                    help="after a verified edit-set mutation, ALSO re-run whole-file "
                         "LLM generation to restyle (default: skip / splice-only)")
    args = ap.parse_args()
    if args.resource_type and args.project and args.env:
        arts = compose(args.repo_root, resource_type=args.resource_type,
                       project=args.project, env=args.env, query=args.like,
                       regen=args.regen)
    elif args.request:
        arts, intent = from_text(args.repo_root, args.request, regen=args.regen)
        print("# intent:", json.dumps(intent))
    else:
        ap.error("provide a request, or --resource-type/--project/--env")
    _print_bootstrap(arts)
    _print_artifacts(arts)
    print("\n# ===== diff vs working tree =====")
    print(diff_against_disk(arts))
    if args.apply:
        written = write_to_tree(arts, overwrite=args.force)
        print("\n# WROTE:")
        for w in written:
            print("#   " + w)
    else:
        print("\n# dry-run (no files written). Re-run with --apply to write.")
