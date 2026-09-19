# STATUS — terra-pilot

As of 2026-09-18, `master` @ `508009f` plus the uncommitted work described below. Everything marked
**verified** was run, not read. Nothing here has been committed yet.

## Summary

The retrieval/planning core is solid and, for the first time, the generation path is under test.
Writing that test exposed three more defects on the shipped layout (below) that CI had never seen;
all are fixed. The project installs cleanly with zero dependencies. It is still a prototype: no
real LLM or real terragrunt was used in this round of testing, and the fine-tuning direction has not
started.

## What was broken and is now fixed (all verified)

| # | Problem | Fix | Proof |
|---|---------|-----|-------|
| 1 | No-precedent generation invented fields (`ami_id` on RDS). Root cause was two leaks, not one: a semantic-retrieval fallback adopted *any* nearby module, and `compose._find_entry` fell back to any catalog text match, so the EC2 schema was injected into an RDS request | `retrieval.module_relates` guard on both; net-new with no module now **refuses and prints a module scaffold** (`--freeform` opts out, with a warning); an existing component can still be edited | `test_compose_e2e`; `quality_sweep`: 0/100 false reuse on the compose path |
| 2 | Generation had no test | `tests/fake_gateway.py` (stdlib OpenAI-compatible server) + `test_compose_e2e.py` drive the real HTTP generator path for all three emitters | 76 checks |
| 3 | Not installable | `[build-system]` (hatchling), console script `terra-pilot`, no runtime deps, `pyspark` moved to a `data` extra, `local`/`reranker`/`full` extras, 3.9 classifier dropped, README claims rewritten | wheel built and installed in a **clean stdlib-only venv**; `terra-pilot … stats` ran with no `PYTHONPATH` |
| 4 | Rerank (and `auto` embeddings) misrouted to the generation gateway | rerank runs only when `LLM_RERANK_GATEWAY_BASE` is set; `auto` embeddings use HTTP only when `LLM_EMBED_GATEWAY_BASE` is set | `test_live_wiring`; before the fix a plain `plan` hit DeepSeek with 401s |
| 5 | Private-origin leftovers | swept from all source, prompts and docs; `payments-indexer` cache dir → `~/.terra-pilot`; dead `hcl/apply_patch.py` removed; `validate.py` no longer points at an internal registry | `grep` over `src/` is clean |
| 6 | Secret hygiene | key removed from `LOCAL_README.md`; `.env.example` added | **You must still rotate the DeepSeek key** — I cannot. It never entered git history (`git log -S` is empty) |
| 7 | Clutter | deleted the 335 MB Spark 3.2 tarball, empty `terrads.db`, `imports_summary.txt` | — |

### Defects found *by* the new tests (none were in the original list)

- **Every compose on an existing env-tier wrote to `auth/aws/auth_auth_prod/…`** (env resolved to the
  directory name, then the project prefix was added again). This broke `--apply` and made every
  mutation report "component does not exist". Fixed in `paths.env_dir`.
- **`NameError: _COMPLEX_TYPES`** — every deterministic scalar mutation crashed (same class as the
  earlier `_default_generate` loss). Defined.
- **Refused mutations crashed** with `TypeError` (missing `inputs_file_path`). Fixed.
- **`list` returned nothing** on every repo: it hard-coded an `infra/` prefix that does not exist.
- **Tests were order-dependent**: `test_live_wiring` passed once and failed on every later run because
  of a shared embedding cache. Cache is now isolated.
- **Spark job OOM** on the full `Resources` table at the 1 GB default; driver memory is now 4 GB.
- `local_generator` hard-coded `.to("cuda")`; it now picks CUDA or CPU.

## What works (verified)

| Area | Evidence |
|------|----------|
| Test suites | **274 checks, 9 files, all pass** with no env setup. The 7 stdlib-safe files also pass in a venv with nothing installed |
| `compose` end to end | reuse create, dry-run, `--apply`, overwrite rules, NL intent → generation, `--plan-only`, exit codes; against a fake gateway |
| Mutations | deterministic scalar set (zero LLM calls), LLM edit-set `add` with scope guard, no-op refusal, missing-component refusal, `--regen`, edit of a component whose module is missing |
| Write-time safety | truncated output and an unterminated string (braces balanced) are both refused; TODO placeholders warn |
| Emitters | `terragrunt`, `tf-modules`, `tf-flat` each generate through the gateway and write |
| `validate.py` | fmt → sanity → module lint → validate/plan/show/checkov ordering and short-circuit, driven by fake binaries; `--apply` reports the fmt result |
| Inventory & graph commands | `list` (explicit, NL, `--where`, `!=`, nested-key warning), `outline`, `find`, `imports`, `importers`, `related`, `search`, `tool`, `plan`, `livecheck` |
| Data tooling | `cpt_data` (grouping, chunking, skips); local generator on a tiny random GPT-2 (CPU, deterministic, routes compose off the gateway) |
| Spark | pipelines unit-tested on synthetic data; **`build_metadata` on the real DB reproduces the notebook's 50,769 licensed repos** (→ 48,857 active → 44,188 sized → 218,957 modules → 1,398,461 resources, 22 s); `extract_files` ran on **all 62,407 archives** (1,071,292 files; 1,070,710 parse; 1,068,691 secret-free; 609,417 unique; 5 m 14 s) |
| Decision quality on real repos | `quality_sweep.py`, 100 repos seed 42 / 200 repos seed 7 (AWS ground truth from the repo itself): compose path 95–98% strict, 99.5–100% lenient, **0% false reuse**; `plan` command 94.5–97.5% strict, 100% lenient, 1.0–1.5% false reuse |
| Robustness | `stress_test.py` 72/72; `showcase_test.py --terrads 30` completes |

