"""Atomic hydration, materialization, leases, and eviction tests."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.artifact_foundation.test_immutable_artifact_catalog import MemoryStore
from tkr_cloud_video.artifact_foundation.composition import (
    ArtifactDependencies,
    compose_artifact_foundation,
)
from tkr_cloud_video.artifacts.cache import CacheIndex, CachePolicy
from tkr_cloud_video.artifacts.eviction import CacheCapacityError, CacheEvictor
from tkr_cloud_video.artifacts.hydrator import ArtifactHydrator, BlobDownloader
from tkr_cloud_video.artifacts.materializer import ModelMaterializer
from tkr_cloud_video.artifacts.models import ArtifactEntry, ArtifactRole
from tkr_cloud_video.artifacts.publisher import ArtifactPublicationError
from tkr_cloud_video.security.validation import RelativeDestination, Sha256Digest


@dataclass
class MemoryDownloader(BlobDownloader):
    """Writes configured bytes to caller-owned partial paths."""

    content: bytes
    downloads: int = 0

    async def download(self, object_key: str, destination: Path) -> None:
        """Create one partial download."""
        assert object_key
        self.downloads += 1
        destination.write_bytes(self.content)


def cache_entry(content: bytes = b"model-bytes") -> ArtifactEntry:
    """Build a digest-addressed hydration entry."""
    digest = hashlib.sha256(content).hexdigest()
    return ArtifactEntry(
        name="model-1",
        role=ArtifactRole.MODEL,
        object_key=f"blobs/sha256/{digest}",
        sha256=digest,
        size_bytes=len(content),
        destination="checkpoints/model-1.bin",
    )


@pytest.mark.asyncio
async def test_hydration_promotes_atomically_and_warm_run_downloads_zero(
    tmp_path: Path,
) -> None:
    """Only verified final content appears and a warm hit performs no download."""
    entry = cache_entry()
    cache = CacheIndex(tmp_path / "cache", CachePolicy(1000, 900))
    downloader = MemoryDownloader(b"model-bytes")
    hydrator = ArtifactHydrator(cache, downloader)

    path, downloaded = await hydrator.hydrate(entry)
    warm_path, warm_downloaded = await hydrator.hydrate(entry)

    assert path == warm_path
    assert path.read_bytes() == b"model-bytes"
    assert downloaded == entry.size_bytes
    assert warm_downloaded == 0
    assert downloader.downloads == 1
    assert list(cache.partial_root.iterdir()) == []


@pytest.mark.asyncio
async def test_corrupt_download_cleans_partial_and_never_exposes_final(
    tmp_path: Path,
) -> None:
    """Checksum failure leaves neither a final blob nor partial residue."""
    entry = cache_entry()
    cache = CacheIndex(tmp_path / "cache", CachePolicy(1000, 900))
    with pytest.raises(ArtifactPublicationError):
        await ArtifactHydrator(cache, MemoryDownloader(b"corrupt")).hydrate(entry)

    assert not cache.blob_path(Sha256Digest(entry.sha256)).exists()
    assert list(cache.partial_root.iterdir()) == []


def test_materialization_is_contained_atomic_and_idempotent(tmp_path: Path) -> None:
    """Repeated materialization preserves the correct managed link."""
    model_root = tmp_path / "models"
    model_root.mkdir()
    blob = tmp_path / "blob"
    blob.write_bytes(b"verified")
    materializer = ModelMaterializer(model_root)

    first = materializer.materialize(blob, RelativeDestination("checkpoints/model.bin"))
    second = materializer.materialize(
        blob, RelativeDestination("checkpoints/model.bin")
    )

    assert first == second
    assert first.is_symlink()
    assert first.resolve() == blob


def test_materializer_refuses_unmanaged_existing_target(tmp_path: Path) -> None:
    """A regular file is never silently replaced by a managed link."""
    model_root = tmp_path / "models"
    (model_root / "checkpoints").mkdir(parents=True)
    target = model_root / "checkpoints" / "model.bin"
    target.write_bytes(b"user-owned")
    blob = tmp_path / "blob"
    blob.write_bytes(b"verified")

    with pytest.raises(FileExistsError):
        ModelMaterializer(model_root).materialize(
            blob, RelativeDestination("checkpoints/model.bin")
        )


def test_eviction_removes_only_old_unprotected_unleased_blobs(tmp_path: Path) -> None:
    """Selected and actively leased digests remain intact under capacity pressure."""
    cache = CacheIndex(tmp_path / "cache", CachePolicy(30, 20))
    cache.initialize()
    digests = [Sha256Digest(character * 64) for character in ("a", "b", "c")]
    for digest in digests:
        path = cache.blob_path(digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 8)

    with cache.lease(digests[1]):
        removed = CacheEvictor(cache).ensure_capacity(4, frozenset({digests[2]}))

    assert removed == (cache.blob_path(digests[0]),)
    assert cache.blob_path(digests[1]).exists()
    assert cache.blob_path(digests[2]).exists()


def test_capacity_failure_happens_before_download(tmp_path: Path) -> None:
    """Impossible capacity fails without creating a partial download."""
    cache = CacheIndex(tmp_path / "cache", CachePolicy(10, 8))
    cache.initialize()
    with pytest.raises(CacheCapacityError):
        CacheEvictor(cache).ensure_capacity(9, frozenset())


def test_artifact_composition_exposes_complete_service_set(tmp_path: Path) -> None:
    """The artifact public boundary injects storage and configured roots once."""
    model_root = tmp_path / "models"
    model_root.mkdir()
    services = compose_artifact_foundation(
        ArtifactDependencies(
            MemoryStore(),
            MemoryDownloader(b"data"),
            tmp_path / "cache",
            model_root,
            CachePolicy(100, 80),
        )
    )

    assert services.publisher is not None
    assert services.resolver is not None
    assert services.hydrator is not None
    assert services.evictor is not None
