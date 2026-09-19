# AGENTS.md — how to read and work in this repo

terra-pilot turns a plain-English infra request into Terraform/Terragrunt code, grounded in the
target repo's own modules. This file is the map. For current health see `STATUS.md`; for open
choices see `DECISIONS.md`.

## Run things (from the repo root; `cli.py` finds `./src` itself, or use the installed `terra-pilot`)

```bash
.venv/bin/python3 cli.py fixtures/myrepo stats      # index summary, no LLM
.venv/bin/python3 cli.py fixtures/myrepo catalog    # reusable modules + schemas, no LLM
.venv/bin/python3 cli.py fixtures/myrepo search "ec2 instance"
.venv/bin/python3 cli.py fixtures/myrepo plan "deploy an ec2 for billing nonprod"  # no LLM
.venv/bin/python3 cli.py fixtures/myrepo compose "..."   # needs an LLM key; dry-run unless --apply
```

Tests (all offline; no env setup needed; CI runs exactly these):

```bash
for f in tests/test_*.py; do .venv/bin/python3 "$f"; done
```

`compose()` is tested against `tests/fake_gateway.py`, a stdlib OpenAI-compatible server. New
generation behaviour needs a case in `tests/test_compose_e2e.py`. Import `tests/harness.py` first in
a new test: it sets hermetic env vars before `terra_pilot.core.config` reads them.

Bigger checks (need `data/`): `stress_test.py` (edge cases + random TerraDS repos), `showcase_test.py
[--terrads N]`, `quality_sweep.py` (are reuse/write decisions right? exits 1 below thresholds).

## The pipeline, in code order

`cli/cli.py` → `pipeline/compose.py` is the orchestrator. Each stage lives in one place:

| # | Stage | File | LLM? |
|---|-------|------|------|
| 1 | NL → JSON intent (destination vs. "like X" reference) | `llm/intent_parser.py` | yes |
| 2 | Detect repo style: `terragrunt` / `tf-modules` / `tf-flat` | `core/convention.py` | no |
| 3 | (project, env, component) → directories | `core/paths.py`, `models/root_schema.py` | no |
| 4 | Parse HCL → symbols + 5 edge types | `hcl/hcl_parser.py` (stdlib) or `utils/ts_backend.py` (tree-sitter), `search/index.py` | no |
| 5 | Module catalog: required/optional inputs from `variables.tf` | `search/catalog.py` | no |
| 6 | Reuse-vs-write decision + scaffold (net-new → `compose` refuses and prints the scaffold) | `llm/planner.py`, `search/retrieval.py` (`module_relates` guard) | no |
| 7 | Grounding retrieval: BM25 + dense + rerank, RRF-fused | `search/retrieval.py`, `search/lexical_search.py` | no |
| 8 | Missing project/env scaffolding | `pipeline/bootstrap.py` | no |
| 9 | Generate/edit `inputs.hcl` | `pipeline/compose.py` + `pipeline/emitters/*` + `llm/generator.py` | yes |
| 10 | Splice edits into existing files | `hcl/hcl_edit.py`, `hcl/hcl_override.py` | no |

Note `llm/planner.py` is the deterministic reuse/write planner despite living under `llm/`; the LLM
calls all go through `llm/generator.py` (`complete`, `complete_json`).

## Package map (`src/terra_pilot/`)

- `cli/` — argument dispatch. `main.py` is a thin entry.
- `core/` — `config.py` (all env vars), `convention.py`, `paths.py`, `storage.py`, `persistence.py`
  (embedding store), `file_cache.py`.
- `hcl/` — parsers and editors (`hcl_edit`, `hcl_override`).
- `llm/` — `generator.py` (chat), `embedder.py` (HTTP or local sentence-transformers),
  `gateway.py` (served-model-id discovery via `GET /v1/models`), `local_generator.py` (air-gapped
  transformers on CUDA or CPU, enabled by `LOCAL_MODEL`), `agent_tools.py` (string-returning `tf_*` helpers).
