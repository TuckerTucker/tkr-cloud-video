"""Whole-blob bounded cache eviction."""

from __future__ import annotations

from pathlib import Path

from tkr_cloud_video.artifacts.cache import CacheIndex
from tkr_cloud_video.core.errors import AppError
from tkr_cloud_video.security.validation import Sha256Digest


class CacheCapacityError(AppError):
    """Cache capacity cannot be satisfied without evicting protected blobs."""


class CacheEvictor:
    """Evicts oldest whole unreferenced blobs while protecting selected content."""

    def __init__(self, cache: CacheIndex) -> None:
        """Initialize with the cache that owns leases and capacity policy."""
        self._cache = cache

    def ensure_capacity(
        self, required_bytes: int, protected: frozenset[Sha256Digest]
    ) -> tuple[Path, ...]:
        """Evict safe candidates until a new hydration fits below high water."""
        used = self._cache.size_bytes()
        target = self._cache.policy.high_water_bytes - required_bytes
        if target < 0:
            raise CacheCapacityError(
                "cache_capacity_insufficient",
                "Required artifact exceeds the configured cache policy.",
                context={"operation": "cache_preflight"},
            )
        candidates = sorted(
            (
                path
                for path in self._cache.blob_root.glob("*/*")
                if path.is_file()
                and Sha256Digest(path.name) not in protected
                and not self._cache.is_leased(Sha256Digest(path.name))
            ),
            key=lambda path: path.stat().st_mtime,
        )
        removed: list[Path] = []
        for path in candidates:
            if used <= target:
                break
            size = path.stat().st_size
            path.unlink()
            used -= size
            removed.append(path)
        if used > target:
            raise CacheCapacityError(
                "cache_capacity_insufficient",
                "No safe cache candidate can satisfy required capacity.",
                context={"operation": "cache_preflight"},
            )
        return tuple(removed)
