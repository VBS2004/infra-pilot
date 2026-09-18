"""
In-session file content cache with mtime invalidation + LRU eviction.

Borrowed pattern from legacy_coder (file_cache.py), trimmed to what the indexer
needs: avoid redundant disk reads while building / refreshing the index, and
stay fresh by checking mtime.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class _Entry:
    content: str
    mtime: float


class FileCache:
    def __init__(self, max_size: int = 256):
        self._cache: Dict[str, _Entry] = {}
        self._order: List[str] = []
        self._max = max_size
        self.hits = 0
        self.misses = 0

    def get(self, path: str) -> Optional[str]:
        ap = os.path.abspath(path)
        e = self._cache.get(ap)
        if e is None:
            self.misses += 1
            return None
        try:
            if os.path.getmtime(ap) != e.mtime:   # changed on disk -> invalidate
                self._drop(ap)
                self.misses += 1
                return None
        except OSError:
            self._drop(ap)
            self.misses += 1
            return None
        self._touch(ap)
        self.hits += 1
        return e.content

    def set(self, path: str, content: str) -> None:
        ap = os.path.abspath(path)
        try:
            mtime = os.path.getmtime(ap)
        except OSError:
            return
        self._cache[ap] = _Entry(content, mtime)
        self._touch(ap)
        while len(self._cache) > self._max and self._order:
            self._drop(self._order[0])

    def invalidate(self, path: str) -> None:
        self._drop(os.path.abspath(path))

    def clear(self) -> None:
        self._cache.clear()
        self._order.clear()

    def _touch(self, ap: str) -> None:
        if ap in self._order:
            self._order.remove(ap)
        self._order.append(ap)

    def _drop(self, ap: str) -> None:
        self._cache.pop(ap, None)
        if ap in self._order:
            self._order.remove(ap)

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {"hits": self.hits, "misses": self.misses,
                "hit_rate": (self.hits / total) if total else 0.0,
                "size": len(self._cache)}
