"""
Embedding persistence through the pluggable StorageBackend.

Borrowed pattern from legacy_coder (semantic._save/_load): persist embeddings as a
numpy array + a JSON sidecar (model, chunk ids, per-chunk content hashes) so the
index doesn't re-embed unchanged chunks across runs. Unlike legacy_coder this goes
through StorageBackend (not a hard-coded ~/.idfc-coder path), so disk today /
shared storage later is a backend swap.

Arrays are serialized with numpy via BytesIO -> bytes, so the backend only ever
handles opaque blobs (works for disk, S3, etc.).
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from typing import List, Optional, Tuple

from storage import StorageBackend


def _np():
    try:
        import numpy as np
        return np
    except Exception:
        return None


def repo_namespace(root: str) -> str:
    """Stable per-repo key prefix for stored artifacts."""
    h = hashlib.md5(os.path.abspath(root).encode("utf-8")).hexdigest()[:12]
    return f"repos/{h}"


class EmbeddingStore:
    """Persists (meta, vectors) for one repo namespace."""

    def __init__(self, storage: StorageBackend, namespace: str):
        self.s = storage
        self.ns = namespace

    def _vkey(self) -> str:
        return f"{self.ns}/embeddings.npy"

    def _mkey(self) -> str:
        return f"{self.ns}/meta.json"

    def load(self) -> Optional[Tuple[dict, object]]:
        np = _np()
        if np is None:
            return None
        raw = self.s.read_bytes(self._vkey())
        meta_txt = self.s.read_text(self._mkey())
        if raw is None or meta_txt is None:
            return None
        try:
            vecs = np.load(io.BytesIO(raw), allow_pickle=False)
            meta = json.loads(meta_txt)
            return meta, vecs
        except Exception:
            return None

    def save(self, meta: dict, vectors) -> None:
        np = _np()
        if np is None:
            return
        buf = io.BytesIO()
        np.save(buf, vectors, allow_pickle=False)
        self.s.write_bytes(self._vkey(), buf.getvalue())
        self.s.write_text(self._mkey(), json.dumps(meta, separators=(",", ":")))

    def clear(self) -> None:
        self.s.delete(self._vkey())
        self.s.delete(self._mkey())


def content_hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8", "replace")).hexdigest()
