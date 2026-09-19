# DECISIONS — open choices for terra-pilot

Each item: the question, the options, a recommendation, and what it costs. Nothing here is decided
unless marked **Decided**. Order is roughly "decide first → decide later".

## Progress since this was written (2026-09-18)

| § | Status |
|---|--------|
| 4 | **Done**: `data` extra, Spark 3.2 tarball deleted, notebook → `data_pipeline/build_metadata.py` + `extract_files.py` (tested; near-dup MinHash still open) |
| 5 | **Decided and done**: refuse + scaffold (`--freeform` opts out); provider-schema fallback still open, do it if §2 shows it matters |
| 6 | **Done**: build system, `terra-pilot` script, no runtime deps, README/classifiers fixed, private names stripped, cache renamed to `~/.terra-pilot`. Open: AWS-only scope statement in the README |
| 7 | **Done**: rerank only with `LLM_RERANK_GATEWAY_BASE`; `auto` embeddings only with `LLM_EMBED_GATEWAY_BASE`. Embeddings still default to "on if a local model exists" |
| 8 | **Partly done**: `--apply` runs the stdlib HCL sanity tier and reports the fmt result; TODO policy is *warn*. Online tiers stay opt-in via `validate.py --online` |
| 9 | **Done**: fake gateway + e2e suite, isolated embedding cache. Open: scheduled `quality_sweep.py` job (needs the data) |
| 10 | **Done** except rotating the DeepSeek key (only you can) |
| 1–3 | Not started. `quality_sweep.py` is a first, decision-level benchmark; §2's generation-level harness is still the next big piece |

## 1. What is this project? (decide first; it gates everything)

- **A. IaC compose tool** — retrieval + LLM over a user's repo. Works today; needs hardening.
- **B. Terraform data/model project** — mine TerraDS, train or fine-tune a model, evaluate it.
- **C. Both, with B feeding A** — the fine-tuned model becomes the generator behind `compose`.

Recommend **C, but sequenced A → eval → B**. The fine-tune has nothing to be measured against until
there is a benchmark, and a benchmark needs a stable A. Cost: B alone would skip the hardening in
§4–§7 and inherit its bugs.

## 2. Build an evaluation harness before any fine-tuning

Without it there is no way to say a tuned model beats prompted DeepSeek + retrieval.

- Held-out TerraDS repos; task = given intent + module schema, produce `inputs`.
- Metrics that need no LLM judge: every emitted key exists in `variables.tf`; all required inputs
  present; values type-check; `terraform validate`/`terragrunt hcl fmt --check` passes; no
  fabricated ARNs/account IDs (regex).
