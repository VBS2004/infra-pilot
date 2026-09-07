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

import bootstrap
import generator
import hcl_edit
import hcl_override
import intent_parser
import paths
import retrieval

GenerateFn = Callable[[List[Dict[str, str]]], str]
GenerateJsonFn = Callable[[List[Dict[str, str]]], Dict]


@dataclasses.dataclass
class ComposeArtifacts:
    component_dir: str
    terragrunt_hcl: str
    inputs_hcl: str
    decision: str                 # "reuse" | "net-new"
    module_key: Optional[str]
    terraform_module: str
    notes: List[str]
    grounding: str
    bootstrap: Optional["bootstrap.BootstrapPlan"] = None
    refused: bool = False
    change_applied: Optional[bool] = None   # None = n/a (create); else did the change land?


_SYS = (
    "You are a Terraform/Terragrunt composer for Acme Corp (AWS). "
    "Emit the COMPLETE contents of ONE env-tier component's inputs.hcl file. "
    "Output ONLY HCL - no prose, no code fence.\n\n"
    "IMPORTANT: Deterministic edits (top-level scalars, nested values, list appends) "
    "have ALREADY been applied to the base inputs.hcl below. PRESERVE these changes "
    "exactly - do NOT undo or regenerate them. Only make requested changes on top.\n\n"
    "RULES:\n"
    "- The file MUST be a single top-level `inputs = { ... }` block.\n"
    "- Use ONLY input names that appear in the MODULE INPUT CONTRACT / variables.tf "
    "below. NEVER invent input names; match the exact spelling shown (e.g. "
    "`bucket_versioning`, not `versioning`).\n"
    "- Satisfy every REQUIRED input. OMIT optional inputs unless the request "
    "specifies them (module defaults apply).\n"
    "- Mirror the field names, nesting, and overall style of the sibling inputs.hcl "
    "example(s) exactly.\n"
    "- Do NOT redefine values already provided by common.hcl or the root configuration "
    "file (account_id, region, provider, backend, default_tags) unless overriding.\n"
    "- TAGS come from common.hcl `default_tags`. Do NOT add a per-resource `tags` "
    "block unless a sibling example shows one, and then copy ONLY its business "
    "keys (e.g. `UsedFor`).\n"
    "- `CUSTOM_*`, `CUSTOM_TF_AWS_*`, and `CUSTOM_DTRACK_*` are checkov POLICY "
    "IDENTIFIERS used by the validation pipeline. They are NOT tags and NOT inputs, "
    "and MUST NEVER appear anywhere in the generated HCL.\n"
    "- Never fabricate account IDs, ARNs, KMS key IDs, role names, or secrets. If a "
    "required value is unknown, use a clearly-marked \"TODO\" string and continue.\n"
    "- If a CURRENT inputs.hcl is provided, treat it as the source of truth: return "
    "it unchanged except for the explicitly requested edits, and never swap real "
    "names/ARNs/account IDs/values for placeholders.\n"
    "- If a REFERENCE inputs.hcl is provided, reproduce its structure and real "
    "values; change ONLY env-specific identifiers (names embedding the source env, "
    "account IDs, regions) to fit the destination, using the concrete DESTINATION "
    "identifiers given in the user message. Never collapse it to generic "
    "placeholders (no `newenv`, `dev-ecs`, `sample-service`, `sample-image:latest`).\n"
    "- Prefer the structure and style of the sibling inputs.hcl example(s)."
)


# ===========================================================================
# EDIT ENGINE (P2, A+C)
# ===========================================================================

# Type-name prefixes that mean "this input is NOT a plain top-level scalar" and
# must be routed to the LLM edit-planner instead of set_top_level.
_COMPLEX_TYPES = ("list", "object", "map", "set", "tuple")

