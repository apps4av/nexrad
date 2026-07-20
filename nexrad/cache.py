"""A tiny thread/async-safe in-memory TTL cache for rendered tiles."""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Optional, Tuple


class TileCache:
    """LRU + TTL cache mapping a key to raw PNG bytes."""

    def __init__(self, ttl_seconds: int, max_entries: int) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._store: "OrderedDict[str, Tuple[float, bytes]]" = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Optional[bytes]:
        entry = self._store.get(key)
        if entry is None:
            self.misses += 1
            return None
        ts, data = entry
        if (time.time() - ts) > self._ttl:
            # Expired.
            self._store.pop(key, None)
            self.misses += 1
            return None
        self._store.move_to_end(key)
        self.hits += 1
        return data

    def set(self, key: str, data: bytes) -> None:
        self._store[key] = (time.time(), data)
        self._store.move_to_end(key)
        while len(self._store) > self._max:
            self._store.popitem(last=False)

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {
            "entries": len(self._store),
            "max_entries": self._max,
            "ttl_seconds": self._ttl,
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 3) if total else 0.0,
        }
