## What and why

<!-- One or two sentences. Link the issue if there is one. -->

## How it was tested

<!-- Which test files cover this? Paste the relevant output if useful. -->

## Checklist

- [ ] Tests added or updated, and `for f in tests/test_*.py; do python "$f"; done` passes
- [ ] `ruff check --select F821,F811,F823 src tests data_pipeline cli.py` passes
- [ ] Docs updated if behaviour or configuration changed (`README.md`, `.env.example`)
- [ ] `STATUS.md` updated if this changes what is verified
- [ ] No secrets and nothing from `data/` included