_EDIT_SYS = (
    "You are an HCL edit planner for a Terragrunt inputs.hcl file. Given the "
    "CURRENT inputs.hcl, the module GROUNDING (variables.tf contract + sibling "
    "examples), and a set of REQUESTED VALUES, output ONLY a JSON object: "
    '{"edits": [ {"path": "a.b.c", "op": "set"|"add"|"remove", "value": <typed>} ]}.\n\n'
    "RULES:\n"
    "- Use ONLY input names that appear in the GROUNDING contract. Map a loosely-"
    "named requested key onto the correct schema field (e.g. requested "
    "'cidr_block' onto a subnet object's 'cidr' field if that is what variables.tf "
    "declares).\n"
    "- To append an element to a list(object(...)) input, target the LIST input "
    "with op 'add' and give the WHOLE element object as 'value' (e.g. path "
    "'workload_subnets', op 'add', value { ...one subnet object with the schema's "
    "exact field names... }). GROUP related requested scalars into ONE object; "
    "never scatter them as separate top-level keys.\n"
    "- MIRROR SIBLINGS: when the target list already has element(s), the new "
    "element MUST copy the siblings' exact shape -- the same field names, the same "
    "use of local.* references (e.g. tags = local.subnet_tags[\"workload\"], NOT an "
    "inlined tags object), and the same naming pattern (e.g. "
    "name = \"${local.env_name}-workload-<az-suffix>\"). Populate ONLY fields that "
    "the siblings have: set the explicitly requested values (cidr, availability_"
    "zone), and DERIVE name/tags from the sibling convention. NEVER invent tag "
    "keys/values, nodepool names, or any field absent from the siblings and not "
    "requested (e.g. do NOT introduce 'optimus_*' or any value not present in the "
    "file or the request).\n"
    "- op 'set' changes an existing scalar/nested value, 'add' appends a list "
    "element, 'remove' deletes one.\n"
    "- Never invent inputs, ARNs, account IDs, or values not implied by the "
    "request. Express ONLY the requested change; preserve everything else.\n"
    "- Output the JSON object only. No prose, no code fence."
)


def _default_generate_json(messages: List[Dict[str, str]]) -> Dict:
    """Edit-planner JSON call."""
    return generator.complete_json(
        messages,
        temperature=0.0, max_tokens=1200,
    )