- `models/` — misc domain code: `root_schema.py`, `inventory.py` (deterministic `list` queries),
  `cpt_data.py` (builds a continued-pretraining JSONL from a repo — the fine-tuning entry point).
- `pipeline/emitters/` — one strategy per convention (`terragrunt.py`, `plain_tf.py`, `flat_tf.py`)
  with system prompts in `prompts/*.txt`. Chosen by `get_emitter(convention.kind)`.
- `search/` — index, catalog, lexical/dense/rerank, retrieval.
- `server/reranker_server.py` — optional local cross-encoder over FastAPI (`/v1/rerank`, `/v1/models`).
- `utils/` — `validate.py` (fmt/sanity/validate/plan/checkov gate; `hcl_text_problems` also guards `--apply`), `livecheck.py`, `repo_scan.py`, `debug_ref.py`.

Top-level `cli.py` and `reranker_server.py` are shims into the package.

## Configuration (env vars only; source of truth is `core/config.py`; template in `.env.example`)

- Generation: `OPENAI_API_KEY`, `LLM_GATEWAY_BASE` (default DeepSeek), `LLM_GEN_MODEL`.
- Embeddings: `LLM_EMBED_ENABLED`, `LLM_EMBED_BACKEND` (`auto|http|sentence_transformers|none`),
  `LLM_EMBED_MODEL`, `LLM_EMBED_GATEWAY_BASE`, `LLM_LOCAL_EMBED_MODEL` (default `all-MiniLM-L6-v2`).
  `auto` uses HTTP only when `LLM_EMBED_GATEWAY_BASE` is set; it never probes the generation gateway.
- Rerank: runs only when `LLM_RERANK_GATEWAY_BASE` is set (`LLM_RERANK_ENABLED=0` force-disables),
  model via `LLM_RERANK_MODEL`. Server-side: `RERANK_MODEL`, `RERANK_HOST`, `RERANK_PORT`.
- With no embedder/reranker configured the pipeline degrades to BM25-only and still works.
- Caches: `~/.terra-pilot/` (index storage) and `EMBED_CACHE_DIR` (default `~/.terra-pilot/embed_cache`).

## Data (not in git)

`data/` is gitignored. It holds TerraDS: `TerraDS_CodeRepos/` (62,407 repo archives),
`TerraDS.sqlite` (tables `Repositories`, `Modules`, `Resources`), and two parquet exports.
Spark work is deliberate (a learning goal, not incidental) and lives in `data_pipeline/`:
`python -m data_pipeline.build_metadata` (sqlite → filtered/joined parquet, ~20 s) and
`python -m data_pipeline.extract_files --limit N` (archives → deduped, parse-checked, secret-free
`.tf/.hcl` rows). Both use pip `pyspark` 4.2 + Java 17 (`pip install -e '.[data]'`); the session
sets the driver memory, `PYTHONPATH` for workers, and drops any system `SPARK_HOME`.
`pyspark_test.*` are the old notebook and can be deleted. Do not read `data/` into context; sample it.

## Gotchas

- Env-tier directories are `<project>_<env>`. `paths.env_dir` accepts either the short word
  (`prod`) or the full directory name (`auth_prod`); passing the full name used to double-prefix it.
- `compose` on a component with no module **refuses** (net-new). That is deliberate, not a bug;
  `--freeform` / `allow_freeform=True` overrides it.
- `tf-modules` / `tf-flat` write to `<repo>/<env>/<component>/main.tf`; that placement is naive.
- `.gitignore` uses `/models/` (anchored). An unanchored `models/` once hid `src/terra_pilot/models`.
- The restructure into `src/` silently lost symbols twice (`_default_generate`, `_COMPLEX_TYPES`).
  After moving code, run `uvx ruff check --select F821 src` and the e2e suite.
- Docstrings may still mention "Jina" / "Qwen" in a technical sense (token limits, chat templates);
  the private-origin names (Acme, MiniMax, legacy_coder, payments gateway) are gone.
- Never commit `LOCAL_README.md` edits containing keys, a `.env`, or anything under `data/`.
