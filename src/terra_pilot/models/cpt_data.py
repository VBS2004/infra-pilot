#!/usr/bin/env python3
"""
CPT Dataset Generator for Qwen2.5-Coder (7B/14B-Instruct)

Context window: 128k tokens (~500k chars) — essentially no single file needs chunking.

Strategy:
  - Group related files by directory (module/component) into ONE training example.
    This teaches the model the relationship between variables.tf, main.tf,
    outputs.tf, and inputs.hcl.
  - Individual standalone files (.tfvars, root.hcl, config.yml, etc.) get their
    own example.
  - Intelligent chunking ONLY fires if a group exceeds MAX_CHARS — splits at
    depth-0 `}` boundaries so every chunk is syntactically valid HCL/Terraform.
  - Skips: .git, .terraform, terragrunt.hcl (generated, not source),
    node_modules, __pycache__

Usage:
  python cpt_data.py <repo_root> <output.jsonl> [--max-chars 400000] [--no-grouping]

Output format (JSONL):
  {"text": "# file: infra/.../variables.tf\nvariable ... {...}\n\n# file: infra/.../main.tf\n..."}
"""

import json
import os
import sys
import argparse
from pathlib import Path
from collections import defaultdict

# ── Config ────────────────────────────────────────────────────────────────

# 400k chars ≈ 100k tokens — leaves headroom in a 128k context window
# for any system prompt / formatting overhead during training.
DEFAULT_MAX_CHARS = 400_000

# Files below this are skipped (too small to be useful training data)
MIN_CHARS = 15

# Extensions we collect
VALID_EXTS = {".tf", ".tfvars", ".hcl", ".yml", ".yaml"}

# Files we always skip (by name)
# NOTE: terragrunt.hcl is generated config, not source — remove from this set
# if you want it included in groups with its sibling files.
SKIP_FILES = {
    "terragrunt.hcl",
    ".terraform.lock.hcl",
    ".gitignore",
    "README.md",
}

# Directories we skip
SKIP_DIRS = {
    ".git", ".terraform", "node_modules", "__pycache__",
    ".terragrunt-cache", ".DS_Store", "venv", ".venv",
}

# When grouping, these filenames in the same directory are bundled together.
# Files NOT in this set (e.g., config.yml, root.hcl, *.tfvars) become standalone examples.
GROUPABLE_FILES = {
    "variables.tf", "main.tf", "outputs.tf", "versions.tf",
    "providers.tf", "locals.tf", "data.tf", "iam.tf",
    "inputs.hcl",
}

# ── Smart Windowing (rarely needed with 128k context) ─────────────────────

def _count_braces(text: str) -> int:
    """Net brace depth change in a line, ignoring braces inside strings/comments."""
    stripped = []
    in_string = False
    escape = False
    in_block_comment = False
    in_line_comment = False
    i = 0
    while i < len(text):
        ch = text[i]
        if escape:
            stripped.append(ch)
            escape = False
            i += 1
            continue
        if ch == '\\':
            stripped.append(ch)
            escape = True
            i += 1
            continue
        if in_string:
            if ch == '"':
                in_string = False
            stripped.append(ch)
            i += 1
            continue
        if ch == '"':
            in_string = True
            stripped.append(ch)
            i += 1
            continue
        if in_block_comment:
            if ch == '*' and i + 1 < len(text) and text[i + 1] == '/':
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        if in_line_comment:
            if ch == '\n':
                in_line_comment = False
            i += 1
            continue
        if ch == '#':
            in_line_comment = True
            i += 1
            continue
        if ch == '/' and i + 1 < len(text) and text[i + 1] == '*':
            in_block_comment = True
            i += 2
            continue
        if ch == '/' and i + 1 < len(text) and text[i + 1] == '/':
            in_line_comment = True
            i += 2
            continue
        stripped.append(ch)
        i += 1

    clean = ''.join(stripped)
    return clean.count('{') - clean.count('}')


def smart_chunk(text: str, max_chars: int) -> list:
    """
    Split text into chunks at depth-0 `}` boundaries.
    Every chunk is syntactically valid (balanced braces).
    Only fires if text exceeds max_chars.
    """
    if len(text) <= max_chars:
        return [text]

    lines = text.splitlines(keepends=True)
    chunks = []
    current = []
    current_len = 0
    depth = 0

    for line in lines:
        current.append(line)
        current_len += len(line)
        depth += _count_braces(line)

        # Split at depth-0 boundary if we've accumulated enough
        if depth == 0 and current_len >= max_chars:
            chunk_text = ''.join(current).rstrip()
            if chunk_text:
                chunks.append(chunk_text)
            current = []
            current_len = 0

    # Don't drop the tail
    tail = ''.join(current).rstrip()
    if tail:
        chunks.append(tail)

    return chunks


# ── File Collection ──────────────────────────────────────────────────────

