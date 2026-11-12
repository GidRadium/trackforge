"""In-memory thumbnail asset cache.

The cache is intentionally process-local: it exists so one inspect call can
choose a thumbnail, a bot/GUI can display it, and a later download can write
the same bytes as a sidecar without fetching the image twice.
"""

# pyright: standard

from __future__ import annotations

import threading
from collections import OrderedDict

from .models import ThumbnailAsset

DEFAULT_MAX_BYTES = 64 * 1024 * 1024


class ThumbnailCache:
    """A small LRU cache of thumbnail bytes keyed by URL."""

    def __init__(self, *, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be >= 1")
        self.max_bytes = max_bytes
        self._items: OrderedDict[str, ThumbnailAsset] = OrderedDict()
        self._sizes: dict[str, int] = {}
        self._total_bytes = 0
        self._lock = threading.Lock()

    def get(self, url: str) -> ThumbnailAsset | None:
        if not url:
            return None
        with self._lock:
            asset = self._items.get(url)
            if asset is None:
                return None
            self._items.move_to_end(url)
            return asset

    def put(self, url: str, asset: ThumbnailAsset) -> None:
        if not url or not asset.data:
            return
        size = len(asset.data)
        if size > self.max_bytes:
            return
        with self._lock:
            previous = self._sizes.get(url)
            if previous is not None:
                self._total_bytes -= previous
                self._items.pop(url, None)
                del self._sizes[url]
            self._items[url] = asset
            self._sizes[url] = size
            self._total_bytes += size
            while self._total_bytes > self.max_bytes and self._items:
                oldest, _ = self._items.popitem(last=False)
                self._total_bytes -= self._sizes.pop(oldest, 0)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._sizes.clear()
            self._total_bytes = 0

    @property
    def total_bytes(self) -> int:
        with self._lock:
            return self._total_bytes

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def __contains__(self, url: object) -> bool:
        if not isinstance(url, str):
            return False
        with self._lock:
            return url in self._items


__all__ = ["DEFAULT_MAX_BYTES", "ThumbnailCache"]
