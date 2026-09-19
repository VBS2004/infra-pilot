import os

# ---------------------------------------------------------------------------
# Gateway / LLM backend
# ---------------------------------------------------------------------------

# Base URL of any OpenAI-compatible API gateway.
# Defaults to DeepSeek. Override with LLM_GATEWAY_BASE.
GATEWAY_BASE = os.environ.get("LLM_GATEWAY_BASE", "https://api.deepseek.com")

# API key (Bearer token).
API_KEY = os.environ.get("OPENAI_API_KEY", "")

# The model name to send in chat completion payloads.
# e.g. "deepseek-chat", "deepseek-reasoner", "gpt-4o", "qwen2.5-coder-32b"
GEN_MODEL = os.environ.get("LLM_GEN_MODEL", "deepseek-chat")

# Optional reasoning control, sent as {"thinking": {"type": <value>}} to gateways that
# support it (DeepSeek: "disabled" | "enabled"). Unset = send nothing. "disabled" is the
# cheapest setting and avoids reasoning tokens eating a small max_tokens budget.
THINKING = os.environ.get("LLM_THINKING", "").strip().lower()

# ---------------------------------------------------------------------------
# Embedding / Reranker (optional — BM25 works fine without them)
# ---------------------------------------------------------------------------

# Set LLM_EMBED_ENABLED=0 to disable dense embeddings entirely (BM25 still runs).
EMBED_ENABLED = os.environ.get("LLM_EMBED_ENABLED", "1") in ("1", "true", "yes", "on")

# The cross-encoder reranker only runs when LLM_RERANK_GATEWAY_BASE points at a
# rerank server. It never falls back to the generation gateway (DeepSeek and
# OpenAI have no /rerank route). LLM_RERANK_ENABLED=0 force-disables it.
_TRUE = ("1", "true", "yes", "on")
RERANK_ENABLED = os.environ.get("LLM_RERANK_ENABLED", "1").lower() in _TRUE

# Embedding model name (only used when EMBED_ENABLED=1).
EMBED_MODEL = os.environ.get("LLM_EMBED_MODEL", "jina-embeddings-v3")

# Reranker model name (only used when RERANK_ENABLED=1).
RERANK_MODEL = os.environ.get("LLM_RERANK_MODEL", "")

# Embedding backend: "auto" tries sentence-transformers locally, then HTTP gateway.
EMBED_BACKEND   = os.environ.get("LLM_EMBED_BACKEND", "auto")
EMBED_BATCH     = int(os.environ.get("LLM_EMBED_BATCH", "16"))
EMBED_TIMEOUT   = int(os.environ.get("LLM_EMBED_TIMEOUT", "60"))
LOCAL_EMBED_MODEL = os.environ.get("LLM_LOCAL_EMBED_MODEL", "all-MiniLM-L6-v2")

# ---------------------------------------------------------------------------
# Slugs (legacy vLLM multi-model gateway support — unused in flat mode)
# Kept so existing code that imports them doesn't break; all resolve to
# the same flat endpoint below.
# ---------------------------------------------------------------------------
EMBED_SLUG   = os.environ.get("LLM_EMBED_SLUG", "embed")
RERANK_SLUG  = os.environ.get("LLM_RERANK_SLUG", "rerank")
PLANNER_SLUG = os.environ.get("LLM_PLANNER_SLUG", "chat")
GEN_SLUG     = os.environ.get("LLM_GEN_SLUG", "chat")

# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
STORAGE_BACKEND = os.environ.get("STORAGE_BACKEND", "local")
STORAGE_DIR     = os.environ.get("STORAGE_DIR",
                                 os.path.join(os.path.expanduser("~"), ".terra-pilot"))

# Jina v3 late chunking (quality boost for code chunks, gateway-only).
LATE_CHUNKING = os.environ.get("LLM_LATE_CHUNKING", "0") == "1"


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def endpoint(slug: str = "") -> str:
    """Return the base OpenAI-compatible endpoint URL (<gateway>/v1)."""
    if slug == EMBED_SLUG and os.environ.get("LLM_EMBED_GATEWAY_BASE"):
        return f"{os.environ.get('LLM_EMBED_GATEWAY_BASE').rstrip('/')}/v1"
    if slug == RERANK_SLUG and os.environ.get("LLM_RERANK_GATEWAY_BASE"):
        return f"{os.environ.get('LLM_RERANK_GATEWAY_BASE').rstrip('/')}/v1"
        
    if not GATEWAY_BASE:
        return ""
    return f"{GATEWAY_BASE.rstrip('/')}/v1"


def models_url(slug: str = "") -> str:
    base = endpoint(slug)
    return f"{base}/models" if base else ""


def rerank_base() -> str:
    """Rerank server base URL, or "" when none is configured. Read from the
    environment on every call so it can be set after import."""
    return (os.environ.get("LLM_RERANK_GATEWAY_BASE") or "").strip().rstrip("/")


def rerank_enabled() -> bool:
    """True only when a rerank server is configured and not force-disabled."""
    flag = os.environ.get("LLM_RERANK_ENABLED", "1").strip().lower()
    return flag in _TRUE and bool(rerank_base())


def rerank_url() -> str:
    base = rerank_base()
    return f"{base}/v1/rerank" if base else ""


def embed_http_base() -> str:
    """Embedding server base URL for the `auto` backend: only the dedicated
    LLM_EMBED_GATEWAY_BASE counts, never the generation gateway."""
    return (os.environ.get("LLM_EMBED_GATEWAY_BASE") or "").strip().rstrip("/")


def is_configured() -> bool:
    return bool(GATEWAY_BASE and API_KEY)