def _plan_edits_llm(base_hcl: str, values: Dict, grounding: str,
                    operation: str, generate_json: GenerateJsonFn) -> List[Dict]:
    """Option A: LLM places `values` into the right (nested/list) location as a
    typed edit-set, grounded in the real variables.tf schema (via grounding)."""
    user = (
        f"OPERATION: {operation}\n\n"
        f"MODULE GROUNDING (schema + examples):\n{grounding[:4000]}\n\n"
        f"REQUESTED VALUES (JSON): {json.dumps(values)}\n\n"
        f"CURRENT inputs.hcl:\n{base_hcl}\n\n"
        "Emit the edits JSON now."
    )
    raw = generate_json([
        {"role": "system", "content": _EDIT_SYS},
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
            edits = _plan_edits_llm(base_hcl, residual, grounding, operation, gen_json)
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


def build_messages(resource_type: str, specifics: Dict[str, object], grounding: str,
                   required: List[str], optional: List[str],
                   decision: str = "reuse", existing: bool = False,
                   existing_inputs: Optional[str] = None,
                   reference_inputs: Optional[str] = None,
                   reference_label: Optional[str] = None,
                   dest_ctx: Optional[Dict[str, object]] = None,
                   source_ctx: Optional[Dict[str, object]] = None,
                   repo: Optional[str] = None) -> List[Dict[str, str]]:
    # The verb reflects the actual situation: we are almost always FILLING a
    # module's inputs, not authoring a resource. "create" only applies when no
    # reusable module exists yet (the net-new leaf-module case).
    if existing and existing_inputs:
        verb = (f"This env-tier `{resource_type}` component ALREADY EXISTS. Start "
                f"from its CURRENT inputs.hcl (below) and return the COMPLETE updated "
                f"file, applying ONLY the requested change(s). Preserve every existing "
                f"entry - names, roles, policies, values - verbatim unless the request "
                f"explicitly changes it. Never drop/rename entries or swap real values "
                f"for placeholders.")
    elif reference_inputs:
        verb = (f"Compose the `inputs.hcl` for a new env-tier `{resource_type}` "
                f"component, reusing the existing `{resource_type}` module and MODELED "
                f"ON the REFERENCE inputs.hcl provided below. Reproduce the reference's "
                f"structure and real values; change ONLY env-specific identifiers "
                f"(names embedding the source env, account IDs, regions) to fit the "
                f"destination. Do NOT collapse or invent placeholder "
                f"services/containers/buckets.")
    elif existing:
        verb = (f"Compose the `inputs.hcl` for the EXISTING env-tier "
                f"`{resource_type}` component (fill the module's inputs).")
    elif decision == "net-new":
        verb = (f"Create a net-new env-tier `{resource_type}` component "
                f"(no reusable module exists yet).")
    else:
        verb = (f"Compose the `inputs.hcl` for a new env-tier `{resource_type}` "
                f"component, reusing the existing `{resource_type}` module.")

    user: List[str] = ["REQUEST: " + verb]
    if specifics:
        user.append("REQUESTED SETTINGS (JSON): " + json.dumps(specifics))
    if required:
        user.append("REQUIRED INPUTS: " + ", ".join(required))
    if optional:
        user.append("OPTIONAL INPUTS (omit unless requested): " + ", ".join(optional))
        
    schema_ctx = ""
    if repo:
        import root_schema
        schema_ctx = root_schema.get_schema(repo).as_prompt_context() + "\n\n"
        
    user.append("\nGROUNDING:\n" + schema_ctx + grounding)
    if existing and existing_inputs:
        user.append("\nCURRENT inputs.hcl (reproduce unless changed):\n" + existing_inputs)
    if reference_inputs and not (existing and existing_inputs):
        label = reference_label or "source to model on"
        user.append(f"\nREFERENCE inputs.hcl ({label}) -- reproduce its structure and "
                    f"real values, changing ONLY the env-specific identifiers to match "
                    f"the destination:\n" + reference_inputs)
        if dest_ctx:
            dl = ["\nDESTINATION -- rename the source env's identifiers to THESE. "
                  "NEVER use generic placeholders like \"newenv\", \"sample-*\", or "
                  "\"dev-ecs\":"]
            dl.append(f"  project     = {dest_ctx.get('project')}")
            dl.append(f"  env-tier    = {dest_ctx.get('env_tier')}   # use as env_name "
                      f"and as the prefix in identifiers that embed the env "
                      f"(cluster_name, bucket_name, <env>-ecs-cluster, role names, etc.)")
            if dest_ctx.get("environment"):
                dl.append(f"  environment = {dest_ctx.get('environment')}")
            if dest_ctx.get("region"):
                dl.append(f"  region      = {dest_ctx.get('region')}")
            if source_ctx:
                env_toks = [t for t in (source_ctx.get("env_tier"),
                                        source_ctx.get("environment")) if t]
                if env_toks:
                    dl.append("  SOURCE env tokens to replace with the destination "
                              "env-tier above: "
                              + ", ".join('"%s"' % t for t in env_toks))
                if source_ctx.get("project") and dest_ctx.get("project"):
                    dl.append(f"  SOURCE project token to replace with "
                              f"'{dest_ctx.get('project')}': \"{source_ctx.get('project')}\" "
                              f"(in resource names, container names, ECR registry paths, ARNs).")
            acct = dest_ctx.get("account_id")
            if acct:
                dl.append(f"  ECR image URIs & ARNs: rewrite the AWS account ID to \"{acct}\" "
                          f"and the project namespace as above; keep the image name and tag.")
            else:
                dl.append("  ECR image URIs & ARNs: rewrite the project namespace as above; "
                          "LEAVE the AWS account ID as-is (unknown for the destination) -- "
                          "do NOT invent one; it is flagged for review.")
            dl.append("  Keep values you cannot derive (RESOURCE_IDs, KMS key IDs, secrets) "
                      "VERBATIM -- never invent them; they are reviewed before apply.")
            user.append("\n".join(dl))

    # --- NAME OVERRIDE: force the requested logical name onto the module's
    #     primary name/identifier input. We do NOT hardcode which field that is;
    #     the model picks it from the INPUT CONTRACT / variables.tf in the
    #     grounding above (e.g. cluster_name, bucket_name, or a plain name). ---
    requested_name = (specifics or {}).get("name")
    if requested_name:
        user.append(
            f'\nNAME OVERRIDE: the requested resource name is "{requested_name}". '
            f"Set the module's primary name/identifier input -- the one declared in "
            f'the INPUT CONTRACT above -- to this value, overriding any existing or '
            f'convention-derived value. Apply this even when reproducing an existing file.')

    user.append("\nEmit inputs.hcl now.")
    return [{"role": "system", "content": _SYS},
            {"role": "user", "content": "\n".join(user)}]


def _default_generate(messages: List[Dict[str, str]]) -> str:
    if os.environ.get("LOCAL_MODEL"):
        import local_generator
        return local_generator.complete(messages, temperature=0.1, max_tokens=8192)
    return generator.complete(
        messages,
        temperature=0.1, max_tokens=8192,
    )


def _strip_fence(t: str) -> str:
    t = (t or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def _indent(t: str, prefix: str = "  ") -> str:
    return "\n".join((prefix + ln if ln.strip() else ln) for ln in t.splitlines())


def _finalize_inputs(raw: str) -> str:
    t = _strip_fence(raw)
    if not re.match(r"^\s*inputs\s*=", t):
        if t.startswith("{") and t.endswith("}"):
            t = "inputs = " + t
        else:
            t = "inputs = {\n" + _indent(t) + "\n}"
    return t if t.endswith("\n") else t + "\n"


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

    rc = paths.resolve(repo, project, env, resource_type, provider=provider,
                       terraform_module=terraform_module)
    tm = rc.terraform_module or "env"

    # --- P0 GUARDRAIL 1: never create net-new under a mutation verb. An
    # update/add/modify/delete against a component that does not exist yet must
    # NOT silently scaffold a new one -- refuse with a clear reason instead.
    if operation != "create":
        _early_inputs = os.path.join(rc.component_dir, "inputs.hcl")
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
        from catalog import ModuleCatalog
        from index import get_index
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

    inputs_path = os.path.join(rc.component_dir, "inputs.hcl")
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
                resource_type, operation, generate_json=generate_json)
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
            inputs_hcl = _finalize_inputs(existing_inputs)
            change_applied = _verify_requested_change(
                inputs_hcl, base_before or "", specifics)
            notes.append("mutation applied via typed edit-set; skipped whole-file "
                         "regeneration (use --regen to also restyle via the LLM)")
            if not change_applied:
                notes.append("# CHANGE NOT APPLIED: requested %s did not alter inputs.hcl"
                             % operation)
        else:
            gen = generate or _default_generate
            messages = build_messages(resource_type, specifics, grounding, req_names, opt_names,
                                      decision=bundle.decision, existing=existing,
                                      existing_inputs=existing_inputs,
                                      reference_inputs=(None if existing else reference_inputs),
                                      reference_label=reference_label,
                                      dest_ctx=dest_ctx, source_ctx=source_ctx,
                                      repo=repo)
            inputs_hcl = _finalize_inputs(gen(messages))
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
        terragrunt_hcl=paths.standard_terragrunt_hcl(repo),
        inputs_hcl=inputs_hcl,
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
    for fname, new in (("terragrunt.hcl", a.terragrunt_hcl), ("inputs.hcl", a.inputs_hcl)):
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
    tg = os.path.join(a.component_dir, "terragrunt.hcl")
    if not os.path.exists(tg):
        open(tg, "w", encoding="utf-8").write(a.terragrunt_hcl)
        written.append(tg)
    ip = os.path.join(a.component_dir, "inputs.hcl")
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
