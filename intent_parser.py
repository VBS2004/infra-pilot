"""intent_parser.py - turn a plain-English infra request into a compose intent.

(Renamed from planner.py: the repo already has a planner.py with a `Planner`
class that does reuse-vs-write decisions; this module only does NL -> JSON.)

Uses generator.complete_json (MiniMax-M2) to emit a strict JSON intent that the
rest of the compose loop consumes. The model proposes; deterministic
post-processing normalizes/validates and NEVER does path math (paths.py owns
that). Project/env/component are later checked against the real tree.

The intent schema (all keys always present; unknowns -> null):
    {
      "resource_type": "s3" | "ec2" | "rds" | ...,   # the THING TO CREATE
      "project":       "payments_pro" | "optimus" | null,   # DESTINATION project
      "env":           "prod" | "dev" | "payments_pro" | null,  # DESTINATION env
      "name":          "short-logical-name" | null,
      "region":        "ap-south-1" | null,
      "specifics":     { ... free-form requested settings ... },
      "reference":     { "project":..., "env":..., "component":..., "provider":... } | null,
      "notes":         "anything the user said that needs a human decision"
    }

DESTINATION (resource_type/project/env) = WHERE to create the new component.
REFERENCE = the existing component to model it on ("similar to X"); it NEVER
fills the destination fields - if the user only gave a reference and no
destination, project/env stay null so the caller can ask.
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional

import generator

INTENT_KEYS = ("resource_type", "project", "env", "name", "region",
               "specifics", "reference", "notes", "operation")

_REFERENCE_KEYS = ("project", "env", "component", "provider")

_SYSTEM = (
    "You convert a plain-English cloud-infrastructure request into a STRICT JSON "
    "intent for a Terraform/Terragrunt compose tool at Acme Corp (AWS only). "
    "Output ONLY a JSON object - no prose, no code fence.\n\n"
    "Keep TWO ideas strictly separate:\n"
    "  DESTINATION = WHERE the new component is created (top-level "
    "resource_type/project/env).\n"
    "  REFERENCE   = WHICH existing component to model it on, signalled by phrases "
    "like 'similar to', 'like', 'modeled on', 'same as', 'copy of', 'based on'. "
    "This goes ONLY in the nested 'reference' object - NEVER in the top-level "
    "destination fields.\n\n"
    "Keys (always include every key; use null when unknown):\n"
    "  resource_type: the AWS component dir name for the THING TO CREATE (e.g. s3, "
    "ec2, rds, alb, asg, security-group, eks, kms, vpc-endpoint). Lowercase, "
    "hyphenated.\n"
    "  project: the DESTINATION infra project slug (where to create it) if the user "
    "named one, else null. Do NOT fill this from a 'similar to <project>' phrase.\n"
    "  env: the DESTINATION environment (where to create it) if stated, else null. "
    "Do NOT fill this from a 'similar to <env>' phrase.\n"
    "  name: a short logical name for the new resource, else null.\n"
    "  region: AWS region if stated, else null.\n"
    "  specifics: an object of concrete requested settings (sizes, counts, "
    "versioning, encryption, ports, cidrs, etc.). Empty object if none.\n"
    "  reference: the existing component to model on, as an object with keys "
    "{\"project\", \"env\", \"component\", \"provider\"} (use null for any unknown "
    "field, and null for the whole object if the user gave no 'similar to' "
    "reference). Example: 'create an ecs in auth preprod similar to the pre-prod "
    "ecs in billing' -> destination project=auth, env=preprod, resource_type=ecs; "
    "reference={\"project\":\"billing\",\"env\":\"pre-prod\",\"component\":\"ecs\","
    "\"provider\":null}.\n"
    "  notes: anything ambiguous or needing a human decision.\n"
    "Never invent account IDs, ARNs, or KMS keys - put such gaps in notes.\n"
    "If the request only describes something to copy (e.g. 'like the pre-prod "
    "billing ecs') but never says where to PUT the new component, leave project "
    "and/or env null - do NOT borrow them from the reference - so the tool can ask.\n"
    "  operation: classify the request as exactly one of create|add|update|modify|"
    "delete. create = stand up a NEW component that does not exist yet; add = add a "
    "sub-element INSIDE an existing component (e.g. 'add a workload subnet', 'add an "
    "ingress rule'); update/modify = change the value of something that already "
    "exists; delete = remove an existing component or sub-element. Default to "
    "'create' ONLY when the user clearly wants a brand-new component; when the "
    "request references an existing component/env/project, prefer add/update/"
    "modify/delete."
)

_OPERATIONS = ("create", "update", "add", "modify", "delete")

_OP_HINTS = (
    ("delete", ("delete", "remove", "drop", "tear down", "destroy")),
    ("add", ("add ", "append", "attach", "insert", "add a", "add an")),
    ("update", ("update", "change", "modify", "set ", "increase", "decrease",
                "bump", "resize", "rename", "edit")),
    ("create", ("create", "new ", "provision", "stand up", "spin up", "bootstrap")),
)


def classify_operation(text: str) -> str:
    """Heuristic op classification used when the model omits `operation`."""
    t = (text or "").lower()
    for op, hints in _OP_HINTS:
        if any(h in t for h in hints):
            return op
    return "create"


def _normalize_operation(value, text: str) -> str:
    v = str(value or "").strip().lower()
    if v in _OPERATIONS:
        return v
    return classify_operation(text)


def is_mutation(intent: Dict[str, object]) -> bool:
    return intent.get("operation", "create") != "create"


def notes_flag_ambiguity(intent: Dict[str, object]) -> bool:
    """True when a mutation is under-specified enough that fanning out or
    guessing would be dangerous (no component resolved, or add/update with no
    target field)."""
    if not is_mutation(intent):
        return False
    if not intent.get("resource_type") and not intent.get("reference"):
        return True
    if intent.get("operation") in ("add", "update", "modify", "delete"):
        return not (intent.get("specifics") or intent.get("name"))
    return False


def build_messages(text: str, *, known_components: Optional[List[str]] = None,
                   known_projects: Optional[List[str]] = None,
                   known_envs: Optional[List[str]] = None) -> List[Dict[str, str]]:
    hints = []
    if known_components:
        hints.append("Valid resource_type values: " + ", ".join(sorted(known_components)) + ".")
    if known_projects:
        hints.append("Known projects: " + ", ".join(sorted(known_projects)) + ".")
    if known_envs:
        hints.append("Known envs: " + ", ".join(sorted(known_envs)) + ".")
    sys_content = _SYSTEM + (("\n\n" + " ".join(hints)) if hints else "")
    return [
        {"role": "system", "content": sys_content},
        {"role": "user", "content": text},
    ]


def _normalize_reference(ref: object) -> Optional[Dict[str, object]]:
    """Normalize the nested reference object; return None if it carries nothing."""
    if not isinstance(ref, dict):
        return None
    out: Dict[str, object] = {}
    for k in _REFERENCE_KEYS:
        v = ref.get(k)
        if isinstance(v, str):
            v = v.strip()
            if k == "component" and v:
                v = v.lower().replace(" ", "-").replace("_", "-")
            out[k] = v or None
        else:
            out[k] = None
    if not any(out.get(k) for k in _REFERENCE_KEYS):
        return None
    return out


def _normalize(raw: Dict[str, object]) -> Dict[str, object]:
    intent: Dict[str, object] = {k: raw.get(k) for k in INTENT_KEYS}
    rt = intent.get("resource_type")
    if isinstance(rt, str):
        intent["resource_type"] = rt.strip().lower().replace(" ", "-").replace("_", "-")
    if not isinstance(intent.get("specifics"), dict):
        intent["specifics"] = {}
    intent["reference"] = _normalize_reference(intent.get("reference"))
    for k in ("project", "env", "name", "region", "notes"):
        v = intent.get(k)
        if isinstance(v, str) and not v.strip():
            intent[k] = None
    return intent


def parse_intent(text: str, *, known_components: Optional[List[str]] = None,
                 known_projects: Optional[List[str]] = None,
                 known_envs: Optional[List[str]] = None) -> Dict[str, object]:
    """Return a normalized intent dict. Raises generator.GeneratorError on
    gateway failure; raises ValueError if the model emits unparseable JSON."""
    messages = build_messages(text, known_components=known_components,
                              known_projects=known_projects, known_envs=known_envs)
    raw = generator.complete_json(messages, temperature=0.0, max_tokens=800)
    if not isinstance(raw, dict):
        raise ValueError("intent_parser: model did not return a JSON object: " + json.dumps(raw)[:300])
    intent = _normalize(raw)
    intent["operation"] = _normalize_operation(intent.get("operation"), text)
    return intent


def missing_fields(intent: Dict[str, object]) -> List[str]:
    """Required DESTINATION fields the caller must resolve (ask the user / infer
    from tree) before path resolution can proceed. A 'similar to' reference does
    NOT satisfy these - it describes the source, not where to create."""
    return [k for k in ("resource_type", "project", "env") if not intent.get(k)]


if __name__ == "__main__":
    import sys
    req = sys.argv[1] if len(sys.argv) > 1 else \
        "Create an S3 bucket for payments_pro prod with versioning enabled"
    out = parse_intent(req)
    print(json.dumps(out, indent=2))
    miss = missing_fields(out)
    print("\nmissing:", miss or "(none)")
