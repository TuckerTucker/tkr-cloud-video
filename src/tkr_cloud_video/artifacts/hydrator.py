"""Concurrent-safe verified cache hydration."""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from typing import Protocol

from tkr_cloud_video.artifacts.cache import CacheIndex
from tkr_cloud_video.artifacts.models import ArtifactEntry
from tkr_cloud_video.artifacts.publisher import ArtifactPublicationError
from tkr_cloud_video.security.validation import Sha256Digest


class BlobDownloader(Protocol):
    """Read-only object-store adapter for artifact hydration."""

    async def download(self, object_key: str, destination: Path) -> None:
        """Download exact object bytes to a caller-owned partial path."""
        ...


class ArtifactHydrator:
    """Downloads missing blobs to unique partials and atomically promotes them."""

    def __init__(self, cache: CacheIndex, downloader: BlobDownloader) -> None:
        """Initialize with injected cache policy and downloader ports."""
        self._cache = cache
        self._downloader = downloader
        self._locks: dict[str, asyncio.Lock] = {}

    async def hydrate(self, entry: ArtifactEntry) -> tuple[Path, int]:
        """Return verified cache path and downloaded bytes (zero on a warm hit)."""
        digest = Sha256Digest(entry.sha256)
        self._cache.initialize()
        lock = self._locks.setdefault(entry.sha256, asyncio.Lock())
        async with lock:
            final = self._cache.blob_path(digest)
            if self._cache.verify(digest, entry.size_bytes):
                return final, 0
            if final.exists():
                final.unlink()
            partial = (
                self._cache.partial_root / f"{entry.sha256}.{uuid.uuid4().hex}.part"
            )
            try:
                await self._downloader.download(entry.object_key, partial)
                if not self._cache.verify_partial(partial, digest, entry.size_bytes):
                    raise ArtifactPublicationError(
                        "hydration_verification_failed",
                        "Downloaded artifact failed size or digest verification.",
                        context={"resource_id": entry.name},
                    )
                final.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.replace(partial, final)
                return final, entry.size_bytes
            finally:
                partial.unlink(missing_ok=True)