- Baselines: DeepSeek zero-shot, DeepSeek + retrieval (today's system), then the fine-tune.

Recommend doing this first. It also fixes the "generation has no test" gap (STATUS #2). Cost: a few
days of work; the payoff is that every later decision becomes measurable.

## 3. Fine-tuning approach (only after §2)

- **CPT** on raw repo text — `models/cpt_data.py` already builds this JSONL. Cheap to produce,
  weak signal for "follow this schema".
- **SFT on (intent, schema, reference → inputs.hcl) pairs** — derivable from TerraDS module +
  usage pairs. Directly matches the task. Recommend this over CPT.
- **Preference/RL on validity checks** — later, if SFT plateaus; the §2 metrics are the reward.

Hardware: 4GB VRAM only fits QLoRA on ≤~1.5–3B models with short sequences. Options: (a) accept
that and use a small coder model, (b) rent a 24GB GPU for the actual runs and use the laptop for
data prep and eval, (c) skip training and improve retrieval/prompts. Recommend (b) once §2 exists.
Also decide data hygiene up front: license filtering, dedup across the 62k repos (forks are
common), secret scrubbing, and dropping repos that fail to parse.

## 4. Data engineering stack

**Decided: use PySpark.** Getting hands-on Spark exposure is a goal of this project in its own
right, so scale is not the deciding factor. (The metadata alone, 414MB of sqlite, would fit in
DuckDB or pandas; that is not the reason to choose.) Already working: pyspark 4.2.0, Java 17,
and `pyspark_test.ipynb` (license filter → archived/dedupe/size filter → module/resource joins →
parquet export to `data/terrads_*.parquet`).

What is left to decide is how to make that Spark work substantial and repeatable:

- **Put the real workload on the file corpus, not the sqlite metadata.** The genuinely Spark-shaped
  job is `TerraDS_CodeRepos/` (62,407 archives → millions of `.tf`/`.hcl` files): extract, parse
  with `mapInPartitions` or a pandas UDF, content-hash and near-duplicate (MinHash/LSH) dedup, drop
  parse failures, scrub secrets, write partitioned parquet. That is where partitioning, shuffles,
  skew and file-size tuning become real. Recommended as the main learning target.
- **Turn the notebook into a script** (`data_pipeline/`, runnable with `spark-submit` or
  `python`), so the dataset build is reproducible and can feed §2 and §3.
- **Keep pyspark out of the compose tool's core install.** Move `pyspark`, `pandas`, `pyarrow` to a
  `data` extra in `pyproject.toml` (`pip install -e '.[data]'`). Someone who only wants to
  generate Terragrunt should not pull a JVM stack. This is the one change to today's setup.
- **Delete `spark-3.2.0-bin-hadoop3.2.tgz`.** The pip `pyspark` 4.2.0 ships its own Spark, and 3.2
  predates Java 17 support. It is already gitignored; it is just 335MB of dead weight.
- Optional later: Delta Lake or Iceberg tables for versioned dataset snapshots, and a Spark
  structured-streaming or Airflow-style scheduled rebuild if you want the orchestration side too.

## 5. Behaviour when no module matches (net-new)

Today the LLM free-generates and can produce the wrong resource's fields.

- **Provider-schema fallback** — read `terraform providers schema -json` (or a vendored subset) and
  constrain output to the real resource arguments. Correct, more work, needs the provider binary or
  a cached schema.
- **Refuse + scaffold** — emit the `.tf` skeleton only, mark inputs `TODO`. Honest, cheap.
- **Status quo** — keep free generation, warn loudly.

Recommend refuse+scaffold now, provider schema when §2 shows it matters. Cost of status quo:
plausible-looking wrong output, which is worse than an obvious TODO.

## 6. Packaging and scope

- Add `[build-system]` + src-layout config so `pip install -e .` works; drop the `PYTHONPATH=src`
  requirement and the cwd-relative `sys.path` hack in `cli.py`. **Recommended, small.**
- Move `pyspark` to an optional `data` extra, not a core dependency (see §4). Fix stale Python 3.9
  classifier.
- Rewrite README claims to match reality (zero-dep, test counts, roadmap checkboxes).
- Strip private-origin naming (Acme, MiniMax, legacy_coder, payments cache dir). Decide whether the
  `~/.payments-indexer` cache path should be renamed (invalidates existing caches — harmless).
- Cloud scope: AWS is what is exercised. GCP/Azure appear in prompts only. Recommend declaring AWS
  the supported target until §2 exists to measure others.

## 7. Model serving defaults (embedder + reranker)

- Bundled FastAPI reranker: works, simple, ~1.3GB VRAM. Keep as the default local option.
- Infinity / vLLM: better throughput, but `infinity-emb` failed to import against current deps;
  vLLM is heavy for 4GB. Document as "bring your own OpenAI-style server", do not bundle.
- **Change the default:** enable rerank only when `LLM_RERANK_GATEWAY_BASE` is set, instead of
  falling back to the generation gateway (STATUS #4). Small and clearly right.
- Should embeddings default to a local model, or to off? Off keeps the "zero-dep" promise; local
  costs a torch install. Recommend off unless configured, documented as a quality upgrade.

## 8. Safety gates before `--apply`

`utils/validate.py` already models fmt → validate → plan → checkov, but is untested and needs
terragrunt. Decide whether `--apply` should require at least the offline fmt/HCL sanity tier
(recommended, no external deps) and whether the online tiers stay opt-in. Also decide the policy
for `TODO` values: block apply, or warn.

## 9. Test strategy

- Add a fake OpenAI-compatible gateway (stdlib `http.server`) so CI can run `compose()` end to end
  for all three emitters. Recommend — this is the exact hole that hid the `NameError`.
- Keep live-LLM tests opt-in behind an env var, never in default CI.
- Isolate the embedding cache per test run (`EMBED_CACHE_DIR` temp dir) so tests stop depending on
  `~/.payments-indexer`.
- Run the TerraDS sweep on a fixed seed as a scheduled job, not per push.

## 10. Repo hygiene and secrets

- Rotate the DeepSeek key; delete `LOCAL_README.md` and replace with `.env.example` plus a
  README section. **Do this regardless of everything else.**
- Delete `imports_summary.txt`, the empty `terrads.db`, and the Spark 3.2 tarball (§4). Keep
  `pyspark_test.*` until it becomes `data_pipeline/`.
- Decide whether `stress_test.py`, `showcase_test.py`, `pyspark_test.*` move under `scripts/` or
  `tests/` (they were committed at the root).

## 11. Product surface (later)

Web UI, GitHub Action on PR comments, OPA/Sentinel policy checks, drift detection, and LlamaIndex
integration are on the README roadmap. None should start before §2 and §9; a UI on an unmeasured
generator mostly demos its failure modes.

## Suggested order

1. §10 secrets rotation and hygiene (an hour).
2. §6 packaging + §7 rerank default + §9 fake-gateway test (a few days; makes the base trustworthy).
3. §2 evaluation harness.
4. §4 Spark data pipeline on TerraDS (this is the part you want exposure in, so it can start in
   parallel with steps 2 and 3, since it does not depend on the compose code), then §5 and §3
   informed by §2's numbers.