def collect_files(repo_root: str) -> list:
    """
    Walk repo, collect .tf/.hcl/.tfvars/.yml/.yaml files.
    Returns list of (relative_path, content) tuples.
    """
    results = []
    repo = Path(repo_root)

    for root, dirs, files in os.walk(repo):
        # Filter out skip dirs in-place (prunes the walk)
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]

        for fname in sorted(files):
            if fname in SKIP_FILES:
                continue

            ext = os.path.splitext(fname)[1]
            if ext not in VALID_EXTS:
                continue

            fpath = os.path.join(root, fname)
            try:
                with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
                    content = f.read()
            except Exception as e:
                print(f"  [SKIP] Cannot read {fpath}: {e}", file=sys.stderr)
                continue

            if len(content.strip()) < MIN_CHARS:
                continue

            rel_path = os.path.relpath(fpath, repo_root)
            results.append((rel_path, content))

    return results


# ── Grouping ─────────────────────────────────────────────────────────────

def group_files(files: list, max_chars: int) -> list:
    """
    Group files from the same directory so the model sees them together.
    - Files in the same directory → one group (teaches relationships)
    - Standalone files (config.yml, root.hcl, *.tfvars, etc.) → own group
    - If a group exceeds max_chars, split via smart_chunk
    """
    dir_groups = defaultdict(list)
    standalone = []

    for rel_path, content in files:
        parent_dir = os.path.dirname(rel_path)
        filename = os.path.basename(rel_path)

        if filename in GROUPABLE_FILES and parent_dir:
            dir_groups[parent_dir].append((rel_path, content))
        else:
            standalone.append((rel_path, content))

    result = []

    # Grouped files (same directory → one training example)
    for dir_path, file_list in sorted(dir_groups.items()):
        # Sort by canonical order: variables.tf first, main.tf, then alphabetical
        order = {"variables.tf": 0, "main.tf": 1, "outputs.tf": 2,
                "versions.tf": 3, "providers.tf": 4, "locals.tf": 5,
                "data.tf": 6, "iam.tf": 7, "inputs.hcl": 8}
        file_list.sort(key=lambda x: order.get(os.path.basename(x[0]), 99))

        combined_text = "\n\n".join(
            f"# file: {rp}\n{c.rstrip()}" for rp, c in file_list
        )
        combined_len = len(combined_text)

        if combined_len <= max_chars:
            result.append((dir_path, combined_text))
        else:
            # Smart chunk the combined file
            chunks = smart_chunk(combined_text, max_chars)
            for i, chunk in enumerate(chunks):
                if len(chunks) > 1:
                    result.append((f"{dir_path}::part{i}", chunk))
                else:
                    result.append((dir_path, chunk))

    # Standalone files (one per example)
    for rel_path, content in standalone:
        chunks = smart_chunk(content, max_chars)
        for i, chunk in enumerate(chunks):
            if len(chunks) > 1:
                result.append((f"{rel_path}::part{i}", chunk))
            else:
                result.append((rel_path, chunk))

    return result


# ── Dataset Builder ─────────────────────────────────────────────────────

def build_dataset(repo_root: str, output_path: str, max_chars: int, no_grouping: bool) -> int:
    """Walk repo, build JSONL CPT dataset."""
    print(f"\n{'='*60}")
    print(f"CPT Dataset Generator")
    print(f"{'='*60}")
    print(f"  Repo:       {repo_root}")
    print(f"  Output:     {output_path}")
    print(f"  Max chars:  {max_chars:,} (estimated {max_chars//4:,} tokens)")
    print(f"  Grouping:   {'OFF' if no_grouping else 'ON (files in same dir bundled)'}")
    print(f"  Context:    128k tokens (Qwen2.5-Coder-7B/14B-Instruct)")
    print(f"{'='*60}\n")

    files = collect_files(repo_root)
    print(f"  Collected {len(files)} files")

    if no_grouping:
        # Each file is its own example (still smart-chunked if huge)
        groups = []
        for rp, c in files:
            chunks = smart_chunk(c, max_chars)
            for i, chunk in enumerate(chunks):
                if len(chunks) > 1:
                    groups.append((f"{rp}::part{i}", f"# file: {rp}::part{i}\n{chunk}"))
                else:
                    groups.append((rp, f"# file: {rp}\n{chunk}"))
    else:
        groups = group_files(files, max_chars)

    print(f"  Built {len(groups)} training examples")

    total_chars = 0
    count = 0

    with open(output_path, 'w') as fout:
        for label, text in groups:
            total_chars += len(text)
            fout.write(json.dumps({"text": text}) + '\n')
            count += 1

    print(f"\n  Dataset: {count} examples")
    print(f"  Total chars: {total_chars:,}")
    print(f"  Total tokens (est): {total_chars // 4:,}")
    print(f"  Mean tokens/example: {(total_chars // 4) // max(count, 1):,}")
    print(f"\n  Output written to: {output_path}\n")

    return count


# ── CLI ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="CPT Dataset Generator for Qwen2.5-Coder")
    parser.add_argument("repo_root", help="Path to the CNOF repo root")
    parser.add_argument("output", help="Output JSONL file path")
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS,
                        help=f"Max chars per training example (default: {DEFAULT_MAX_CHARS:,})")
    parser.add_argument("--no-grouping", action="store_true",
                        help="Don't group files by directory — each file is one example")
    args = parser.parse_args()

    build_dataset(args.repo_root, args.output, args.max_chars, args.no_grouping)


if __name__ == "__main__":
    main()
