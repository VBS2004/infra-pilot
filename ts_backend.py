"""
Tree-sitter HCL backend (primary parser on the laptop).

This emits the SAME `Block` AST as hcl_parser.py, so index.py is backend-
agnostic. It is imported lazily by index.py; if tree-sitter or the HCL grammar
is missing, index.py silently falls back to the stdlib parser.

Install on the laptop (network available):
    uv pip install tree-sitter tree-sitter-hcl
  (or)
    pip install tree-sitter tree-sitter-hcl

The tree-sitter HCL grammar labels block nodes as `block` with a first child
`identifier` (the type) and zero or more `string_lit`/`identifier` labels,
followed by a `body`. Object-literal expressions are `object` nodes, so they are
naturally excluded from the block set — one of the reasons the design plan
prefers tree-sitter over regex for the production parser.
"""
from __future__ import annotations

from typing import List, Tuple

from hcl_parser import Block

# Import errors here propagate to index.py, which treats them as "backend
# unavailable" and falls back to the stdlib parser.
from tree_sitter import Parser, Language  # noqa: E402


def _load_language() -> Language:
    # tree-sitter-hcl exposes a PyCapsule via .language()
    import tree_sitter_hcl as tshcl
    return Language(tshcl.language())


_LANG = _load_language()
_PARSER = Parser(_LANG)


def _text(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _strip_quotes(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        return v[1:-1]
    return v


def _build_clean(src: str) -> str:
    """Blank out comments (keep newlines) to mirror hcl_parser.tokenize's clean.
    Tree-sitter gives us comment nodes; we overwrite their spans with spaces."""
    data = bytearray(src, "utf-8")
    tree = _PARSER.parse(bytes(data))
    stack = [tree.root_node]
    while stack:
        n = stack.pop()
        if n.type == "comment":
            for i in range(n.start_byte, n.end_byte):
                if data[i] != 0x0A:
                    data[i] = 0x20
        stack.extend(n.children)
    return data.decode("utf-8", "replace")


def _convert(node, src: bytes, parent: Block | None) -> List[Block]:
    """Walk tree-sitter nodes, emitting Block objects for `block` nodes."""
    out: List[Block] = []
    for child in node.children:
        if child.type == "block":
            # children: identifier (type), labels (string_lit/identifier), body
            ttype = None
            labels: List[str] = []
            body = None
            for c in child.children:
                if c.type in ("identifier",) and ttype is None:
                    ttype = _text(c, src)
                elif c.type in ("string_lit", "quoted_template"):
                    labels.append(_strip_quotes(_text(c, src)))
                elif c.type == "identifier":
                    labels.append(_text(c, src))
                elif c.type == "body":
                    body = c
            if ttype is None:
                continue
            blk = Block(
                type=ttype,
                labels=labels,
                line_start=child.start_point[0] + 1,
                line_end=child.end_point[0] + 1,
                char_start=child.start_byte,
                body_start=(body.start_byte if body else child.start_byte),
                body_end=(body.end_byte if body else child.end_byte),
                children=[],
            )
            if body is not None:
                blk.children = _convert(body, src, blk)
                for ch in blk.children:
                    ch.parent = blk
            out.append(blk)
        else:
            # descend to find nested blocks (e.g. inside body)
            out.extend(_convert(child, src, parent))
    return out


def parse_hcl(text: str) -> Tuple[List[Block], str]:
    src = bytes(text, "utf-8")
    tree = _PARSER.parse(src)
    blocks = _convert(tree.root_node, src, None)
    clean = _build_clean(text)
    return blocks, clean
