"""local_generator.py - air-gapped, transformers-only drop-in for generator.complete.

Loads a local HuggingFace causal LM directly with transformers — NO gateway,
NO network — for an air-gapped GPU box.

Usage:
    export LOCAL_MODEL=/path/to/model   # local HF weights dir
    export TRANSFORMERS_OFFLINE=1
    export HF_HUB_OFFLINE=1
    export LLM_EMBED_ENABLED=0          # embedder calls the gateway -> off
    export LLM_RERANK_ENABLED=0         # reranker calls the gateway -> off
    python cli.py "$REPO" compose "..."
"""
from __future__ import annotations

import os
from typing import Dict, List, Optional

_MODEL = None
_TOK = None


def _load() -> None:
    """Load tokenizer + model once, from local files only (no hub download)."""
    global _MODEL, _TOK
    if _MODEL is not None:
        return
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    path = os.environ.get("LOCAL_MODEL")
    if not path:
        raise RuntimeError(
            "set LOCAL_MODEL to the local model directory (HF-format weights)"
        )
    if not os.path.isdir(path):
        raise RuntimeError(f"LOCAL_MODEL is not a directory: {path}")

    # bf16 on Ada (L4) is fine; fall back to fp16 if bf16 is unsupported.
    dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16

    _TOK = AutoTokenizer.from_pretrained(
        path, local_files_only=True, trust_remote_code=True
    )
    _MODEL = AutoModelForCausalLM.from_pretrained(
        path,
        local_files_only=True,
        trust_remote_code=True,
        torch_dtype=dtype,
        low_cpu_mem_usage=True,   # stream weights in; avoids a 2x RAM spike on load
    )
    _MODEL.to("cuda")
    _MODEL.eval()


def complete(
    messages: List[Dict[str, str]],
    *,
    slug: str = "",
    fallback_slug: Optional[str] = None,
    temperature: float = 0.1,
    max_tokens: int = 2048,
    **_ignored,
) -> str:
    """Drop-in for generator.complete.

    Same call shape (messages, temperature, max_tokens) and returns the assistant
    text as a string. slug / fallback_slug are accepted but ignored - there is a
    single local model here.
    """
    import torch

    _load()

    # Qwen / most -Instruct models ship a chat template; use it so the system+user
    # roles are framed exactly as the model expects. Plain-join fallback otherwise.
    if getattr(_TOK, "chat_template", None):
        prompt = _TOK.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )
    else:
        prompt = (
            "\n\n".join(f"{m['role'].upper()}:\n{m['content']}" for m in messages)
            + "\n\nASSISTANT:\n"
        )

    inputs = _TOK(prompt, return_tensors="pt").to(_MODEL.device)
    do_sample = bool(temperature and temperature > 0)

    with torch.no_grad():
        out = _MODEL.generate(
            **inputs,
            max_new_tokens=max_tokens,
            do_sample=do_sample,
            temperature=(temperature if do_sample else None),
            top_p=(0.95 if do_sample else None),
            pad_token_id=(_TOK.pad_token_id or _TOK.eos_token_id),
        )

    # Decode ONLY the newly generated tokens (strip the prompt back off).
    gen = out[0][inputs["input_ids"].shape[1]:]
    return _TOK.decode(gen, skip_special_tokens=True).strip()


if __name__ == "__main__":
    # quick smoke test: python3 local_generator.py
    msgs = [
        {"role": "system", "content": "You are a terse assistant. Output only HCL."},
        {"role": "user", "content": "emit: inputs = { env_name = \"demo\" }"},
    ]
    print(complete(msgs, max_tokens=8192))
