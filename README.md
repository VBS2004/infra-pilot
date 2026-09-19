<div align="center">
  <img src="docs/assets/logo.jpg" alt="terra-pilot logo" width="200"/>

  # terra-pilot

  **Natural-language infrastructure-as-code for Terraform & Terragrunt — grounded in your own repo, with an LLM you choose.**

  [![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
  [![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
  [![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)
  [![CI](https://github.com/VBS2004/terra-pilot/actions/workflows/ci.yml/badge.svg)](https://github.com/VBS2004/terra-pilot/actions/workflows/ci.yml)
  [![GitHub Stars](https://img.shields.io/github/stars/VBS2004/terra-pilot?style=social)](https://github.com/VBS2004/terra-pilot)

  *"Deploy an EC2 for billing nonprod, just like in auth"* → a reviewed `inputs.hcl` + `terragrunt.hcl` that follows your repo's conventions.

  [Quick Start](#-quick-start) · [How It Works](#-how-it-works) · [Architecture](#-architecture) · [Configuration](#-configuration) · [Testing](#-testing) · [Status](#-project-status) · [Roadmap](#-roadmap) · [Contributing](CONTRIBUTING.md)

  <sub>terraform · terragrunt · infrastructure as code · IaC · LLM · RAG · AI code generation · DevOps · platform engineering · DeepSeek · OpenAI-compatible</sub>

</div>

---

## ✨ What is terra-pilot?

**terra-pilot** is an open-source command-line tool that turns a plain-English request into Terraform or Terragrunt code. It composes infrastructure from natural language prompts using any OpenAI-compatible LLM (DeepSeek, OpenAI, Ollama, vLLM, or a local model). It doesn't guess — it **reads your existing repo**, learns your patterns, and generates code that fits your conventions perfectly.

Unlike generic AI code generators, terra-pilot uses a **hybrid RAG architecture** with schema enforcement:

- 🔍 **Learns from your repo** — BM25 + dense embeddings + cross-encoder reranking find the best existing precedent
- 🧠 **Schema-enforced generation** — outputs are constrained by your actual `variables.tf` definitions (no hallucinated inputs)
- 🎯 **Deterministic path resolution** — filesystem-grounded decisions, not vibes
- 🔌 **Bring your own LLM** — works with DeepSeek, OpenAI, Anthropic, local models, or any OpenAI-compatible API
- ⚡ **Works offline** — no embedder, no reranker? BM25 + path-matching still plan and retrieve correctly; the whole test suite runs against a local fake gateway with no network
- 🛑 **Refuses to guess** — if your repo has no module for what you asked (say `rds`), terra-pilot prints a module scaffold and refuses to write, instead of letting an LLM invent inputs (`--freeform` opts out)

## 🎬 Demo

```bash
$ terra-pilot ./my-infra compose "deploy an ec2 for billing nonprod just like in auth"

# intent: {"resource_type": "ec2", "project": "billing", "env": "nonprod",
#          "reference": {"project": "auth", "component": "ec2"}}
#
# semantic retrieval: 8 hits (top: ec2 score=0.94036)
# reference resolved: auth/aws/auth_nonprod/ec2 — using its inputs.hcl as reproduce-base
#
# ===== terragrunt.hcl =====
# include { path = find_in_parent_folders("root.hcl") }
#
# ===== inputs.hcl =====
# inputs = {
#   ami_id             = "ami-0c55b159cbfafe1f0"
#   instance_type      = "t3.medium"
#   subnet_id          = "subnet-0billing001"
#   security_group_ids = ["sg-0billing001"]
#   enable_monitoring  = true
#   ...
# }
#
# dry-run (no files written). Re-run with --apply to write.
```

## 🚀 Quick Start

### 1. Clone & Install

```bash
git clone https://github.com/VBS2004/terra-pilot.git
cd terra-pilot

# Zero-dependency install: stdlib HCL parser + BM25, no third-party packages
pip install -e .
terra-pilot fixtures/myrepo stats            # or, without installing: python cli.py fixtures/myrepo stats

# Better parsing / retrieval (tree-sitter, rank-bm25, numpy, sentence-transformers)
pip install -e ".[full]"
```

Optional extras: `[reranker]` (bundled rerank server), `[data]` (PySpark pipelines over TerraDS, needs Java 17),
`[local]` (air-gapped `LOCAL_MODEL` generation).

### 2. Configure Your LLM

```bash
# Option A: DeepSeek (cheapest, recommended)
export OPENAI_API_KEY="sk-your-deepseek-key"
export LLM_GATEWAY_BASE="https://api.deepseek.com"
export LLM_GEN_MODEL="deepseek-chat"

# Option B: OpenAI
export OPENAI_API_KEY="sk-your-openai-key"
export LLM_GEN_MODEL="gpt-4o"

# Option C: Any OpenAI-compatible API (Ollama, vLLM, LiteLLM, etc.)
export OPENAI_API_KEY="dummy"
export LLM_GATEWAY_BASE="http://localhost:11434"
export LLM_GEN_MODEL="llama3"
```

### 3. Point at Your Repo & Go

See `.env.example` for every variable.

```bash
terra-pilot /path/to/your/terraform-repo compose "deploy an ecs service for billing prod"
```

## 🧠 How It Works

terra-pilot is **not a chatbot** and **not a RAG Q&A system**. It's a **6-stage code generation pipeline** where every stage is grounded in your actual repository:

```
┌─────────────┐    ┌──────────────┐    ┌────────────────┐
│  1. Intent   │───▶│  2. Path     │───▶│  3. Module     │
│    Parse     │    │  Resolution  │    │   Catalog      │
│  (LLM→JSON) │    │ (filesystem) │    │  (AST parse)   │
└─────────────┘    └──────────────┘    └────────────────┘
                                              │
┌─────────────┐    ┌──────────────┐    ┌──────▼─────────┐
│  6. Propose  │◀──│  5. Generate │◀──│  4. Retrieval   │
│  (dry-run/   │   │  (LLM fill   │   │  BM25 + Dense   │
│   --apply)   │   │   inputs)    │   │  + Rerank + RRF │
└─────────────┘    └──────────────┘    └────────────────┘
```

| Stage | What it does | Uses LLM? | Uses Embeddings? |
|-------|-------------|-----------|-----------------|
| **Intent Parse** | Natural language → structured JSON intent | ✅ | ❌ |
| **Path Resolution** | Deterministic filesystem math → target directory | ❌ | ❌ |
| **Module Catalog** | AST-parse `.tf` files → typed schema (required/optional inputs) | ❌ | ❌ |
| **Retrieval** | Find closest existing precedent to use as template | ❌ | ✅ (optional) |
| **Generation** | LLM fills `inputs.hcl` constrained by schema + reference values | ✅ | ❌ |
| **Propose** | Show diff, write files on `--apply` | ❌ | ❌ |

> **Key insight:** Only 1 of 6 stages uses embeddings, and it's optional. The system produces correct output with embeddings **completely disabled**.

## 🏗️ Architecture

```
src/terra_pilot/
├── cli/          # argument dispatch (cli.py at the repo root is a shim)
├── core/         # config (all env vars), convention detection, path resolution, storage, caches
├── hcl/          # stdlib HCL parser + deterministic editors (hcl_edit, hcl_override)
├── llm/          # generator (chat), intent_parser, planner, embedder, gateway discovery,
│                 #   local_generator (air-gapped transformers), agent_tools
├── models/       # root_schema, inventory (`list`), cpt_data (training-data builder)
├── pipeline/     # compose.py orchestrator, bootstrap, emitters/ (terragrunt | plain_tf | flat_tf)
├── search/       # structural index, module catalog, BM25 + dense + rerank retrieval
├── server/       # optional local cross-encoder reranker (FastAPI)
└── utils/        # validate (fmt / sanity / plan / checkov gate), livecheck, repo_scan
data_pipeline/    # PySpark jobs over the TerraDS corpus (metadata + file extraction)
fixtures/         # sample repos used by the tests
tests/            # offline suites, including a fake OpenAI-compatible gateway
```

## ⚙️ Configuration

All configuration is via environment variables. **Zero config files to manage.**

### LLM Backend

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENAI_API_KEY` | — | API key for your LLM provider |
| `LLM_GATEWAY_BASE` | `https://api.deepseek.com` | Base URL of OpenAI-compatible API |
| `LLM_GEN_MODEL` | `deepseek-chat` | Model for intent parsing + code generation |

### Embeddings (Optional)

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_EMBED_ENABLED` | `1` | Set `0` to disable dense retrieval (BM25-only) |
| `LLM_EMBED_BACKEND` | `auto` | `http` (vLLM/API), `sentence_transformers` (local), `none`, `auto` (HTTP only if `LLM_EMBED_GATEWAY_BASE` is set, else local, else BM25-only) |
| `LLM_EMBED_MODEL` | `jina-embeddings-v3` | Embedding model name |
| `LLM_EMBED_GATEWAY_BASE` | — | Embedding server URL (never the generation gateway, unless `LLM_EMBED_BACKEND=http`) |
| `LLM_LOCAL_EMBED_MODEL` | `all-MiniLM-L6-v2` | Any sentence-transformers model id for the local backend |

### Reranker (Optional)

Any OpenAI-rerank-API-compatible server works here — the bundled `reranker_server.py`
is one option, but so is a hosted service or your own.

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_RERANK_ENABLED` | `1` | Set `0` to force-disable. Reranking only runs when `LLM_RERANK_GATEWAY_BASE` is set |
| `LLM_RERANK_GATEWAY_BASE` | — | Base URL of the rerank server (required to enable reranking) |
| `LLM_RERANK_MODEL` | — | Model name sent in the `/rerank` request; auto-discovered via `/v1/models` if unset |

Running the bundled local reranker server (`reranker_server.py`) yourself? It reads
its own env vars to pick which model to load:

| Variable | Default | Description |
|----------|---------|-------------|
| `RERANK_MODEL` | `BAAI/bge-reranker-base` | Any sentence-transformers CrossEncoder model id |
| `RERANK_HOST` | `0.0.0.0` | Bind host |
| `RERANK_PORT` | `8080` | Bind port |

### Example: Full Local Stack (Free, No API Costs)

```bash
# Terminal 1: Embeddings via vLLM
vllm serve BAAI/bge-small-en-v1.5 --port 8000

# Terminal 2: Reranker via built-in server
python reranker_server.py  # starts on port 8080 (pip install -e '.[reranker]')

# Terminal 3: Run terra-pilot
export OPENAI_API_KEY="sk-your-deepseek-key"  # only generation needs an API
export LLM_GATEWAY_BASE="https://api.deepseek.com"
export LLM_EMBED_GATEWAY_BASE="http://localhost:8000"
export LLM_RERANK_GATEWAY_BASE="http://localhost:8080"

terra-pilot ./your-repo compose "create an rds for analytics prod"
```

## 🔌 CLI Commands

```bash
# Compose infrastructure from natural language
terra-pilot <repo> compose "your request here"            # dry-run: prints files + diff
terra-pilot <repo> compose "your request here" --apply    # write (fmt/HCL sanity checked first)
terra-pilot <repo> compose --resource-type ec2 --project billing --env nonprod [--like "auth ec2"]
#   --plan-only  decision + module mapping, no LLM call
#   --force      overwrite an existing inputs.hcl
#   --regen      after a verified edit, also re-run whole-file generation
#   --freeform   allow generation when the repo has no module for the component (unverified fields)

# Explore your repo (no LLM)
terra-pilot <repo> stats                            # repo statistics
terra-pilot <repo> catalog                          # list all reusable modules
terra-pilot <repo> search <query>                   # hybrid search
terra-pilot <repo> find <symbol> [--kind resource]  # symbol lookup
terra-pilot <repo> outline <file>                   # file structure
terra-pilot <repo> imports <file>                   # what a file depends on
terra-pilot <repo> importers <module-dir>           # what depends on a module
terra-pilot <repo> related <file>                   # related files
terra-pilot <repo> plan <intent>                    # reuse-vs-write decision
terra-pilot <repo> list ec2 --project billing --where instance_type=t3.large   # component inventory
terra-pilot <repo> livecheck                        # gateway connectivity check
```

## 🧪 Testing

Everything runs offline. `compose()` is tested end to end against a stdlib fake OpenAI-compatible
gateway (`tests/fake_gateway.py`), and external tools (terragrunt, checkov, ...) are faked with
shell scripts.

```bash
pip install -e .
for f in tests/test_*.py; do python "$f"; done
```

| Suite | Covers |
|-------|--------|
| `test_index` / `test_planner` / `test_root_schema` / `test_borrowed` | parser, index, catalog, planner, agent tools |
| `test_compose_e2e` | reuse create, `--apply`, net-new refusal, edit-set mutations, `--regen`, write-time safety, all three emitters |
| `test_commands` | outline / find / imports / importers / related / search / list / livecheck, env-dir resolution |
| `test_validate` | fmt → sanity → module lint → validate/plan/checkov gate |
| `test_live_wiring` | embed/rerank HTTP request shapes and default routing |
| `test_data_tools` | CPT dataset, local generator (tiny model), PySpark pipelines (skip if torch / Java are absent) |

Bigger checks that need the TerraDS data: `python stress_test.py` (edge cases + random repos),
`python quality_sweep.py` (are the reuse/write *decisions* right on 100+ real repos?).

## 📌 Project status

terra-pilot is a **working prototype**, not a released product. What is verified and what is not is
tracked, with evidence, in [STATUS.md](STATUS.md); open design choices are in
[DECISIONS.md](DECISIONS.md), and [AGENTS.md](AGENTS.md) is the map of the codebase.

- Verified: the full offline test suite (fake LLM gateway), real `terragrunt`/`terraform`/`tflint`/`tfsec`/`checkov`
  runs, a real-AWS-provider plan of generated output (opt-in), live `deepseek-flash` runs, and reuse-vs-write
  decisions on 200 real GitHub Terraform repos (0% false reuse on the compose path).
- Not yet done: measuring generation *quality* across many requests (an evaluation harness),
  non-AWS providers, and a fine-tuned model.

## 🗺️ Roadmap

- [x] **Hybrid RAG retrieval** — BM25 + dense embeddings + cross-encoder reranking
- [x] **Schema-enforced generation** — outputs constrained by `variables.tf`
- [x] **Multi-backend LLM support** — DeepSeek, OpenAI, local models
- [x] **Local embedding & reranking** — vLLM + custom reranker server
- [x] **Zero-dep offline mode** — stdlib parser + BM25, no GPU needed
- [x] **Root schema extraction** — parse `root.hcl` to understand directory conventions automatically
- [ ] **LlamaIndex integration** — pluggable indexing & retrieval framework
- [ ] **Multi-cloud support** — Azure, GCP module catalogs
- [ ] **Web UI** — browser-based compose interface
- [ ] **GitHub Action** — compose infrastructure from PR comments
- [ ] **Policy-as-code** — OPA/Sentinel validation before apply
- [ ] **Drift detection** — compare generated vs actual state

## 🤝 Contributing

We welcome contributions! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

```bash
git checkout -b feature/my-feature
# make changes
for f in tests/test_*.py; do python "$f"; done
git commit -m "feat: add my feature"
```

## 📄 License

MIT License — see [LICENSE](LICENSE) for details.

## 🌟 Support

If terra-pilot saves you time, a ⭐ helps others find it. Bug reports and PRs are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

---

<div align="center">
  <b>Built with ❤️ by <a href="https://github.com/vbs2004">Venkat Balaji S</a></b>
</div>