## What is still weak or unknown

1. **Live LLM verified only lightly.** On 2026-09-19 four real `deepseek-flash` runs (thinking
   disabled, ~$0 of a $0.22 balance) all behaved: reuse create with reference (correct `auth_prod` path,
   values reproduced), deterministic scalar update (zero edit-planner calls), LLM edit-set `add`
   (appended, verified), and net-new refusal. Output *quality* across many requests is unmeasured;
   that is the §2 evaluation harness.
2. **No real terragrunt/terraform/checkov/tflint** on this machine. `validate.py` is verified
   against stand-in binaries (argv, exit codes, ordering), not against the real tools.
3. **`tf-modules` / `tf-flat` placement is evidence-based now, but only heuristically**: it recognises
   `environments/ envs/ env/ live/ stacks/ deployments/` env roots. Exotic layouts (nested stacks,
   workspaces) fall back to `<env>/<component>/main.tf`.
4. **`plan` (not `compose`) still has ~1% false reuse** on ambiguous multi-word requests. The
   compose path is stricter (0%).
5. **Local generator tested only with a random tiny model.** Real 1–3B model quality and the 4 GB
   VRAM fit are unmeasured. `infinity-emb` still fails to import against current deps; it is not a
   dependency, and the docs now say "bring your own OpenAI-style server".
6. **Spark scope**: exact-content dedup only (43% of files were exact duplicates; near-duplicate
   MinHash/LSH is still open), and secret handling drops files rather than scrubbing them.
7. Quality sweep ground truth is AWS-only; other providers are unmeasured.
8. Nothing is committed. `git status` shows 50+ changed/new files.

## Work queue (in progress, started 2026-09-18)

| # | Item | Status |
|---|------|--------|
| A | `tf-modules` / `tf-flat` file placement (weak #3) | **done**: follows the repo's own layout (`envs/dev/main.tf` → new `envs/dev/<component>.tf`; component sub-dirs; flat → `<repo>/<component>.tf`). 300 targets over 150 real repos, none outside the repo |
| B | `plan` reuse rule too loose (weak #4) | **done**: modules scored on resource-type words, generic words ignored, short phrases need half their words covered. Strict 94.5–97.5% (was 91–94%), false reuse 1.0–1.5% |
| C | Lost-symbol lint in CI | **done**: `ruff --select F821,F811,F823` runs before the tests |
| D | Live `compose` with a real key; real terragrunt for `validate.py` | **live compose done** (above). Real terragrunt still not installed |
| E | Full 62k-archive Spark run | **done**: 62,407 archives → 1,071,292 files → 609,417 unique in 5 m 14 s (`data/terrads_files.parquet`, 409 MB). MinHash near-dup still open |
| F | Commit the work in logical chunks | waiting for your go-ahead |

## LLM configuration used

`.env` (gitignored): `LLM_GATEWAY_BASE=https://api.deepseek.com`, `LLM_GEN_MODEL=deepseek-flash`,
`LLM_THINKING=disabled`. New `LLM_THINKING` option forwards `{"thinking": {"type": ...}}`; with thinking
on, small `max_tokens` budgets can return empty content because reasoning tokens consume them.

## Data and environment

- TerraDS in `data/` (gitignored): 62,407 repos, sqlite (`Repositories` 62,406 / `Modules` 279,344 /
  `Resources` 1,773,991 rows), filtered/joined parquet.
- Dev box: Linux, Python 3.10 venv (`uv`), RTX 3050 4 GB, 15 GB RAM. Torch 2.14 + transformers 4.57 in
  the venv. Java 17 for Spark.
- Repo: `github.com/VBS2004/infra-pilot`, default branch `master`. About 8.7k lines of source, 2.1k of
  tests and data pipelines.
- `pyspark_test.py` / `.ipynb` are now superseded by `data_pipeline/` and can be deleted.

## History that matters

`298b253` initial flat layout → `601b304` `src/terra_pilot/` restructure → `d4638db` CI trigger
fixed → `7bd4d10` restored `models/` (gitignore bug) → `305e4d1` reranker BYO-model → `508009f`
restored `_default_generate`. The restructure lost symbols twice (`_default_generate`,
`_COMPLEX_TYPES`); `ruff --select F821` is clean now and worth adding to CI.
