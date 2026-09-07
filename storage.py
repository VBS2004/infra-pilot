"""
Pluggable storage backend for index + embedding persistence.

TODAY: LocalDiskStorage (per-client disk) -- chosen for the prototype.
LATER: the earlier design plan calls for SHARED storage on a server. When that
happens, implement a new StorageBackend (S3 / NFS / HTTP object store) and select
it via PAYMENTS_STORAGE_BACKEND. NOTHING at the call sites changes -- they only
ever see the StorageBackend interface (read_bytes / write_bytes / exists / ...).
This is the "make it easily changeable later" seam.

Keys are POSIX-style relative strings, e.g. "<repo-hash>/embeddings.npy".
"""
from __future__ import annotations

import abc
import os
import shutil
from typing import List, Optional


class StorageBackend(abc.ABC):
    """Backend-agnostic blob store. Implement this to swap disk -> shared."""

    @abc.abstractmethod
    def read_bytes(self, key: str) -> Optional[bytes]: ...

    @abc.abstractmethod
    def write_bytes(self, key: str, data: bytes) -> None: ...

    @abc.abstractmethod
    def exists(self, key: str) -> bool: ...

    @abc.abstractmethod
    def delete(self, key: str) -> None: ...

    @abc.abstractmethod
    def list_keys(self, prefix: str = "") -> List[str]: ...

    # text convenience (shared by all backends)
    def read_text(self, key: str) -> Optional[str]:
        b = self.read_bytes(key)
        return b.decode("utf-8") if b is not None else None

    def write_text(self, key: str, text: str) -> None:
        self.write_bytes(key, text.encode("utf-8"))


class LocalDiskStorage(StorageBackend):
    """Per-client disk implementation rooted at a single directory."""

    def __init__(self, root: str):
        self.root = os.path.abspath(os.path.expanduser(root))
        os.makedirs(self.root, exist_ok=True)

    def _p(self, key: str) -> str:
        return os.path.join(self.root, key.replace("/", os.sep))

    def read_bytes(self, key: str) -> Optional[bytes]:
        p = self._p(key)
        if not os.path.exists(p):
            return None
        with open(p, "rb") as f:
            return f.read()

    def write_bytes(self, key: str, data: bytes) -> None:
        p = self._p(key)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)

    def exists(self, key: str) -> bool:
        return os.path.exists(self._p(key))

    def delete(self, key: str) -> None:
        p = self._p(key)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        elif os.path.exists(p):
            os.remove(p)

    def list_keys(self, prefix: str = "") -> List[str]:
        out: List[str] = []
        for dp, _, fns in os.walk(self.root):
            for fn in fns:
                rel = os.path.relpath(os.path.join(dp, fn), self.root).replace(os.sep, "/")
                if rel.startswith(prefix):
                    out.append(rel)
        return sorted(out)


# --------------------------------------------------------------------------- #
# Selection (single seam) -- swap backends here when shared storage lands.
# --------------------------------------------------------------------------- #
_STORAGE: Optional[StorageBackend] = None


def get_storage() -> StorageBackend:
    global _STORAGE
    if _STORAGE is not None:
        return _STORAGE
    import config
    backend = (config.STORAGE_BACKEND or "local").lower()
    if backend == "local":
        _STORAGE = LocalDiskStorage(config.STORAGE_DIR)
    else:
        # FUTURE: elif backend == "s3": _STORAGE = S3Storage(...) etc.
        raise ValueError(f"unknown PAYMENTS_STORAGE_BACKEND={backend!r} "
                         f"(implement a StorageBackend and register it here)")
    return _STORAGE


def set_storage(storage: Optional[StorageBackend]) -> None:
    """Explicitly wire a backend (used by tests / shared-storage bootstrap)."""
    global _STORAGE
    _STORAGE = storage
