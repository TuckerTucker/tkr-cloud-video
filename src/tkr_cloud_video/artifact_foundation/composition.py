"""IoC composition for immutable publication and deterministic hydration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tkr_cloud_video.artifacts.cache import CacheIndex, CachePolicy
from tkr_cloud_video.artifacts.eviction import CacheEvictor
from tkr_cloud_video.artifacts.hydrator import ArtifactHydrator, BlobDownloader
from tkr_cloud_video.artifacts.materializer import ModelMaterializer
from tkr_cloud_video.artifacts.publisher import ArtifactPublisher, ArtifactStore
from tkr_cloud_video.artifacts.resolver import ApprovedReleaseResolver


@dataclass(frozen=True, slots=True)
class ArtifactDependencies:
    """External adapters and configured roots required by artifact services."""

    store: ArtifactStore
    downloader: BlobDownloader
    cache_root: Path
    model_root: Path
    cache_policy: CachePolicy


@dataclass(frozen=True, slots=True)
class ArtifactServices:
    """Composed artifact catalog and cache hydration services."""

    publisher: ArtifactPublisher
    resolver: ApprovedReleaseResolver
    cache: CacheIndex
    hydrator: ArtifactHydrator
    materializer: ModelMaterializer
    evictor: CacheEvictor


def compose_artifact_foundation(
    dependencies: ArtifactDependencies,
) -> ArtifactServices:
    """Compose all artifact services from explicit storage and path dependencies."""
    cache = CacheIndex(dependencies.cache_root, dependencies.cache_policy)
    return ArtifactServices(
        publisher=ArtifactPublisher(dependencies.store),
        resolver=ApprovedReleaseResolver(dependencies.store),
        cache=cache,
        hydrator=ArtifactHydrator(cache, dependencies.downloader),
        materializer=ModelMaterializer(dependencies.model_root),
        evictor=CacheEvictor(cache),
    )
