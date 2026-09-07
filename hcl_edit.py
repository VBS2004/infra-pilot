"""hcl_edit.py - deterministic nested + list edits for a Terragrunt inputs.hcl.

P2 of the compose pipeline. hcl_override.py owns TOP-LEVEL SCALARS; this owns
NESTED paths (a.b.c) and LIST ops (append / remove) by splicing only the edited
node's byte span - never regenerating the file.

Built on hcl_parser.tokenize() (stdlib-only, already in the repo), so it runs
air-gapped with no new deps. hcl_parser's Block tree skips `inputs = { ... }`
(an object-literal value), so we navigate the TOKEN STREAM instead. Both parser
backends (tree-sitter / python-fallback) expose the same tokenizer, so this is
backend-independent.

Public API
----------
apply_edits(inputs_hcl, edits)  -> (new_hcl, results)
read_path(inputs_hcl, path)     -> raw value text | None   (readback)
verify_edits(new_hcl, edits)    -> (ok, failures)
coerce_edits(edits, contract)   -> normalized + type-coerced edits (stage 2)

An edit is a dict: {"path": "a.b.c", "op": "set"|"add"|"remove", ...}
  set:    {"value": <typed>}          # replace or inject a scalar/object/list
  add:    {"value": <typed>}          # append an element to a list
  remove: {"index": int} | {"match": {k: v}} | {"value": <scalar>}
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import hcl_override
from hcl_parser import tokenize, Token

_OPENERS = {"lbrace": "rbrace", "lbracket": "rbracket", "lparen": "rparen"}
_CLOSERS = {v: k for k, v in _OPENERS.items()}
_VALID_OPS = ("set", "add", "remove")


class PathError(ValueError):
    """Raised when an edit path cannot be resolved in the inputs.hcl tree."""


# --------------------------------------------------------------------------- #
# Token navigation
# --------------------------------------------------------------------------- #
def _match(tokens: List[Token], open_idx: int) -> int:
    depth = 0
    for j in range(open_idx, len(tokens)):
        k = tokens[j].kind
        if k in _OPENERS:
            depth += 1
        elif k in _CLOSERS:
            depth -= 1
            if depth == 0:
                return j
    return len(tokens) - 1


def _strip_quotes(v: str) -> str:
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        return v[1:-1]
    return v


def _find_inputs_object(tokens: List[Token]) -> Tuple[int, int]:
    for i, t in enumerate(tokens):
        if t.kind == "id" and t.value == "inputs":
            j = i + 1
            while j < len(tokens) and tokens[j].kind == "newline":
                j += 1
            if j < len(tokens) and tokens[j].kind == "eq":
                k = j + 1
                while k < len(tokens) and tokens[k].kind == "newline":
                    k += 1
                if k < len(tokens) and tokens[k].kind == "lbrace":
                    return k, _match(tokens, k)
    for i, t in enumerate(tokens):
        if t.kind == "lbrace":
            return i, _match(tokens, i)
    raise PathError("no `inputs = { ... }` object found")


def _value_end(tokens: List[Token], start: int, limit: int) -> int:
    depth = 0
    i = start
    while i < limit:
        k = tokens[i].kind
        if k in _OPENERS:
            depth += 1
        elif k in _CLOSERS:
            if depth == 0:
                return i
            depth -= 1
        elif depth == 0 and k in ("comma", "newline"):
            return i
        i += 1
    return limit


def _object_entries(tokens, lbrace_idx, rbrace_idx):
    entries = []
    i = lbrace_idx + 1
    while i < rbrace_idx:
        t = tokens[i]
        if t.kind in ("newline", "comma"):
            i += 1
            continue
        if t.kind in ("id", "str"):
            j = i + 1
            while j < rbrace_idx and tokens[j].kind == "newline":
                j += 1
            if j < rbrace_idx and tokens[j].kind == "eq":
                vstart = j + 1
                while vstart < rbrace_idx and tokens[vstart].kind == "newline":
                    vstart += 1
                vend = _value_end(tokens, vstart, rbrace_idx)
                entries.append({"key": _strip_quotes(t.value), "key_idx": i,
                                "val_start_idx": vstart, "val_end_idx": vend})
                i = vend
                continue
        if t.kind in _OPENERS:
            i = _match(tokens, i) + 1
            continue
        i += 1
    return entries


def _find_entry(entries, key):
    for e in entries:
        if e["key"] == key:
            return e
    return None


def _list_elements(tokens, lb, rb):
    elems = []
    i = lb + 1
    cur = None
    while i < rb:
        k = tokens[i].kind
        if k == "newline":
            i += 1
            continue
        if k == "comma":
            if cur is not None:
                elems.append((cur, i))
                cur = None
            i += 1
            continue
        if cur is None:
            cur = i
        if k in _OPENERS:
            i = _match(tokens, i) + 1
            continue
        i += 1
    if cur is not None:
        elems.append((cur, rb))
    return elems


def _last_content_idx(tokens, span):
    s, e = span
    j = e - 1
    while j > s and tokens[j].kind == "newline":
        j -= 1
    return j


def _navigate(tokens, obj_open, obj_close, segments):
    cur_open, cur_close = obj_open, obj_close
    for seg in segments[:-1]:
        match = _find_entry(_object_entries(tokens, cur_open, cur_close), seg)
        if match is None:
            raise PathError("path segment %r not found" % seg)
        vs = match["val_start_idx"]
        if tokens[vs].kind != "lbrace":
            raise PathError("path segment %r is not an object" % seg)
        cur_open, cur_close = vs, _match(tokens, vs)
    last = segments[-1]
    entry = _find_entry(_object_entries(tokens, cur_open, cur_close), last)
    return cur_open, cur_close, last, entry


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _fmt_scalar(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "null"
    if isinstance(v, (int, float)):
        return str(v)
    return '"%s"' % str(v)


def to_hcl(v, indent: int = 0) -> str:
    pad = "  " * indent
    child = "  " * (indent + 1)
    if isinstance(v, dict):
        if not v:
            return "{}"
        lines = ["{"]
        for k, val in v.items():
            lines.append("%s%s = %s" % (child, k, to_hcl(val, indent + 1)))
        lines.append(pad + "}")
        return "\n".join(lines)
    if isinstance(v, list):
        if not v:
            return "[]"
        lines = ["["]
        for item in v:
            lines.append("%s%s," % (child, to_hcl(item, indent + 1)))
        lines.append(pad + "]")
        return "\n".join(lines)
    return _fmt_scalar(v)


# --------------------------------------------------------------------------- #
# Edit normalization + type coercion (stage 2)
# --------------------------------------------------------------------------- #
def _coerce_edit(raw) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise PathError("edit must be an object, got %r" % type(raw).__name__)
    path = raw.get("path")
    if not isinstance(path, str) or not path.strip():
        raise PathError("edit missing string 'path'")
    op = str(raw.get("op", "set")).lower()
    if op not in _VALID_OPS:
        raise PathError("edit op must be one of %s" % (_VALID_OPS,))
    out: Dict[str, Any] = {"path": path.strip(), "op": op}
    if op in ("set", "add"):
        if "value" not in raw:
            raise PathError("'%s' edit needs a 'value'" % op)
        out["value"] = raw["value"]
    if op == "remove":
        for k in ("index", "match", "value"):
            if k in raw:
                out[k] = raw[k]
    return out


def coerce_value(value, type_str):
    t = (type_str or "").lower()
    if t.startswith("bool"):
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("true", "1", "yes")
    if t.startswith("number") or t in ("int", "integer", "float"):
        if isinstance(value, bool):
            return value
        try:
            f = float(value)
            return int(f) if f.is_integer() else f
        except (TypeError, ValueError):
            return value
    if t.startswith("string"):
        return value if isinstance(value, str) else str(value)
    return value


def coerce_edits(edits, contract=None):
    """Normalize edits and type-coerce top-level scalar `set` values against a
    module contract ({input_name: type_string}). Nested/list values pass through;
    unknown inputs are left as-is."""
    contract = contract or {}
    out = []
    for e in edits:
        c = _coerce_edit(e)
        if c["op"] == "set" and not isinstance(c.get("value"), (dict, list)):
            top = c["path"].split(".")[0]
            if top in contract:
                c["value"] = coerce_value(c["value"], contract[top])
        out.append(c)
    return out


# --------------------------------------------------------------------------- #
# Apply (stage 3)
# --------------------------------------------------------------------------- #
def _char_col(text, char_offset):
    ls = text.rfind("\n", 0, char_offset) + 1
    return char_offset - ls


def _inject_entry(text, tokens, parent_open, parent_close, key, value):
    close_char = tokens[parent_close].start
    line_start = text.rfind("\n", 0, close_char) + 1
    close_indent = text[line_start:close_char]
    if close_indent.strip():  # inline object `{ ... }`
        left = text[:close_char].rstrip()
        rendered = to_hcl(value, 0)
        pad = "" if left.endswith("{") else " "
        return left + "%s%s = %s " % (pad, key, rendered) + text[close_char:]
    entry_indent = close_indent + "  "
    level = len(entry_indent) // 2
    rendered = to_hcl(value, level)
    return text[:line_start] + "%s%s = %s\n" % (entry_indent, key, rendered) + text[line_start:]


def _apply_set(text, tokens, obj_open, obj_close, segments, value):
    parent_open, parent_close, key, entry = _navigate(tokens, obj_open, obj_close, segments)
    if entry is not None:
        vstart_char = tokens[entry["val_start_idx"]].start
        vend_char = tokens[entry["val_end_idx"] - 1].end
        level = _char_col(text, tokens[entry["key_idx"]].start) // 2
        rendered = to_hcl(value, level)
        return text[:vstart_char] + rendered + text[vend_char:], "set:replaced"
    return _inject_entry(text, tokens, parent_open, parent_close, key, value), "set:injected"


def _apply_add(text, tokens, obj_open, obj_close, segments, value):
    parent_open, parent_close, key, entry = _navigate(tokens, obj_open, obj_close, segments)
    if entry is None:
        return _inject_entry(text, tokens, parent_open, parent_close, key, [value]), "add:created-list"
    vs = entry["val_start_idx"]
    if tokens[vs].kind != "lbracket":
        raise PathError("path %r is not a list" % ".".join(segments))
    lb, rb = vs, _match(tokens, vs)
    open_char, close_char = tokens[lb].start, tokens[rb].start
    inline = "\n" not in text[open_char:close_char]
    elems = _list_elements(tokens, lb, rb)
    if inline:
        rendered = to_hcl(value, 0)
        inner = text[open_char + 1:close_char]
        if inner.strip() == "":
            new_inner = rendered
        else:
            head = inner.rstrip()
            tail_ws = inner[len(head):]  # preserve original edge spacing
            sep = " " if head.endswith(",") else ", "
            new_inner = head + sep + rendered + tail_ws
        return text[:open_char + 1] + new_inner + text[close_char:], "add:appended"
    line_start = text.rfind("\n", 0, close_char) + 1
    close_indent = text[line_start:close_char]
    elem_indent = close_indent + "  "
    level = len(elem_indent) // 2
    rendered = to_hcl(value, level)
    new_block = "%s%s,\n" % (elem_indent, rendered)
    inserts = [(line_start, new_block)]
    if elems:
        last_end = tokens[_last_content_idx(tokens, elems[-1])].end
        if "," not in text[last_end:close_char]:
            inserts.append((last_end, ","))
    for pos, ins in sorted(inserts, key=lambda x: -x[0]):
        text = text[:pos] + ins + text[pos:]
    return text, "add:appended"


def _element_matches(text, tokens, span, match):
    s, _e = span
    if tokens[s].kind != "lbrace":
        return False
    entries = _object_entries(tokens, s, _match(tokens, s))
    for mk, mv in match.items():
        me = _find_entry(entries, mk)
        if me is None:
            return False
        raw = text[tokens[me["val_start_idx"]].start:tokens[me["val_end_idx"] - 1].end]
        if hcl_override.norm_value(raw) != hcl_override.norm_value(mv):
            return False
    return True


def _delete_span(text, tokens, span):
    start_char = tokens[span[0]].start
    end_char = tokens[_last_content_idx(tokens, span)].end
    n = len(text)
    j = end_char
    while j < n and text[j] in " \t":
        j += 1
    if j < n and text[j] == ",":
        j += 1
    if j < n and text[j] == "\n":
        j += 1
    ls = text.rfind("\n", 0, start_char) + 1
    if text[ls:start_char].strip() == "":
        start_char = ls
    return text[:start_char] + text[j:]


def _apply_remove(text, tokens, obj_open, obj_close, segments, edit):
    parent_open, parent_close, key, entry = _navigate(tokens, obj_open, obj_close, segments)
    if entry is None:
        raise PathError("cannot remove: path %r not found" % ".".join(segments))
    vs = entry["val_start_idx"]
    if tokens[vs].kind == "lbracket":
        lb, rb = vs, _match(tokens, vs)
        elems = _list_elements(tokens, lb, rb)
        target = None
        if edit.get("index") is not None:
            idx = int(edit["index"])
            if not (0 <= idx < len(elems)):
                raise PathError("remove index %d out of range (0..%d)" % (idx, len(elems) - 1))
            target = elems[idx]
        elif edit.get("match"):
            for span in elems:
                if _element_matches(text, tokens, span, edit["match"]):
                    target = span
                    break
            if target is None:
                raise PathError("remove: no element matches %r" % edit["match"])
        elif "value" in edit:
            want = hcl_override.norm_value(edit["value"])
            for span in elems:
                li = _last_content_idx(tokens, span)
                raw = text[tokens[span[0]].start:tokens[li].end]
                if hcl_override.norm_value(raw) == want:
                    target = span
                    break
            if target is None:
                raise PathError("remove: no element equal to %r" % edit["value"])
        else:
            raise PathError("remove on a list needs index, match, or value")
        return _delete_span(text, tokens, target), "remove:element"
    # remove a whole key = value entry
    return _delete_span(text, tokens, (entry["key_idx"], entry["val_end_idx"])), "remove:entry"


def _apply_one(text, edit):
    segments = [s for s in edit["path"].split(".") if s]
    if not segments:
        raise PathError("empty path")
    tokens, _clean = tokenize(text)
    obj_open, obj_close = _find_inputs_object(tokens)
    op = edit["op"]
    if op == "set":
        return _apply_set(text, tokens, obj_open, obj_close, segments, edit["value"])
    if op == "add":
        return _apply_add(text, tokens, obj_open, obj_close, segments, edit["value"])
    if op == "remove":
        return _apply_remove(text, tokens, obj_open, obj_close, segments, edit)
    raise PathError("unknown op %r" % op)


def apply_edits(inputs_hcl, edits, contract=None):
    """Apply a typed edit-set to inputs_hcl. Re-tokenizes between edits so byte
    offsets stay valid. Returns (new_hcl, results); a failed edit is recorded
    with status 'error' and does not abort the rest."""
    text = inputs_hcl
    results = []
    for raw in coerce_edits(edits, contract):
        try:
            text, status = _apply_one(text, raw)
        except PathError as ex:
            results.append({"path": raw.get("path"), "op": raw.get("op"),
                            "status": "error", "error": str(ex)})
            continue
        results.append({"path": raw["path"], "op": raw["op"], "status": status})
    return text, results


# --------------------------------------------------------------------------- #
# Readback + verify (stage 4)
# --------------------------------------------------------------------------- #
def read_path(inputs_hcl, path):
    tokens, _ = tokenize(inputs_hcl)
    obj_open, obj_close = _find_inputs_object(tokens)
    segments = [s for s in path.split(".") if s]
    if not segments:
        raise PathError("empty path")
    _po, _pc, _key, entry = _navigate(tokens, obj_open, obj_close, segments)
    if entry is None:
        return None
    fi, li = entry["val_start_idx"], entry["val_end_idx"] - 1
    return inputs_hcl[tokens[fi].start:tokens[li].end]


def _leaf_strings(value):
    out = []
    if isinstance(value, dict):
        for v in value.values():
            out += _leaf_strings(v)
    elif isinstance(value, list):
        for v in value:
            out += _leaf_strings(v)
    elif isinstance(value, bool):
        out.append("true" if value else "false")
    elif value is not None:
        s = str(value)
        if s:
            out.append(s)
    return out


def verify_edits(new_hcl, edits):
    """Readback-verify: (ok, failures). set -> value present; add -> appended
    leaves present; remove -> identifying leaves gone / entry absent."""
    failures = []
    for raw in edits:
        try:
            edit = _coerce_edit(raw)
        except PathError as ex:
            failures.append(str(ex))
            continue
        path, op = edit["path"], edit["op"]
        try:
            found = read_path(new_hcl, path)
            resolve_err = None
        except PathError as ex:
            found, resolve_err = None, str(ex)
        if op == "set":
            val = edit["value"]
            if isinstance(val, (dict, list)):
                blob = found or ""
                for leaf in _leaf_strings(val):
                    if leaf not in blob:
                        failures.append("set %s: missing %r" % (path, leaf))
                        break
            elif found is None or hcl_override.norm_value(found) != hcl_override.norm_value(val):
                failures.append("set %s: expected %r, found %r" % (path, val, found))
        elif op == "add":
            blob = found or ""
            for leaf in _leaf_strings(edit["value"]):
                if leaf not in blob:
                    failures.append("add %s: appended element missing %r" % (path, leaf))
                    break
        elif op == "remove":
            if resolve_err:
                continue  # whole entry (key = value) gone -> good
            tokens, _ = tokenize(new_hcl)
            obj_open, obj_close = _find_inputs_object(tokens)
            segs = [s for s in path.split(".") if s]
            _po, _pc, _k, entry = _navigate(tokens, obj_open, obj_close, segs)
            if entry is None:
                continue  # whole entry removed -> good
            vs = entry["val_start_idx"]
            if tokens[vs].kind == "lbracket":
                lb, rb = vs, _match(tokens, vs)
                elems = _list_elements(tokens, lb, rb)
                if edit.get("match"):
                    if any(_element_matches(new_hcl, tokens, sp, edit["match"]) for sp in elems):
                        failures.append("remove %s: element matching %r still present" % (path, edit["match"]))
                elif "value" in edit:
                    want = hcl_override.norm_value(edit["value"])
                    for sp in elems:
                        li = _last_content_idx(tokens, sp)
                        raw = new_hcl[tokens[sp[0]].start:tokens[li].end]
                        if hcl_override.norm_value(raw) == want:
                            failures.append("remove %s: element %r still present" % (path, edit["value"]))
                            break
    return (len(failures) == 0, failures)


if __name__ == "__main__":
    import json
    sample = (
        'inputs = {\n'
        '  vpc_cidr = "10.160.0.0/16"\n'
        '  enable_flow_logs = true\n'
        '  workload_subnets = [\n'
        '    {\n'
        '      name              = "app-1a"\n'
        '      cidr              = "10.160.0.0/22"\n'
        '      availability_zone = "ap-south-1a"\n'
        '    },\n'
        '  ]\n'
        '}\n'
    )
    edits = [
        {"path": "workload_subnets", "op": "add", "value": {
            "name": "workload-1a", "cidr": "10.170.0.0/22",
            "availability_zone": "ap-south-1a"}},
        {"path": "enable_flow_logs", "op": "set", "value": False},
        {"path": "vpc_cidr", "op": "set", "value": "10.160.0.0/15"},
    ]
    new, res = apply_edits(sample, edits)
    print(new)
    print("results:", json.dumps(res, indent=2))
    print("verify:", verify_edits(new, edits))


def read_all_values(inputs_hcl: str) -> Dict[str, Any]:
    """Read ALL values from inputs.hcl into a flat dict with dot-notation keys.
    Example: {"cluster_name": "foo", "node_groups": [...], "node_groups[0].name": "bar"}
    Returns both top-level scalars and nested values for inventory filtering."""
    if not inputs_hcl:
        return {}
    
    tokens, _ = tokenize(inputs_hcl)
    obj_open, obj_close = _find_inputs_object(tokens)
    entries = _object_entries(tokens, obj_open, obj_close)
    
    result = {}
    
    for entry in entries:
        key = entry["key"]
        vstart = entry["val_start_idx"]
        vend = entry["val_end_idx"]
        
        # Check if value is a container (object or list)
        if vstart < len(tokens) and tokens[vstart].kind in ("lbrace", "lbracket"):
            # Recursively parse nested value
            nested = _parse_value(inputs_hcl, tokens, vstart, vend)
            result[key] = nested
            # Also add dot-notation paths for filtering
            _flatten(key, nested, result)
        else:
            # Simple scalar
            raw = inputs_hcl[tokens[vstart].start:tokens[vend-1].end]
            result[key] = hcl_override.norm_value(raw)
    
    return result


def _parse_value(inputs_hcl, tokens, vstart, vend) -> Any:
    """Parse a token range into a Python value."""
    if vstart >= len(tokens):
        return None
    
    tk = tokens[vstart].kind
    if tk == "lbrace":
        # Object
        rb = _match(tokens, vstart)
        entries = _object_entries(tokens, vstart, rb)
        out = {}
        for e in entries:
            k = e["key"]
            vs = e["val_start_idx"]
            ve = e["val_end_idx"]
            if vs < len(tokens) and tokens[vs].kind in ("lbrace", "lbracket"):
                out[k] = _parse_value(inputs_hcl, tokens, vs, ve)
            else:
                raw = inputs_hcl[tokens[vs].start:tokens[ve-1].end]
                out[k] = hcl_override.norm_value(raw)
        return out
    elif tk == "lbracket":
        # List
        rb = _match(tokens, vstart)
        elems = _list_elements(tokens, vstart, rb)
        out = []
        for es, ee in elems:
            if es < len(tokens) and tokens[es].kind in ("lbrace", "lbracket"):
                out.append(_parse_value(inputs_hcl, tokens, es, ee))
            else:
                raw = inputs_hcl[tokens[es].start:tokens[ee-1].end]
                out.append(hcl_override.norm_value(raw))
        return out
    else:
        raw = inputs_hcl[tokens[vstart].start:tokens[vend-1].end]
        return hcl_override.norm_value(raw)


def _flatten(prefix: str, value: Any, result: Dict[str, Any]) -> None:
    """Add dot-notation keys for nested values."""
    if isinstance(value, dict):
        for k, v in value.items():
            path = f"{prefix}.{k}"
            result[path] = v
            _flatten(path, v, result)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            path = f"{prefix}[{i}]"
            if isinstance(item, dict):
                result[path] = item
                _flatten(path, item, result)
            else:
                result[path] = item
