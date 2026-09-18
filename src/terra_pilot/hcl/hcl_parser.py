"""
Dependency-free HCL / Terragrunt parser for the Acme Terraform Assistant.

Why not tree-sitter?
--------------------
The design plan specifies a *tree-sitter HCL parser*. On the air-gapped build
box (and this sandbox) the tree-sitter wheels + grammar are not installable, so
this module implements an equivalent, deterministic, stdlib-only parser behind a
stable AST interface (`Block`, `parse_hcl`). When tree-sitter becomes available
you can drop in a `TreeSitterBackend` that emits the same `Block` tree and the
rest of the indexer (index.py) is unchanged.

What it produces
----------------
A tree of `Block` objects. Each block records its type, labels, line span,
character span, the (comment-stripped) raw text of its body, and its children.
That is everything index.py needs to build the symbol table and the 5 edge types.

The parser is intentionally "dumb and deterministic" (per the design plan): a
lexer + brace-matched recursive descent. No LLM, no network, no guessing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple


# --------------------------------------------------------------------------- #
# Tokenizer
# --------------------------------------------------------------------------- #
@dataclass
class Token:
    kind: str          # 'id' | 'str' | 'lbrace' | 'rbrace' | 'lbracket'
                       # | 'rbracket' | 'lparen' | 'rparen' | 'eq' | 'comma'
                       # | 'newline' | 'other'
    value: str
    line: int          # 1-based line where the token starts
    start: int         # char offset (into the *clean* text)
    end: int           # char offset (exclusive)


_ID_START = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_")
_ID_CONT = _ID_START | set("0123456789-.")


def tokenize(text: str) -> Tuple[List[Token], str]:
    """Lex HCL text.

    Returns (tokens, clean_text) where ``clean_text`` is the same length as
    ``text`` but with comment characters replaced by spaces (newlines kept).
    String contents are preserved in clean_text so that interpolations such as
    ``${var.foo}`` remain scannable for reference extraction.
    """
    n = len(text)
    clean = list(text)
    tokens: List[Token] = []
    i = 0
    line = 1

    def blank(a: int, b: int) -> None:
        for k in range(a, b):
            if clean[k] != "\n":
                clean[k] = " "

    while i < n:
        c = text[i]

        # newline
        if c == "\n":
            tokens.append(Token("newline", "\n", line, i, i + 1))
            line += 1
            i += 1
            continue

        # whitespace
        if c in " \t\r":
            i += 1
            continue

        # line comments: # or //
        if c == "#" or (c == "/" and i + 1 < n and text[i + 1] == "/"):
            j = i
            while j < n and text[j] != "\n":
                j += 1
            blank(i, j)
            i = j
            continue

        # block comment /* ... */
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            j = i + 2
            while j < n and not (text[j] == "*" and j + 1 < n and text[j + 1] == "/"):
                if text[j] == "\n":
                    line += 1
                j += 1
            j = min(j + 2, n)
            blank(i, j)
            i = j
            continue

        # heredoc:  <<TAG ... \nTAG   or  <<-TAG ... \nTAG
        if c == "<" and i + 1 < n and text[i + 1] == "<":
            k = i + 2
            if k < n and text[k] == "-":
                k += 1
            tag_start = k
            while k < n and (text[k] in _ID_START or text[k].isdigit()):
                k += 1
            tag = text[tag_start:k]
            if tag:
                # consume to end of opening line
                while k < n and text[k] != "\n":
                    k += 1
                # now scan lines until a line whose stripped content == tag
                body_start = k
                while k < n:
                    if text[k] == "\n":
                        line += 1
                        line_begin = k + 1
                        le = line_begin
                        while le < n and text[le] != "\n":
                            le += 1
                        if text[line_begin:le].strip() == tag:
                            k = le
                            break
                    k += 1
                # emit as a string token (so refs inside heredocs are scannable)
                tokens.append(Token("str", text[i:k], line, i, k))
                i = k
                continue

        # quoted string with ${...} interpolation awareness
        if c == '"':
            j = i + 1
            while j < n:
                ch = text[j]
                if ch == "\\":
                    j += 2
                    continue
                if ch == "$" and j + 1 < n and text[j + 1] == "{":
                    # interpolation: track brace depth, allow nested strings
                    depth = 1
                    j += 2
                    while j < n and depth > 0:
                        if text[j] == "{":
                            depth += 1
                        elif text[j] == "}":
                            depth -= 1
                        elif text[j] == '"':
                            j += 1
                            while j < n and text[j] != '"':
                                if text[j] == "\\":
                                    j += 1
                                j += 1
                        elif text[j] == "\n":
                            line += 1
                        j += 1
                    continue
                if ch == '"':
                    j += 1
                    break
                if ch == "\n":
                    line += 1
                j += 1
            tokens.append(Token("str", text[i:j], line, i, j))
            i = j
            continue

        # structural single chars
        simple = {
            "{": "lbrace",
            "}": "rbrace",
            "[": "lbracket",
            "]": "rbracket",
            "(": "lparen",
            ")": "rparen",
            "=": "eq",
            ",": "comma",
        }
        if c in simple:
            # '==' is a comparison, not assignment
            if c == "=" and i + 1 < n and text[i + 1] == "=":
                tokens.append(Token("other", "==", line, i, i + 2))
                i += 2
                continue
            tokens.append(Token(simple[c], c, line, i, i + 1))
            i += 1
            continue

        # identifier / keyword / number
        if c in _ID_START or c.isdigit():
            j = i
            while j < n and text[j] in _ID_CONT:
                j += 1
            tokens.append(Token("id", text[i:j], line, i, j))
            i = j
            continue

        # anything else (operators, ':', '?', etc.)
        tokens.append(Token("other", c, line, i, i + 1))
        i += 1

    return tokens, "".join(clean)


# --------------------------------------------------------------------------- #
# AST
# --------------------------------------------------------------------------- #
@dataclass
class Block:
    type: str                       # e.g. 'resource', 'module', 'variable'
    labels: List[str]               # quoted/bare labels after the type
    line_start: int
    line_end: int
    char_start: int                 # offset of first label/type token
    body_start: int                 # offset just after '{'
    body_end: int                   # offset of matching '}'
    children: List["Block"] = field(default_factory=list)
    parent: Optional["Block"] = None

    @property
    def header(self) -> str:
        parts = [self.type] + [f'"{l}"' for l in self.labels]
        return " ".join(parts)


def _strip_quotes(v: str) -> str:
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        return v[1:-1]
    return v


def parse_hcl(text: str) -> Tuple[List[Block], str]:
    """Parse HCL text into a list of top-level Blocks.

    Returns (top_level_blocks, clean_text). ``clean_text`` (comments blanked) is
    returned so callers can slice block bodies for reference scanning.
    """
    tokens, clean = tokenize(text)
    pos = 0
    ntok = len(tokens)

    def parse_body(end_is_rbrace: bool) -> List[Block]:
        nonlocal pos
        blocks: List[Block] = []
        header: List[Token] = []        # accumulated id/str tokens at stmt start
        saw_eq = False                  # are we on the RHS of an '=' ?

        while pos < ntok:
            tok = tokens[pos]

            if tok.kind == "rbrace":
                if end_is_rbrace:
                    return blocks
                # stray rbrace at top level: skip
                pos += 1
                header = []
                saw_eq = False
                continue

            if tok.kind == "newline":
                pos += 1
                # a newline ends a simple statement; reset accumulation unless
                # we are mid-expression continuation (best-effort: reset)
                header = []
                saw_eq = False
                continue

            if tok.kind == "eq":
                saw_eq = True
                header = []
                pos += 1
                continue

            if tok.kind == "comma":
                header = []
                saw_eq = False
                pos += 1
                continue

            if tok.kind in ("id", "str"):
                header.append(tok)
                pos += 1
                continue

            if tok.kind in ("lbracket", "lparen"):
                # expression container: skip to its match
                _skip_container(tok.kind)
                header = []
                continue

            if tok.kind == "lbrace":
                if header and not saw_eq:
                    # this is a block: header[0] is type, rest are labels
                    btype = header[0].value
                    labels = [_strip_quotes(t.value) for t in header[1:]]
                    char_start = header[0].start
                    line_start = header[0].line
                    body_start = tok.end
                    pos += 1  # consume '{'
                    children = parse_body(end_is_rbrace=True)
                    # pos now points at the matching rbrace (or EOF)
                    if pos < ntok and tokens[pos].kind == "rbrace":
                        body_end = tokens[pos].start
                        line_end = tokens[pos].line
                        pos += 1
                    else:
                        body_end = tokens[pos - 1].end if pos > 0 else body_start
                        line_end = tokens[pos - 1].line if pos > 0 else line_start
                    blk = Block(
                        type=btype,
                        labels=labels,
                        line_start=line_start,
                        line_end=line_end,
                        char_start=char_start,
                        body_start=body_start,
                        body_end=body_end,
                        children=children,
                    )
                    for ch in children:
                        ch.parent = blk
                    blocks.append(blk)
                    header = []
                    saw_eq = False
                    continue
                else:
                    # object-literal expression value: skip to matching '}'
                    _skip_container("lbrace")
                    header = []
                    saw_eq = False
                    continue

            # any other token
            pos += 1
            continue

        return blocks

    def _skip_container(open_kind: str) -> None:
        """Skip a balanced (), [] or {} starting at tokens[pos] (the opener)."""
        nonlocal pos
        pairs = {"lparen": "rparen", "lbracket": "rbracket", "lbrace": "rbrace"}
        openers = set(pairs.keys())
        closers = set(pairs.values())
        depth = 0
        while pos < ntok:
            k = tokens[pos].kind
            if k in openers:
                depth += 1
            elif k in closers:
                depth -= 1
                if depth == 0:
                    pos += 1
                    return
            pos += 1

    top = parse_body(end_is_rbrace=False)
    return top, clean


def iter_blocks(blocks: List[Block]):
    """Depth-first iteration over a block tree."""
    for b in blocks:
        yield b
        yield from iter_blocks(b.children)
