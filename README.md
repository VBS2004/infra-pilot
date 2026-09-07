<div align="center">
  <img src="docs/assets/logo.jpg" alt="terra-pilot logo" width="200"/>

  # terra-pilot

  **Autopilot for Terraform & Terragrunt — generate production-ready IaC from natural language.**

  [![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
  [![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
  [![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)
  [![GitHub Stars](https://img.shields.io/github/stars/vbs2004/terra-pilot?style=social)](https://github.com/vbs2004/terra-pilot)

  *"Deploy an EC2 for auth-service nonprod, just like in payments"* → production `inputs.hcl` + `terragrunt.hcl` in seconds.

  [Quick Start](#-quick-start) · [How It Works](#-how-it-works) · [Architecture](#-architecture) · [Configuration](#-configuration) · [Roadmap](#-roadmap)

</div>

---

## ✨ What is terra-pilot?

**terra-pilot** is an AI-powered code generation pipeline that composes Terraform/Terragrunt infrastructure from natural language prompts. It doesn't guess — it **reads your existing repo**, learns your patterns, and generates code that fits your conventions perfectly.

Unlike generic AI code generators, terra-pilot uses a **hybrid RAG architecture** with schema enforcement:

- 🔍 **Learns from your repo** — BM25 + dense embeddings + cross-encoder reranking find the best existing precedent
- 🧠 **Schema-enforced generation** — outputs are constrained by your actual `variables.tf` definitions (no hallucinated inputs)
- 🎯 **Deterministic path resolution** — filesystem-grounded decisions, not vibes
- 🔌 **Bring your own LLM** — works with DeepSeek, OpenAI, Anthropic, local models, or any OpenAI-compatible API
- ⚡ **Works offline** — embeddings disabled? BM25 + path-matching still produce correct output (35/35 tests pass offline)

## 🎬 Demo

```bash
$ python cli.py ./my-infra compose "deploy an ec2 for billing nonprod just like in auth"

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
git clone https://github.com/vbs2004/terra-pilot.git
cd terra-pilot

# Zero-dep mode (works immediately — stdlib parser + BM25)
python cli.py examples/sample-repo compose "create an s3 bucket for logging"

# Full mode (tree-sitter + dense retrieval)
pip install -r requirements.txt
```

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

```bash
python cli.py /path/to/your/terraform-repo compose "deploy an ecs service for payments prod"
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
terra-pilot/
├── cli.py                 # CLI entry point
├── compose.py             # Core composition pipeline (65KB of battle-tested logic)
├── intent_parser.py       # LLM-powered natural language → JSON intent
├── planner.py             # Reuse-vs-write decision engine
├── catalog.py             # Module catalog — AST-derived schema for every .tf module
├── retrieval.py           # Orchestrates hybrid search + reference resolution
├── lexical_search.py      # BM25 + dense + reranker + RRF fusion
├── embedder.py            # Pluggable embedding backends (HTTP / local SentenceTransformers)
├── generator.py           # LLM code generation with schema constraints
├── index.py               # Structural code index — symbol table + 5 edge types
├── hcl_parser.py          # Zero-dep stdlib HCL parser (works everywhere)
├── ts_backend.py          # Tree-sitter HCL backend (production accuracy)
├── bootstrap.py           # Project/env scaffolding when target dirs don't exist
├── config.py              # All configuration via environment variables
├── gateway.py             # LLM gateway routing
├── reranker_server.py     # Lightweight local cross-encoder server
└── fixtures/              # Sample Terragrunt repos for testing
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
| `LLM_EMBED_BACKEND` | `auto` | `http` (vLLM/API), `sentence_transformers` (local), `auto` |
| `LLM_EMBED_MODEL` | `jina-embeddings-v3` | Embedding model name |
| `LLM_EMBED_GATEWAY_BASE` | — | Override gateway URL for embeddings only |
| `LLM_LOCAL_EMBED_MODEL` | `BAAI/bge-small-en-v1.5` | Model for local SentenceTransformer backend |

### Reranker (Optional)

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_RERANK_ENABLED` | `1` | Set `0` to skip cross-encoder reranking |
| `LLM_RERANK_GATEWAY_BASE` | — | URL for reranker server |
| `LLM_RERANK_MODEL` | `BAAI/bge-reranker-base` | Cross-encoder model |

### Example: Full Local Stack (Free, No API Costs)

```bash
# Terminal 1: Embeddings via vLLM
vllm serve BAAI/bge-small-en-v1.5 --port 8000

# Terminal 2: Reranker via built-in server
python reranker_server.py  # starts on port 8080

# Terminal 3: Run terra-pilot
export OPENAI_API_KEY="sk-your-deepseek-key"  # only generation needs an API
export LLM_GATEWAY_BASE="https://api.deepseek.com"
export LLM_EMBED_GATEWAY_BASE="http://localhost:8000"
export LLM_RERANK_GATEWAY_BASE="http://localhost:8080"

python cli.py ./your-repo compose "create an rds for analytics prod"
```

## 🔌 CLI Commands

```bash
# Compose infrastructure from natural language
python cli.py <repo> compose "your request here"
python cli.py <repo> compose "your request here" --apply  # write files

# Explore your repo
python cli.py <repo> stats                          # repo statistics
python cli.py <repo> catalog                        # list all reusable modules
python cli.py <repo> search <query>                 # hybrid search
python cli.py <repo> outline <file>                 # file structure
python cli.py <repo> imports <file>                 # dependency graph
python cli.py <repo> related <file>                 # related files
python cli.py <repo> plan <intent>                  # plan without generating
```

## 🧪 Testing

```bash
# Zero-dep tests (run anywhere, no GPU/API needed)
python tests/test_index.py        # 15/15 structural index checks
python tests/test_planner.py      # catalog + planning checks
python tests/test_borrowed.py     # integration checks

# Full pipeline test (requires LLM API key)
python cli.py fixtures/myrepo compose "deploy an ec2 for billing nonprod like auth"
```

## 🗺️ Roadmap

- [x] **Hybrid RAG retrieval** — BM25 + dense embeddings + cross-encoder reranking
- [x] **Schema-enforced generation** — outputs constrained by `variables.tf`
- [x] **Multi-backend LLM support** — DeepSeek, OpenAI, local models
- [x] **Local embedding & reranking** — vLLM + custom reranker server
- [x] **Zero-dep offline mode** — stdlib parser + BM25, no GPU needed
- [ ] **Root schema extraction** — parse `root.hcl` to understand directory conventions automatically
- [ ] **LlamaIndex integration** — pluggable indexing & retrieval framework
- [ ] **Multi-cloud support** — Azure, GCP module catalogs
- [ ] **Web UI** — browser-based compose interface
- [ ] **GitHub Action** — compose infrastructure from PR comments
- [ ] **Policy-as-code** — OPA/Sentinel validation before apply
- [ ] **Drift detection** — compare generated vs actual state

## 🤝 Contributing

We welcome contributions! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

```bash
# Fork → Clone → Branch → PR
git checkout -b feature/my-feature
# make changes
python tests/test_index.py  # make sure tests pass
git commit -m "feat: add my feature"
git push origin feature/my-feature
```

## 📄 License

MIT License — see [LICENSE](LICENSE) for details.

## 🌟 Star History

If terra-pilot saves you time, please consider giving it a ⭐ — it helps others discover the project!

---

<div align="center">
  <b>Built with ❤️ by <a href="https://github.com/vbs2004">Venkat Balaji S</a></b>
  <br/>
  <sub>Originally developed for enterprise-scale Terraform/Terragrunt infrastructure automation.</sub>
</div>
