"""Streaming publication for large, digest-pinned remote artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from tkr_cloud_video.artifacts.models import ArtifactEntry, ModelSetManifest
from tkr_cloud_video.artifacts.publisher import (
    ArtifactPublicationError,
    ArtifactPublisher,
    ArtifactStore,
    ObjectMetadata,
    PublicationReceipt,
)


class StreamingArtifactStore(ArtifactStore, Protocol):
    """Artifact store that can transfer a trusted HTTPS source without buffering."""

    async def put_url(
        self, key: str, url: str, sha256: str, size_bytes: int
    ) -> ObjectMetadata:
        """Stream a URL into one immutable object and return verified metadata."""
        ...


@dataclass(frozen=True, slots=True)
class UrlArtifactSource:
    """One immutable remote source whose digest is independently pinned."""

    url: str


ArtifactSource = Path | UrlArtifactSource


class StreamingArtifactPublisher:
    """Publish local and URL-backed artifacts with one publish-last transaction."""

    def __init__(self, store: StreamingArtifactStore) -> None:
        """Initialize with an explicitly streaming-capable private store."""
        self._store = store
        self._publisher = ArtifactPublisher(store)

    async def publish(
        self, manifest: ModelSetManifest, sources: dict[str, ArtifactSource]
    ) -> PublicationReceipt:
        """Verify every source and remote object before committing the manifest."""
        reused = 0
        uploaded = 0
        for entry in manifest.artifacts:
            source = sources.get(entry.name)
            if source is None:
                raise ArtifactPublicationError(
                    "artifact_source_missing",
                    "A manifest artifact has no publication source.",
                    context={"resource_id": entry.name},
                )
            was_reused = (
                await self._publisher.publish_blob(source, entry)
                if isinstance(source, Path)
                else await self._publish_url(source, entry)
            )
            if was_reused:
                reused += 1
            else:
                uploaded += 1
        return await self._publisher.publish_manifest(manifest, reused, uploaded)

    async def _publish_url(
        self, source: UrlArtifactSource, entry: ArtifactEntry
    ) -> bool:
        current = await self._store.head(entry.object_key)
        if current is not None:
            self._validate_metadata(current, entry, "immutable_blob_conflict")
            return True
        created = await self._store.put_url(
            entry.object_key,
            source.url,
            entry.sha256,
            entry.size_bytes,
        )
        self._validate_metadata(created, entry, "remote_verification_failed")
        return False

    @staticmethod
    def _validate_metadata(
        metadata: ObjectMetadata, entry: ArtifactEntry, error_code: str
    ) -> None:
        if metadata.size_bytes != entry.size_bytes or metadata.sha256 != entry.sha256:
            raise ArtifactPublicationError(
                error_code,
                "Remote artifact does not match its immutable catalog entry.",
                context={"resource_id": entry.name},
            )
