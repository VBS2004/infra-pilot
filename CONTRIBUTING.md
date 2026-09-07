# Contributing to terra-pilot

Thank you for your interest in contributing to terra-pilot! 🎉

## How to Contribute

### Reporting Bugs

1. Check [existing issues](https://github.com/vbs2004/terra-pilot/issues) first
2. Open a new issue using the **Bug Report** template
3. Include: Python version, OS, steps to reproduce, expected vs actual behavior

### Suggesting Features

1. Open a new issue using the **Feature Request** template
2. Describe the problem you're trying to solve (not just the solution)
3. If possible, reference how other tools handle similar use cases

### Submitting Code

1. **Fork** the repository
2. **Clone** your fork: `git clone https://github.com/YOUR-USERNAME/terra-pilot.git`
3. **Create a branch**: `git checkout -b feature/my-feature`
4. **Make changes** and ensure tests pass:
   ```bash
   python tests/test_index.py
   python tests/test_planner.py
   python tests/test_borrowed.py
   ```
5. **Commit** with a clear message following [Conventional Commits](https://www.conventionalcommits.org/):
   ```
   feat: add Azure module catalog support
   fix: resolve path resolution for nested env-tiers
   docs: update configuration table with new env vars
   ```
6. **Push** your branch: `git push origin feature/my-feature`
7. **Open a Pull Request** against `main`

## Development Setup

```bash
# Clone and set up
git clone https://github.com/vbs2004/terra-pilot.git
cd terra-pilot

# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # Linux/macOS
# .venv\Scripts\activate   # Windows

# Install dependencies
pip install -r requirements.txt

# Run tests (zero-dep — works without any external services)
python tests/test_index.py
```

## Code Style

- Follow PEP 8
- Use type hints where practical
- Keep functions focused — one job per function
- Add docstrings to public functions and classes
- Preserve existing comments and docstrings unless they're incorrect

## Architecture Guidelines

- **No hardcoded paths or company-specific logic** — terra-pilot must work with any Terragrunt repo
- **Graceful degradation** — if an optional dependency (embeddings, reranker) is unavailable, the pipeline must still produce correct output
- **Environment variables for configuration** — no config files
- **Zero-dep core** — the stdlib parser + BM25 path must always work without pip installs

## Questions?

Open a [Discussion](https://github.com/vbs2004/terra-pilot/discussions) or reach out via issues. We're happy to help!
