# Contributing to terra-pilot

Thanks for helping! Bug reports, docs fixes, tests and features are all welcome. This file is the
short path to a merged PR. For a map of the code read [AGENTS.md](AGENTS.md); for what is verified
and what is not, [STATUS.md](STATUS.md); for open design questions, [DECISIONS.md](DECISIONS.md).

## Quick start

```bash
git clone https://github.com/VBS2004/terra-pilot.git
cd terra-pilot
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[full]"                              # core has zero dependencies; extras are optional

for f in tests/test_*.py; do python "$f"; done        # offline, no API key, no network
```

Try it without an LLM: `terra-pilot fixtures/myrepo stats`, `catalog`, `plan "ec2 instance"`.
Copy `.env.example` if you want to run `compose` against a real model.

## Ways to contribute

| You want to… | Do this |
|---|---|
| Report a bug | Open an issue with the **Bug Report** template: version, OS, exact command, expected vs actual. Security problems: see [SECURITY.md](SECURITY.md) instead. |
| Suggest a feature | Open an issue with the **Feature Request** template. Describe the problem, not just the solution. |
| Fix something small | Fork, branch, PR. No issue needed for typos and obvious fixes. |
| Take something bigger | Comment on (or open) an issue first so we agree on the approach. Good starting points are the "Not yet done" items in [STATUS.md](STATUS.md): per-convention file placement for plain Terraform, MinHash near-duplicate removal in `data_pipeline/`, non-AWS providers, an evaluation harness. |

## Pull requests

1. Branch from `master`: `git checkout -b fix/short-description`.
2. Make the change **with a test** (see below).
3. Run the whole suite and the lint:
   ```bash
   for f in tests/test_*.py; do python "$f"; done
   pip install ruff && ruff check --select F821,F811,F823 src tests data_pipeline cli.py
   ```
4. Commit with [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `test:`, `docs:`, `refactor:`, `chore:`), one logical change per commit.
5. Open a PR against `master` and fill in the template. CI runs Python 3.10–3.12.

PR checklist: tests added or updated · suite green · docs updated if behaviour or config changed ·
no secrets, no files from `data/` · `STATUS.md` updated if you changed what is verified.

## Tests: what to add where

Everything runs offline. LLM calls go to `tests/fake_gateway.py`, a stdlib OpenAI-compatible server,
and external tools (terragrunt, checkov, ...) are faked with tiny shell scripts.

| Change | Test file |
|---|---|
| `compose` / emitters / mutations / writing files | `tests/test_compose_e2e.py` |
| CLI commands (`list`, `search`, `importers`, ...) and path resolution | `tests/test_commands.py` |
| `utils/validate.py` (fmt, sanity, lint, plan, checkov) | `tests/test_validate.py` |
| Parser, index, catalog, planner | `tests/test_index.py`, `tests/test_planner.py`, `tests/test_borrowed.py` |
| Embedding/rerank HTTP behaviour and defaults | `tests/test_live_wiring.py` |
| `cpt_data`, local generator, Spark pipelines | `tests/test_data_tools.py` |

Start every new test file with `import harness` (it sets hermetic env vars before the config is read).

Optional checks that need more than the default install (each skips itself when unavailable):

- Real tools: install `terragrunt`, `terraform`, `tflint`, `tfsec`, `checkov` and `test_validate.py` exercises them.
- Real AWS provider (about 700 MB download, dummy credentials, no account touched):
  `TP_REAL_AWS=1 python tests/test_real_aws_e2e.py`
- Real repos: with the TerraDS data in `data/`, `python stress_test.py` and
  `python quality_sweep.py` measure robustness and reuse-vs-write accuracy.

## Code guidelines

- Match the surrounding style; PEP 8, type hints where practical, small focused functions.
- **No company-specific names, paths or logic.** It must work on any Terraform/Terragrunt repo.
- **Degrade gracefully.** Without an embedder or reranker the pipeline must still work (BM25 only).
- **Zero-dependency core.** `pip install -e .` alone must run everything except the optional features.
- **Configuration is environment variables** (`core/config.py` is the source of truth; update `.env.example`).
- **Never let an LLM guess silently.** Unverifiable output is refused or flagged, not written.
- After moving or renaming code, run the lint above: undefined names have slipped through a refactor twice.

## Secrets and data

Never commit API keys, `.env`, or anything under `data/`. If a key leaks, rotate it; deleting the
commit is not enough.

## Conduct

Be kind and assume good faith. Harassment and personal attacks are not tolerated; maintainers may
remove comments or contributions that cross the line.

## Questions?

Open a [Discussion](https://github.com/VBS2004/terra-pilot/discussions) (or an issue if Discussions
are off). A short reproduction gets the fastest answer.
